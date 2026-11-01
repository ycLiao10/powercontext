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

"""HTTP reranking adapter for coarse Memory search candidates.

The adapter speaks one offline scoring protocol: a request carries the query and a bounded
candidate batch, and the response carries one score per requested rank. Every candidate
keeps the 1-based rank it holds in the complete coarse pool, so a batched request still
identifies the original Memory entry it scored.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision, validate_rerank_bounds
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"
_RESPONSE_KEYS = frozenset({"scores"})
_SCORE_KEYS = frozenset({"rank", "score"})


class HTTPMemoryReranker:
    """Select Memory hits with an external HTTP rerank service."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANK_POLICY_ID,
    ) -> None:
        """Bind one scoring endpoint and the batch ceiling every request respects."""

        if batch_size < 1:
            raise ValueError("rerank batch size must be positive")  # noqa: TRY003
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
        """Score the coarse pool in batches and select the globally best ranks.

        Batches are sent one after another, so the reported request count is exactly the
        number of requests issued, and the selection is taken over the scores of the whole
        pool instead of per batch. A candidate's rank is its 1-based position in
        ``candidates`` and never changes when the pool is split.
        """

        validate_rerank_bounds(len(candidates), limit)
        scores: dict[int, float] = {}
        requests = 0
        for batch in _batches(candidates, self._batch_size):
            response = await self._client.post(self._endpoint, json=_request_body(query, batch))
            response.raise_for_status()
            scores.update(_batch_scores(response, batch))
            requests += 1
        selected_ranks = tuple(rank for rank, _ in _ranks_by_score(scores)[:limit])
        return MemoryRerankDecision(
            selected_ranks=selected_ranks,
            usage=InferenceUsage(requests=requests),
            discarded_rank_count=len(candidates) - len(selected_ranks),
        )


def _batches(
    candidates: tuple[MemoryHit, ...],
    batch_size: int,
) -> Iterator[tuple[tuple[int, MemoryHit], ...]]:
    """Split the coarse pool into batches that keep the original rank of every candidate."""

    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start : start + batch_size]
        yield tuple((start + offset + 1, candidate) for offset, candidate in enumerate(chunk))


def _request_body(query: str, batch: tuple[tuple[int, MemoryHit], ...]) -> dict[str, Any]:
    """Render one request body for one batch of original ranks."""

    return {
        "query": query,
        "candidates": [{"rank": rank, "text": candidate.text} for rank, candidate in batch],
    }


def _response_json(response: httpx.Response) -> Any:
    """Read one JSON response body, reporting a malformed body as invalid output."""

    try:
        return response.json()
    except ValueError as error:
        raise ValueError("rerank service response was not valid JSON") from error


def _batch_scores(response: httpx.Response, batch: tuple[tuple[int, MemoryHit], ...]) -> dict[int, float]:
    """Validate one batch response and return the score of every rank it covered."""

    scores = _parse_scores(_response_json(response))
    if set(scores) != {rank for rank, _ in batch}:
        raise ValueError("rerank service response must cover every requested rank exactly once")  # noqa: TRY003
    return scores


def _parse_scores(payload: Any) -> dict[int, float]:
    """Validate the response schema and return its scores keyed by rank."""

    if not isinstance(payload, Mapping):
        raise ValueError("rerank service response must be a JSON object")  # noqa: TRY003
    if set(payload) != _RESPONSE_KEYS:
        raise ValueError("rerank service response must contain exactly the 'scores' key")  # noqa: TRY003
    raw_scores = payload["scores"]
    if not isinstance(raw_scores, Sequence) or isinstance(raw_scores, (str, bytes)):
        raise ValueError("rerank service 'scores' must be a list")  # noqa: TRY003
    scores: dict[int, float] = {}
    for item in raw_scores:
        if not isinstance(item, Mapping):
            raise ValueError("each rerank score must be a JSON object")  # noqa: TRY003
        if set(item) != _SCORE_KEYS:
            raise ValueError("each rerank score must contain exactly the 'rank' and 'score' keys")  # noqa: TRY003
        rank = item["rank"]
        if isinstance(rank, bool) or not isinstance(rank, int):
            raise ValueError("rerank score 'rank' must be an integer")  # noqa: TRY003
        score = item["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("rerank score 'score' must be a number")  # noqa: TRY003
        if not math.isfinite(score):
            raise ValueError("rerank score 'score' must be finite")  # noqa: TRY003
        if rank in scores:
            raise ValueError("rerank service response must not repeat a rank")  # noqa: TRY003
        scores[rank] = float(score)
    return scores


def _ranks_by_score(scores: Mapping[int, float]) -> list[tuple[int, float]]:
    """Order ranks by descending score, breaking a tie on the original ascending rank."""

    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


__all__ = [
    "HTTP_RERANK_POLICY_ID",
    "HTTPMemoryReranker",
]
