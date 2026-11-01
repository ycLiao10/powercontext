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

"""Share Embedding work between concurrent callers and cache successful vectors.

The wrapper turns many overlapping text batches into a few provider requests: identical
texts already being computed are joined instead of requested again, and a bounded LRU
cache answers later calls without touching the provider. Every caller still receives its
own vectors in its own order, keeps the usage of the batches it actually waited for, and
is isolated from another caller's cancellation.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel

_EMPTY_USAGE = InferenceUsage(requests=0)


@dataclass(slots=True)
class _Batch:
    """One provider request and the pending texts it resolves."""

    identifier: int
    texts: tuple[str, ...]
    usage: InferenceUsage | None = None
    futures: tuple[asyncio.Future[EmbeddingVector], ...] = ()


class CoalescingEmbeddingModel:
    """Wrap one Embedding model with cross-call batching and a bounded vector cache."""

    def __init__(
        self,
        model: EmbeddingModel,
        /,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be positive")  # noqa: TRY003
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must not be negative")  # noqa: TRY003
        if cache_size < 0:
            raise ValueError("cache_size must not be negative")  # noqa: TRY003
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1000.0
        self._cache_size = cache_size
        self.profile: EmbeddingProfile = model.profile
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._pending: dict[str, asyncio.Future[EmbeddingVector]] = {}
        self._unscheduled: list[str] = []
        self._batch_of: dict[asyncio.Future[EmbeddingVector], _Batch] = {}
        self._dispatcher: asyncio.Task[None] | None = None
        self._batches = 0

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one vector per input text, sharing provider work with concurrent calls."""

        ordered = tuple(texts)
        if not ordered:
            return EmbeddingResult(vectors=(), usage=_EMPTY_USAGE)
        unique = tuple(dict.fromkeys(ordered))
        futures: dict[str, asyncio.Future[EmbeddingVector]] = {}
        fresh = False
        for text in unique:
            cached = self._cached(text)
            if cached is not None:
                futures[text] = _resolved(cached)
                continue
            future = self._pending.get(text)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._pending[text] = future
                self._unscheduled.append(text)
                fresh = True
            futures[text] = future
        if fresh:
            self._kick()
        vectors = await asyncio.gather(*(asyncio.shield(futures[text]) for text in unique))
        resolved = dict(zip(unique, vectors, strict=True))
        return EmbeddingResult(
            vectors=tuple(resolved[text] for text in ordered),
            usage=self._usage_for(unique, futures),
        )

    def _cached(self, text: str) -> EmbeddingVector | None:
        if self._cache_size == 0:
            return None
        vector = self._cache.get(text)
        if vector is not None:
            self._cache.move_to_end(text)
        return vector

    def _remember(self, text: str, vector: EmbeddingVector) -> None:
        if self._cache_size == 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _usage_for(
        self,
        unique: tuple[str, ...],
        futures: dict[str, asyncio.Future[EmbeddingVector]],
    ) -> InferenceUsage:
        seen: set[int] = set()
        requests = 0
        input_tokens: int | None = None
        output_tokens: int | None = None
        for text in unique:
            batch = self._batch_of.get(futures[text])
            if batch is None or batch.identifier in seen:
                continue
            seen.add(batch.identifier)
            usage = batch.usage or _EMPTY_USAGE
            requests += usage.requests
            input_tokens = _total(input_tokens, usage.input_tokens)
            output_tokens = _total(output_tokens, usage.output_tokens)
        return InferenceUsage(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)

    def _kick(self) -> None:
        if self._dispatcher is not None and not self._dispatcher.done():
            return
        self._dispatcher = asyncio.get_running_loop().create_task(self._drain())

    async def _drain(self) -> None:
        if self._coalesce_seconds > 0:
            await asyncio.sleep(self._coalesce_seconds)
        while self._unscheduled:
            batch = self._claim()
            if batch is None:
                continue
            await self._run(batch)

    def _claim(self) -> _Batch | None:
        texts: list[str] = []
        futures: list[asyncio.Future[EmbeddingVector]] = []
        while self._unscheduled and len(texts) < self._max_batch_size:
            text = self._unscheduled.pop(0)
            future = self._pending.get(text)
            if future is None:
                continue
            texts.append(text)
            futures.append(future)
        if not texts:
            return None
        self._batches += 1
        batch = _Batch(identifier=self._batches, texts=tuple(texts), futures=tuple(futures))
        for future in futures:
            self._batch_of[future] = batch
        return batch

    async def _run(self, batch: _Batch) -> None:
        try:
            result = await self._model.embed(batch.texts)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._release(batch, error)
            return
        if len(result.vectors) != len(batch.texts):
            self._release(batch, ValueError("embedding model returned the wrong number of vectors"))
            return
        batch.usage = result.usage
        for text, vector, future in zip(batch.texts, result.vectors, batch.futures, strict=True):
            self._pending.pop(text, None)
            self._remember(text, vector)
            if not future.done():
                future.set_result(vector)

    def _release(self, batch: _Batch, error: BaseException) -> None:
        """Fail one batch without caching it, so a later call can retry the texts."""

        for text, future in zip(batch.texts, batch.futures, strict=True):
            self._pending.pop(text, None)
            if not future.done():
                future.set_exception(error)
                future.exception()


def _resolved(vector: EmbeddingVector) -> asyncio.Future[EmbeddingVector]:
    """Return an already-completed future for one cached vector."""

    future: asyncio.Future[EmbeddingVector] = asyncio.get_running_loop().create_future()
    future.set_result(vector)
    return future


def _total(current: int | None, extra: int | None) -> int | None:
    if extra is None:
        return current
    return extra if current is None else current + extra


__all__ = ["CoalescingEmbeddingModel"]
