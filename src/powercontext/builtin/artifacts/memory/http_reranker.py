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

"""HTTP scoring-service reranking for coarse Memory search candidates."""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any, Final

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANKER_POLICY_ID: Final = "http-cross-encoder-v1"

__all__ = [
    "HTTP_RERANKER_POLICY_ID",
    "HTTPMemoryReranker",
]


class HTTPMemoryReranker:
    """Select Memory hits with a remote HTTP scoring service.

    The caller owns the ``httpx.AsyncClient`` and its lifetime. The adapter exchanges only
    the coarse rank and the candidate text, so every selected hit keeps the identity of the
    original Memory entry and entry version.

    The service bounds one request to ``batch_size`` candidates, so the pool is split into
    batches that keep the original one-based rank. Every batch joins one global selection:
    the highest scores win and equal scores are ordered by the original rank.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANKER_POLICY_ID,
    ) -> None:
        """Bind one scoring endpoint, the caller's client, and the per-request batch ceiling."""

        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("rerank batch size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate in bounded batches and keep the best ``limit`` ranks.

        The service may answer a batch in any order, so every score is mapped back through
        the rank it was asked about. The selection spans the whole pool: a high-scoring
        candidate from a later batch can outrank an earlier one.
        """

        _validate_bounds(len(candidates), limit)
        scores: dict[int, float] = {}
        requests = 0
        for batch in _batches(candidates, self._batch_size):
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [{"rank": rank, "text": candidate.text} for rank, candidate in batch],
                },
            )
            requests += 1
            response.raise_for_status()
            scores.update(_parse_scores(response, expected_ranks=frozenset(rank for rank, _ in batch)))
        ordered = sorted(scores, key=lambda rank: (-scores[rank], rank))
        return MemoryRerankDecision(
            selected_ranks=tuple(ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _batches(
    candidates: tuple[MemoryHit, ...],
    batch_size: int,
) -> Iterator[tuple[tuple[int, MemoryHit], ...]]:
    """Split the pool into batches that keep the original one-based rank.

    The rank is the position in the complete pool, never a batch-local counter: the
    service must see the same number for one candidate in every request.
    """

    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start : start + batch_size]
        yield tuple((start + offset + 1, candidate) for offset, candidate in enumerate(chunk))


def _parse_scores(response: httpx.Response, *, expected_ranks: frozenset[int]) -> dict[int, float]:
    """Validate one batch answer and map every score back to its original rank.

    The answer must carry exactly the ``scores`` field and every score record must carry
    exactly ``rank`` and ``score``. Anything else is a protocol error rather than a
    ranking hint.
    """

    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError("rerank response is not valid JSON") from error  # noqa: TRY003
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("rerank response must contain exactly the scores field")  # noqa: TRY003
    records = payload["scores"]
    if not isinstance(records, list):
        raise ValueError("rerank scores must be a JSON array")  # noqa: TRY003,TRY004
    return _parse_score_records(records, expected_ranks=expected_ranks)


def _parse_score_records(records: list[Any], *, expected_ranks: frozenset[int]) -> dict[int, float]:
    """Keep one finite score per asked rank and reject every other answer.

    The records may arrive in any order, but they must cover this batch's ranks exactly
    once: a missing, a repeated, or a foreign rank is a protocol error.
    """

    scores: dict[int, float] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"rank", "score"}:
            raise ValueError("each rerank score must contain exactly the rank and score fields")  # noqa: TRY003
        rank = record["rank"]
        if type(rank) is not int:
            raise ValueError("rerank rank must be an integer")  # noqa: TRY003
        if rank not in expected_ranks:
            raise ValueError("rerank response returned a rank outside the requested batch")  # noqa: TRY003
        if rank in scores:
            raise ValueError("rerank response returned a duplicate rank")  # noqa: TRY003
        score = record["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("rerank score must be a number")  # noqa: TRY003,TRY004
        value = float(score)
        if not math.isfinite(value):
            raise ValueError("rerank score must be a finite number")  # noqa: TRY003
        scores[rank] = value
    if scores.keys() != expected_ranks:
        raise ValueError("rerank response must cover every requested rank exactly once")  # noqa: TRY003
    return scores


def _validate_bounds(candidate_count: int, limit: int) -> None:
    """Reject an empty pool and a limit outside the pool."""

    if candidate_count < 1:
        raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("rerank limit must be an integer")  # noqa: TRY003,TRY004
    if limit < 1 or limit > candidate_count:
        raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003
