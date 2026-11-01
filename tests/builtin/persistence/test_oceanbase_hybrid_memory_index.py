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

"""The one-statement hybrid candidate query over the OceanBase projections."""

from __future__ import annotations

import asyncio
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemorySearchRequest
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex
from powercontext.builtin.persistence.oceanbase.memory_index import (
    _OCEANBASE_VECTOR_SEARCH_SQL,
    OceanBaseHybridMemoryIndex,
    OceanBaseMemoryFTSIndex,
    OceanBaseMemoryVectorIndex,
    _hybrid_search_sql,
)
from powercontext.builtin.persistence.tags import memory_tag_sql
from powercontext.builtin.tags import TagFilter

_MEMORY = ArtifactRef(family="memory", artifact_id="memory-a", revision=3)


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="hybrid-test-v1",
        model="test",
        dimension=3,
        distance="l2",
        normalization="unit",
    )


def _index() -> tuple[OceanBaseHybridMemoryIndex, OceanBaseMemoryVectorIndex]:
    vector_index = OceanBaseMemoryVectorIndex(_profile())
    return OceanBaseHybridMemoryIndex(OceanBaseMemoryFTSIndex(), vector_index), vector_index


def _request(mode: Literal["fts", "vector", "hybrid"], *, tag_filter: TagFilter | None = None) -> MemorySearchRequest:
    return MemorySearchRequest(
        query="alpha",
        analyzed_query="alpha",
        memories=(_MEMORY,),
        candidate_limit=32,
        mode=mode,
        query_vector=(1.0, 0.0, 0.0),
        embedding_profile=_profile(),
        tag_filter=tag_filter,
    )


def _connection(rows: list[dict[str, Any]]) -> AsyncMock:
    connection = AsyncMock(spec=AsyncConnection)
    connection.execute.return_value.mappings = MagicMock(return_value=rows)
    return connection


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=mysql.dialect()))


def test_hybrid_statement_keeps_both_native_access_paths_in_one_statement() -> None:
    sql = _hybrid_search_sql(tagged=False)

    assert "pc_memory_entry_heads" in sql
    assert "pc_memory_vector_entries" in sql
    assert "AGAINST" in sql
    assert "l2_distance" in sql
    assert "UNION ALL" in sql
    assert "'fts' AS channel" in sql
    assert "'vector' AS channel" in sql
    # Each branch keeps the candidate limit of the statement it replaces.
    assert sql.count("LIMIT :candidate_limit") == 2


def test_hybrid_lexical_branch_keeps_the_frozen_fulltext_shape() -> None:
    sql = _hybrid_search_sql(tagged=False)

    assert "FROM pc_memory_entry_heads AS f" in sql
    assert "JOIN pc_memory_entry_versions AS v" in sql
    assert "WHERE f.scope_id = :scope_id" in sql
    assert "AND f.memory_artifact_id IN :memory_artifact_ids" in sql
    assert "AND MATCH (f.searchable_text) AGAINST (:analyzed_query)" in sql
    assert "ORDER BY MATCH (f.searchable_text) AGAINST (:analyzed_query) DESC" in sql
    assert "f.memory_artifact_id,\n           f.entry_id,\n           f.entry_version_id" in sql


def test_hybrid_semantic_branch_keeps_the_frozen_vector_statement() -> None:
    frozen = _OCEANBASE_VECTOR_SEARCH_SQL
    hybrid = _hybrid_search_sql(tagged=False)

    for fragment in (
        "FROM pc_memory_vector_entries AS m",
        "JOIN pc_memory_entry_versions AS v",
        "WHERE m.scope_id = :scope_id",
        "AND m.memory_artifact_id IN :memory_artifact_ids",
        "ORDER BY l2_distance(m.embedding, :query_vector) APPROXIMATE",
        "LIMIT :candidate_limit",
    ):
        assert fragment in frozen
        assert fragment in hybrid


def test_tagged_hybrid_statement_filters_both_branches_before_ordering() -> None:
    sql = _hybrid_search_sql(tagged=True)
    lexical, semantic = sql.split("UNION ALL")

    # A tag pre-filter cannot narrow an approximate index scan, so the tagged
    # variant drops APPROXIMATE exactly like the frozen tagged vector query.
    assert "APPROXIMATE" not in sql
    assert memory_tag_sql("f") in lexical
    assert memory_tag_sql("m") in semantic
    assert lexical.index(memory_tag_sql("f")) < lexical.index("ORDER BY")
    assert semantic.index(memory_tag_sql("m")) < semantic.index("ORDER BY")


