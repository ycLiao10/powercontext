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

"""Tests for the coalescing embedding wrapper."""

from __future__ import annotations

import asyncio

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts

TEST_PROFILE = EmbeddingProfile(profile_id="coalescing-test", model="local", dimension=2)


class RecordingEmbedding:
    """Deterministic offline provider that records every request it receives."""

    profile = TEST_PROFILE

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._index: dict[str, int] = {}

    def vector(self, text: str) -> tuple[float, ...]:
        index = self._index.setdefault(text, len(self._index))
        return (float(index) + 1.0, 1.0)

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        self.calls.append(tuple(texts))
        return EmbeddingResult(
            vectors=tuple(self.vector(text) for text in texts),
            usage=InferenceUsage(requests=1),
        )


def test_deduplicates_one_call_and_restores_the_original_order() -> None:
    async def scenario() -> None:
        base = RecordingEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        result = await model.embed(("alpha", "beta", "alpha"))
        assert result.vectors == (base.vector("alpha"), base.vector("beta"), base.vector("alpha"))
        assert base.calls == [("alpha", "beta")]

    asyncio.run(scenario())


def test_merges_concurrent_calls_and_keeps_batches_within_the_limit() -> None:
    async def scenario() -> None:
        base = RecordingEmbedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=20, cache_size=8)
        first, second = await asyncio.gather(
            model.embed(("alpha", "beta", "alpha")),
            model.embed(("beta", "gamma")),
        )
        assert first.vectors == (base.vector("alpha"), base.vector("beta"), base.vector("alpha"))
        assert second.vectors == (base.vector("beta"), base.vector("gamma"))
        assert sorted(text for call in base.calls for text in call) == ["alpha", "beta", "gamma"]
        assert all(len(call) <= 2 for call in base.calls)
        assert first.usage.requests == sum(bool(set(call) & {"alpha", "beta"}) for call in base.calls)
        assert second.usage.requests == sum(bool(set(call) & {"beta", "gamma"}) for call in base.calls)

    asyncio.run(scenario())


def test_late_caller_reuses_a_running_request() -> None:
    class Blocking(RecordingEmbedding):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.calls.append(tuple(texts))
            self.started.set()
            await self.release.wait()
            return EmbeddingResult(vectors=tuple(self.vector(text) for text in texts))

    async def scenario() -> None:
        base = Blocking()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        first = asyncio.create_task(model.embed(("shared",)))
        await base.started.wait()
        second = asyncio.create_task(model.embed(("shared",)))
        await asyncio.sleep(0)
        base.release.set()
        assert (await first).vectors == (base.vector("shared"),)
        assert (await second).vectors == (base.vector("shared"),)
        assert base.calls == [("shared",)]

    asyncio.run(scenario())


def test_empty_input_never_reaches_the_provider() -> None:
    async def scenario() -> None:
        base = RecordingEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        result = await model.embed(())
        assert result.vectors == ()
        assert result.usage.requests == 0
        assert base.calls == []

    asyncio.run(scenario())


def test_cache_evicts_the_least_recently_used_vector() -> None:
    async def scenario() -> None:
        base = RecordingEmbedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=2)
        await model.embed(("alpha",))
        await model.embed(("beta",))
        await model.embed(("alpha",))
        await model.embed(("gamma",))
        await model.embed(("alpha",))
        assert sum(call.count("alpha") for call in base.calls) == 1
        cached = await model.embed(("alpha",))
        assert cached.vectors == (base.vector("alpha"),)
        await model.embed(("beta",))
        assert sum(call.count("beta") for call in base.calls) == 2

    asyncio.run(scenario())


def test_disabled_cache_still_shares_running_requests() -> None:
    class Blocking(RecordingEmbedding):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.calls.append(tuple(texts))
            self.started.set()
            await self.release.wait()
            return EmbeddingResult(vectors=tuple(self.vector(text) for text in texts))

    async def scenario() -> None:
        base = Blocking()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)
        first = asyncio.create_task(model.embed(("shared",)))
        second = asyncio.create_task(model.embed(("shared",)))
        await base.started.wait()
        base.release.set()
        assert (await first).vectors == (base.vector("shared"),)
        assert (await second).vectors == (base.vector("shared"),)
        assert base.calls == [("shared",)]
        await model.embed(("shared",))
        assert len(base.calls) == 2

    asyncio.run(scenario())


