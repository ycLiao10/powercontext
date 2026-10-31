# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Concurrent embedding request coalescing with a bounded vector cache.

Memory writes and vector searches both embed text through the same
:class:`~powercontext.builtin.inference.protocols.EmbeddingModel` port. Without
coordination two callers that need the same vector pay for it twice, and a
caller that arrives while the provider is still working may start the very
same request again.

This module adds one wrapper around a provider model:

* identical texts inside one call are deduplicated;
* identical texts across concurrent calls share one provider request, including
  callers that arrive while that request is already running;
* every successful vector is kept in a bounded LRU cache, so later callers
  reuse it without touching the provider;
* one caller being cancelled never cancels the provider work other callers
  are waiting for, and never makes them lose the result;
* usage is summarised per caller over the distinct provider requests that
  caller waited for, so a shared request is charged to each of its waiters
  while a pure cache hit charges nothing.

Models that do not use this wrapper keep their previous behaviour: the wrapper
only adds coordination around a provider model, it never changes the
``EmbeddingResult`` contract.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["CoalescingEmbeddingModel"]


def _discard_exception(future: asyncio.Future[EmbeddingVector]) -> None:
    """Mark a settled future as observed so an abandoned waiter stays quiet."""

    if not future.cancelled():
        future.exception()


@dataclass(slots=True)
class _PendingText:
    """One text whose vector is shared by every caller that asked for it."""

    text: str
    future: asyncio.Future[EmbeddingVector]
    batch: _Batch | None = None


@dataclass(slots=True)
class _Batch:
    """One provider request shared by the callers waiting for its texts."""

    texts: tuple[str, ...]
    entries: tuple[_PendingText, ...]
    usage: InferenceUsage = field(default_factory=lambda: InferenceUsage(requests=0))