def test_hybrid_search_issues_one_statement_and_splits_both_channels() -> None:
    async def scenario() -> None:
        index, vector_index = _index()
        rows = [
            {
                "memory_artifact_id": "memory-a",
                "head_revision": 3,
                "entry_id": "e1",
                "entry_version_id": "v1",
                "text": "Alpha one",
                "distance": None,
                "channel": "fts",
            },
            {
                "memory_artifact_id": "memory-a",
                "head_revision": 3,
                "entry_id": "e2",
                "entry_version_id": "v2",
                "text": "Alpha two",
                "distance": None,
                "channel": "fts",
            },
            {
                "memory_artifact_id": "memory-a",
                "head_revision": 3,
                "entry_id": "e1",
                "entry_version_id": "v1",
                "text": "Alpha one",
                "distance": 0.0,
                "channel": "vector",
            },
        ]
        connection = _connection(rows)
        with patch.object(vector_index, "vector_complete", AsyncMock(return_value=True)):
            channels = await index.search(
                cast(AsyncConnection, connection),
                "scope-a",
                _request("hybrid"),
            )

        assert connection.execute.await_count == 1
        statement, parameters = connection.execute.await_args.args
        sql = _sql(statement)
        assert "pc_memory_entry_heads" in sql
        assert "pc_memory_vector_entries" in sql
        assert "AGAINST" in sql
        assert "l2_distance" in sql
        assert parameters["scope_id"] == "scope-a"
        assert parameters["memory_artifact_ids"] == ("memory-a",)
        assert parameters["analyzed_query"] == "alpha"
        assert parameters["candidate_limit"] == 32

        assert [hit.entry_id for hit in channels.fts] == ["e1", "e2"]
        assert [hit.entry_id for hit in channels.vector] == ["e1"]
        assert channels.fts[0].distance is None
        assert channels.vector[0].distance == 0.0
        assert channels.fts[0].memory_ref == _MEMORY
        assert channels.vector[0].memory_ref == _MEMORY

    asyncio.run(scenario())


def test_tagged_hybrid_search_binds_the_tag_parameters() -> None:
    async def scenario() -> None:
        index, vector_index = _index()
        connection = _connection([])
        with patch.object(vector_index, "vector_complete", AsyncMock(return_value=True)):
            await index.search(
                cast(AsyncConnection, connection),
                "scope-a",
                _request("hybrid", tag_filter=TagFilter(tags=("selected",))),
            )

        statement, parameters = connection.execute.await_args.args
        sql = _sql(statement)
        assert "pc_artifact_tags" in sql
        assert parameters["tag_keys"] == ("selected",)
        assert parameters["tag_minimum"] == 1
        assert len(parameters["tag_hashes"]) == 1

    asyncio.run(scenario())


def test_single_channel_modes_delegate_to_the_unchanged_indexes() -> None:
    async def scenario() -> None:
        index, vector_index = _index()
        connection = _connection([])
        with patch.object(vector_index, "vector_complete", AsyncMock(return_value=True)):
            lexical = await index.search(
                cast(AsyncConnection, connection),
                "scope-a",
                _request("fts"),
            )
            semantic = await index.search(
                cast(AsyncConnection, connection),
                "scope-a",
                _request("vector"),
            )

        statements = [_sql(call.args[0]) for call in connection.execute.await_args_list]
        assert len(statements) == 2
        assert "AGAINST" in statements[0]
        assert "pc_memory_vector_entries" not in statements[0]
        assert "l2_distance" in statements[1]
        assert "pc_memory_entry_heads" not in statements[1]
        assert lexical.fts == ()
        assert lexical.vector == ()
        assert semantic.vector == ()
        assert semantic.fts == ()

    asyncio.run(scenario())


def test_hybrid_capabilities_match_the_composite_aggregation() -> None:
    fts_index = OceanBaseMemoryFTSIndex()
    vector_index = OceanBaseMemoryVectorIndex(_profile())

    hybrid = OceanBaseHybridMemoryIndex(fts_index, vector_index)
    composite = CompositeMemoryIndex(fts_index, vector_index)

    assert hybrid.capabilities == composite.capabilities
    assert hybrid.capabilities.hybrid is True
    assert hybrid.tables == composite.tables


def test_hybrid_without_a_vector_projection_answers_with_the_lexical_channel() -> None:
    async def scenario() -> None:
        index = OceanBaseHybridMemoryIndex(OceanBaseMemoryFTSIndex(), None)
        connection = _connection([])
        channels = await index.search(
            cast(AsyncConnection, connection),
            "scope-a",
            _request("hybrid"),
        )

        statements = [_sql(call.args[0]) for call in connection.execute.await_args_list]
        assert len(statements) == 1
        assert "AGAINST" in statements[0]
        assert "pc_memory_vector_entries" not in statements[0]
        assert channels.vector == ()

    asyncio.run(scenario())