def test_failure_is_not_cached_and_other_batches_still_finish() -> None:
    class Flaky(RecordingEmbedding):
        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            if "broken" in texts:
                self.calls.append(tuple(texts))
                raise RuntimeError("provider failed")  # noqa: TRY003
            return await super().embed(texts)

    async def scenario() -> None:
        base = Flaky()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        with pytest.raises(RuntimeError):
            await model.embed(("broken",))
        with pytest.raises(RuntimeError):
            await model.embed(("broken",))
        assert (await model.embed(("fine",))).vectors == (base.vector("fine"),)

    asyncio.run(scenario())


def test_a_mismatched_vector_count_raises_value_error() -> None:
    class Broken(RecordingEmbedding):
        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.calls.append(tuple(texts))
            return EmbeddingResult(vectors=())

    async def scenario() -> None:
        model = CoalescingEmbeddingModel(Broken(), coalesce_ms=0)
        with pytest.raises(ValueError):
            await model.embed(("broken",))

    asyncio.run(scenario())


def test_cancelling_one_waiter_keeps_the_shared_request_alive() -> None:
    class Blocking(RecordingEmbedding):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.calls.append(tuple(texts))
            self.started.set()
            await self.release.wait()
            return EmbeddingResult(vectors=tuple(self.vector(text) for text in texts))

    async def scenario() -> None:
        base = Blocking()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        cancelled = asyncio.create_task(model.embed(("shared",)))
        survivor = asyncio.create_task(model.embed(("shared",)))
        await base.started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        base.release.set()
        assert (await survivor).vectors == (base.vector("shared"),)
        assert base.calls == [("shared",)]

    asyncio.run(scenario())


def test_usage_keeps_provider_tokens_and_ignores_cache_hits() -> None:
    class Metered(RecordingEmbedding):
        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.calls.append(tuple(texts))
            return EmbeddingResult(
                vectors=tuple(self.vector(text) for text in texts),
                usage=InferenceUsage(requests=1, input_tokens=7, output_tokens=0),
            )

    async def scenario() -> None:
        base = Metered()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        result = await model.embed(("first", "first"))
        assert result.usage.requests == 1
        assert result.usage.input_tokens == 7
        assert result.usage.output_tokens == 0
        cached = await model.embed(("first",))
        assert cached.vectors == (base.vector("first"),)
        assert cached.usage.requests == 0
        assert cached.usage.input_tokens in (None, 0)

    asyncio.run(scenario())


def test_native_memory_budget_across_six_scopes() -> None:
    async def scenario() -> None:
        base = RecordingEmbedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=6, coalesce_ms=0, cache_size=48)
        texts = tuple(f"memory entry {index:02d}" for index in range(24))
        scopes = tuple(f"coalescing-scope-{index}" for index in range(6))
        async with open_builtin_contexts(BuiltinConfig(database=SQLiteConfig()), embedding_model=model) as contexts:
            async def write(scope_id: str):
                service = (await contexts.get(scope_id)).artifacts.memory
                return await service.remember(
                    memory=None,
                    entries=tuple(MemoryEntryInput(kind="fact", text=text) for text in texts),
                    mode="append",
                )

            memories = await asyncio.gather(*(write(scope_id) for scope_id in scopes))
            assert all(memory is not None for memory in memories)
            assert len(base.calls) == 4
            assert all(len(call) <= 6 for call in base.calls)
            assert sorted(text for call in base.calls for text in call) == sorted(texts)

            async def query(scope_id: str, memory):
                service = (await contexts.get(scope_id)).artifacts.memory
                return await service.search(texts[0], memories=(memory,), limit=1, mode="vector")

            results = await asyncio.gather(
                *(query(scope_id, memory) for scope_id, memory in zip(scopes, memories, strict=True))
            )
            assert len(base.calls) == 4
            for result in results:
                assert result.hits[0].text == texts[0]

    asyncio.run(scenario())