class CoalescingEmbeddingModel:
    """Merge concurrent embedding requests and cache successful vectors."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        """Wrap ``model`` with request coalescing and a bounded vector cache."""

        if max_batch_size < 1:
            raise ValueError("max_batch_size must be at least 1")  # noqa: TRY003
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must not be negative")  # noqa: TRY003
        if cache_size < 0:
            raise ValueError("cache_size must not be negative")  # noqa: TRY003
        self._model = model
        self.profile = model.profile
        self._max_batch_size = max_batch_size
        self._coalesce_ms = coalesce_ms
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._waiting: dict[str, _PendingText] = {}
        self._running: dict[str, _PendingText] = {}
        self._workers: set[asyncio.Task[None]] = set()
        self._dispatcher: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one vector per input text, sharing and caching provider work."""

        requested = tuple(texts)
        if not requested:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        self._bind_loop()
        vectors: dict[str, EmbeddingVector] = {}
        entries: list[_PendingText] = []
        for text in dict.fromkeys(requested):
            cached = self._cached_vector(text)
            if cached is not None:
                vectors[text] = cached
                continue
            entry = self._waiting.get(text) or self._running.get(text)
            if entry is None:
                entry = _PendingText(text=text, future=asyncio.get_running_loop().create_future())
                entry.future.add_done_callback(_discard_exception)
                self._waiting[text] = entry
                self._request_dispatch()
            entries.append(entry)
        if not entries:
            return EmbeddingResult(
                vectors=tuple(vectors[text] for text in requested),
                usage=InferenceUsage(requests=0),
            )
        resolved = await asyncio.gather(*(asyncio.shield(entry.future) for entry in entries))
        for entry, vector in zip(entries, resolved, strict=True):
            vectors[entry.text] = vector
        return EmbeddingResult(
            vectors=tuple(vectors[text] for text in requested),
            usage=self._summarise_usage(entries),
        )

    def _bind_loop(self) -> None:
        """Drop state that belongs to an event loop this model no longer runs on."""

        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        self._waiting.clear()
        self._running.clear()
        self._dispatcher = None
        self._wake = asyncio.Event()

    def _request_dispatch(self) -> None:
        """Make sure one dispatcher is scheduled for the pending texts."""

        self._wake.set()
        if self._dispatcher is None:
            self._dispatcher = asyncio.get_running_loop().create_task(self._dispatch_loop())

    async def _dispatch_loop(self) -> None:
        """Turn pending texts into provider requests."""

        try:
            while True:
                while not self._waiting:
                    self._wake.clear()
                    if self._waiting:
                        break
                    await self._wake.wait()
                await self._coalesce_window()
                while self._waiting:
                    chunk = self._take_chunk()
                    if not chunk:
                        break
                    self._start_batch(chunk)
                if not self._waiting:
                    return
        finally:
            self._dispatcher = None

    async def _coalesce_window(self) -> None:
        """Give concurrent callers a chance to join the same provider request."""

        if self._coalesce_ms > 0:
            await asyncio.sleep(self._coalesce_ms / 1000)
            return
        # A zero window still yields once, so callers that are already queued in
        # this event loop iteration join the same request before it is sent.
        expected = len(self._waiting)
        while True:
            await asyncio.sleep(0)
            current = len(self._waiting)
            if current == expected:
                return
            expected = current

    def _take_chunk(self) -> tuple[_PendingText, ...]:
        """Move up to ``max_batch_size`` pending texts into one provider request."""

        chunk: list[_PendingText] = []
        for text in list(self._waiting):
            if len(chunk) >= self._max_batch_size:
                break
            entry = self._waiting.pop(text)
            self._running[text] = entry
            chunk.append(entry)
        return tuple(chunk)

    def _start_batch(self, entries: tuple[_PendingText, ...]) -> None:
        """Send one provider request for the given texts."""

        batch = _Batch(texts=tuple(entry.text for entry in entries), entries=entries)
        for entry in entries:
            entry.batch = batch
        task = asyncio.get_running_loop().create_task(self._run_batch(batch))
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)

    async def _run_batch(self, batch: _Batch) -> None:
        """Run one provider request and hand its vectors to every waiter."""

        try:
            result = await self._model.embed(batch.texts)
            vectors = self._checked_vectors(batch, result)
            usage = result.usage
        except asyncio.CancelledError:
            self._fail(batch, asyncio.CancelledError())
            raise
        except Exception as error:
            self._fail(batch, error)
            return
        batch.usage = usage
        for entry, vector in zip(batch.entries, vectors, strict=True):
            self._running.pop(entry.text, None)
            self._store(entry.text, vector)
            if not entry.future.done():
                entry.future.set_result(vector)

    def _fail(self, batch: _Batch, error: BaseException) -> None:
        """Hand one provider failure to the waiters of that batch only."""

        for entry in batch.entries:
            self._running.pop(entry.text, None)
            if not entry.future.done():
                entry.future.set_exception(error)

    @staticmethod
    def _checked_vectors(batch: _Batch, result: EmbeddingResult) -> tuple[EmbeddingVector, ...]:
        """Return the provider vectors, refusing a count that does not match the batch."""

        vectors = result.vectors
        if len(vectors) != len(batch.texts):
            raise ValueError("embedding provider returned a vector count that does not match the batch")  # noqa: TRY003
        return vectors

    def _cached_vector(self, text: str) -> EmbeddingVector | None:
        """Return a cached vector and mark the text as most recently used."""

        if self._cache_size <= 0:
            return None
        vector = self._cache.get(text)
        if vector is None:
            return None
        self._cache.move_to_end(text)
        return vector

    def _store(self, text: str, vector: EmbeddingVector) -> None:
        """Cache one successful vector, evicting the least recently used one."""

        if self._cache_size <= 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _summarise_usage(self, entries: Iterable[_PendingText]) -> InferenceUsage:
        """Sum the usage of the distinct provider requests one caller waited for."""

        batches: dict[int, _Batch] = {}
        for entry in entries:
            if entry.batch is not None:
                batches.setdefault(id(entry.batch), entry.batch)
        requests = sum(batch.usage.requests for batch in batches.values())
        input_tokens = self._sum_tokens(batch.usage.input_tokens for batch in batches.values())
        output_tokens = self._sum_tokens(batch.usage.output_tokens for batch in batches.values())
        return InferenceUsage(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)

    @staticmethod
    def _sum_tokens(values: Iterable[int | None]) -> int | None:
        """Sum provider token counts, keeping an unknown count unknown."""

        total = 0
        for value in values:
            if value is None:
                return None
            total += value
        return total
