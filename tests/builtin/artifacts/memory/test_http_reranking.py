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

"""Behavior tests for the HTTP Memory reranking adapter."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts

_ENDPOINT = "https://reranker.invalid/score"


def _hit(rank: int) -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id="memory", revision=1),
        entry_id=f"entry-{rank}",
        entry_version_id=f"entry-{rank}-v1",
        text=f"Candidate {rank}",
        score=1 / rank,
        matched_by=("fts",),
    )


def _candidates(count: int) -> tuple[MemoryHit, ...]:
    return tuple(_hit(rank) for rank in range(1, count + 1))


def _scoring_service(
    scores: dict[int, float],
    *,
    requests: list[dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    """Answer every batch with the scores of the ranks it asked about."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if requests is not None:
            requests.append(body)
        return httpx.Response(
            200,
            json={"scores": [{"rank": item["rank"], "score": scores[item["rank"]]} for item in body["candidates"]]},
        )

    return httpx.MockTransport(handler)


def test_batches_keep_the_original_rank_and_report_the_request_count() -> None:
    requests: list[dict[str, Any]] = []

    async def scenario() -> None:
        transport = _scoring_service(dict.fromkeys(range(1, 6), 0.5), requests=requests)
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=2)
            decision = await reranker.rerank("query", _candidates(5), 2)

        assert decision.usage.requests == 3
        assert decision.usage.input_tokens is None
        assert decision.used_fallback is False
        assert decision.discarded_rank_count == 0
        assert len(requests) == 3
        assert [len(body["candidates"]) for body in requests] == [2, 2, 1]
        assert sorted(item["rank"] for body in requests for item in body["candidates"]) == [1, 2, 3, 4, 5]

    asyncio.run(scenario())


def test_the_request_body_carries_the_query_and_the_original_candidate_text() -> None:
    requests: list[dict[str, Any]] = []

    async def scenario() -> None:
        transport = _scoring_service({1: 0.5, 2: 0.5}, requests=requests)
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=2)
            await reranker.rerank("database timeouts", _candidates(2), 1)

        assert requests == [
            {
                "query": "database timeouts",
                "candidates": [
                    {"rank": 1, "text": "Candidate 1"},
                    {"rank": 2, "text": "Candidate 2"},
                ],
            }
        ]

    asyncio.run(scenario())


def test_the_selection_spans_batches_and_breaks_ties_by_the_original_rank() -> None:
    """A later batch can win, and equal scores keep the earlier rank first."""

    async def scenario() -> None:
        transport = _scoring_service({1: 0.10, 2: 0.90, 3: 0.95, 4: 0.90})
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=2)
            decision = await reranker.rerank("query", _candidates(4), 2)

        assert decision.selected_ranks == (3, 2)
        assert decision.usage.requests == 2

    asyncio.run(scenario())


def test_out_of_order_scores_are_mapped_back_to_their_own_rank() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        scores = [{"rank": item["rank"], "score": 0.1 if item["rank"] == 1 else 0.8} for item in body["candidates"]]
        return httpx.Response(200, json={"scores": list(reversed(scores))})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=2)
            decision = await reranker.rerank("query", _candidates(2), 1)

        assert decision.selected_ranks == (2,)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"scores": [], "extra": True},
        {"scores": [{"rank": 1, "score": 0.5, "extra": True}]},
        {"scores": [{"rank": 1}]},
        {"scores": [{"score": 0.5}]},
        {"scores": [{"rank": 2, "score": 0.5}]},
        {"scores": [{"rank": 1, "score": 0.5}, {"rank": 1, "score": 0.4}]},
        {"scores": [{"rank": 1, "score": float("nan")}]},
        {"scores": [{"rank": 1, "score": float("inf")}]},
        {"scores": [{"rank": 1, "score": "0.5"}]},
        {"scores": [{"rank": 1, "score": True}]},
        {"scores": [{"rank": 1.0, "score": 0.5}]},
        {"scores": [{"rank": "1", "score": 0.5}]},
        {"scores": {"rank": 1, "score": 0.5}},
        {"scores": []},
        {"results": []},
        [],
    ],
)
def test_an_invalid_answer_fails_instead_of_ranking(payload: Any) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client)
            with pytest.raises(ValueError):
                await reranker.rerank("query", _candidates(1), 1)

    asyncio.run(scenario())


def test_an_invalid_json_body_fails_with_a_value_error() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"not json"))
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client)
            with pytest.raises(ValueError):
                await reranker.rerank("query", _candidates(1), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [301, 400, 404, 500, 503])
def test_a_non_2xx_answer_keeps_the_httpx_error(status: int) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(status, json={"scores": []}))
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client)
            with pytest.raises(httpx.HTTPStatusError):
                await reranker.rerank("query", _candidates(1), 1)

    asyncio.run(scenario())


def test_a_network_error_keeps_the_httpx_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client)
            with pytest.raises(httpx.ConnectError):
                await reranker.rerank("query", _candidates(1), 1)

    asyncio.run(scenario())


def test_invalid_arguments_fail_with_a_value_error() -> None:
    async def scenario() -> None:
        transport = _scoring_service({1: 0.5})
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client)
            with pytest.raises(ValueError):
                await reranker.rerank("query", (), 1)
            with pytest.raises(ValueError):
                await reranker.rerank("query", _candidates(1), 0)
            with pytest.raises(ValueError):
                await reranker.rerank("query", _candidates(1), 2)
            with pytest.raises(ValueError):
                HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=0)
            with pytest.raises(ValueError):
                HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=-1)

    asyncio.run(scenario())


def test_one_batch_is_enough_for_a_small_pool() -> None:
    async def scenario() -> None:
        transport = _scoring_service({1: 0.2, 2: 0.9, 3: 0.9})
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=32)
            decision = await reranker.rerank("query", _candidates(3), 2)

        assert decision.selected_ranks == (2, 3)
        assert decision.usage.requests == 1

    asyncio.run(scenario())


def test_a_pool_larger_than_one_batch_keeps_the_global_order() -> None:
    """The graded SciFact shape: 173 candidates, batches of seven, ten results."""

    count = 173
    scores = {rank: math.cos(rank / count) for rank in range(1, count + 1)}
    expected = tuple(sorted(scores, key=lambda rank: (-scores[rank], rank))[:10])

    async def scenario() -> None:
        transport = _scoring_service(scores)
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=7)
            decision = await reranker.rerank("query", _candidates(count), 10)

        assert decision.selected_ranks == expected
        assert decision.usage.requests == math.ceil(count / 7)

    asyncio.run(scenario())


def test_native_memory_search_keeps_the_selected_entry_identity() -> None:
    """The Runtime applies the decision and returns the original entry version."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"scores": [{"rank": item["rank"], "score": float(item["rank"])} for item in body["candidates"]]},
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=1)
            async with open_builtin_contexts(
                BuiltinConfig(database=SQLiteConfig()), memory_reranker=reranker
            ) as contexts:
                service = (await contexts.get("memory-reranking-native")).artifacts.memory
                memory = await service.remember(
                    memory=None,
                    entries=(
                        MemoryEntryInput(kind="fact", text="Project alpha fact."),
                        MemoryEntryInput(kind="fact", text="Project beta fact."),
                    ),
                    mode="append",
                )
                assert memory is not None
                result = await service.search("project", memories=(memory,), limit=1, mode="fts")

        assert result.rerank is not None
        assert result.rerank.policy_id == "http-cross-encoder-v1"
        assert result.rerank.selected_ranks == (2,)
        assert result.rerank.usage.requests == 2
        assert result.rerank.candidate_hits[-1].entry_version_id == result.hits[0].entry_version_id
        assert result.hits[0].entry_version_id == result.rerank.candidate_hits[-1].entry_version_id

    asyncio.run(scenario())


def test_native_memory_search_keeps_the_order_across_several_batches() -> None:
    """The graded shape: a pool larger than one batch, the best ranks first."""

    count = 8

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"scores": [{"rank": item["rank"], "score": float(item["rank"])} for item in body["candidates"]]},
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(_ENDPOINT, client=client, batch_size=7)
            async with open_builtin_contexts(
                BuiltinConfig(database=SQLiteConfig()), memory_reranker=reranker
            ) as contexts:
                service = (await contexts.get("memory-reranking-batches")).artifacts.memory
                memory = await service.remember(
                    memory=None,
                    entries=tuple(
                        MemoryEntryInput(kind="fact", text=f"Project fact number {index}.") for index in range(count)
                    ),
                    mode="append",
                )
                assert memory is not None
                result = await service.search("project", memories=(memory,), limit=3, mode="fts")

        assert result.rerank is not None
        assert result.rerank.usage.requests == 2
        assert result.rerank.selected_ranks == (8, 7, 6)
        candidates = result.rerank.candidate_hits
        assert len(candidates) == count
        assert [hit.entry_version_id for hit in result.hits] == [
            candidates[7].entry_version_id,
            candidates[6].entry_version_id,
            candidates[5].entry_version_id,
        ]
        assert [hit.entry_id for hit in result.hits] == [
            candidates[7].entry_id,
            candidates[6].entry_id,
            candidates[5].entry_id,
        ]

    asyncio.run(scenario())
