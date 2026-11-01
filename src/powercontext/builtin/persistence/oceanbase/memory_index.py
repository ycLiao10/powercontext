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

"""OceanBase Memory search indexes using FULLTEXT and vector indexes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pyobvector import VECTOR, VectorIndex
from sqlalchemy import (
    BigInteger,
    Column,
    MetaData,
    Table,
    UniqueConstraint,
    bindparam,
    delete,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.mysql import match
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemoryCapabilities,
    MemoryProjection,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.artifacts.memory.canonical import embedding_content_hash, validate_embedding
from powercontext.builtin.persistence.memory_index import memory_channel_hits
from powercontext.builtin.persistence.tables import (
    ARTIFACT_HEADS_TABLE,
    MAX_MEMORY_ENTRY_ID_LENGTH,
    MAX_MEMORY_HASH_LENGTH,
    MEMORY_ENTRY_HEADS_TABLE,
    MEMORY_ENTRY_VERSIONS_TABLE,
    identity_string,
)
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql, tag_predicate
from powercontext.limits import MAX_ARTIFACT_ID_LENGTH, MAX_SCOPE_ID_LENGTH

_OCEANBASE_FTS_INDEX_NAME = "ix_pc_memory_entry_heads_fts"
_OCEANBASE_CREATE_FTS_SQL = f"""
CREATE FULLTEXT INDEX {_OCEANBASE_FTS_INDEX_NAME}
ON pc_memory_entry_heads (searchable_text) WITH PARSER SPACE
"""
_OCEANBASE_FTS_INDEX_EXISTS_SQL = text(
    """
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'pc_memory_entry_heads'
      AND index_name = :index_name
    """
)
_OCEANBASE_VECTOR_TABLE_NAME = "pc_memory_vector_entries"
_OCEANBASE_VECTOR_INDEX_NAME = "ix_pc_memory_vector_entries_embedding"
_OCEANBASE_VECTOR_TYPE_SQL = text(
    """
    SELECT data_type
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = :table_name
      AND column_name = 'embedding'
    """
)
_OCEANBASE_VECTOR_SEARCH_SQL = """
SELECT
    m.memory_artifact_id,
    m.head_revision,
    m.entry_id,
    m.entry_version_id,
    v.text,
    l2_distance(m.embedding, :query_vector) AS distance
FROM pc_memory_vector_entries AS m
JOIN pc_memory_entry_versions AS v
  ON v.scope_id = m.scope_id
 AND v.memory_artifact_id = m.memory_artifact_id
 AND v.entry_version_id = m.entry_version_id
WHERE m.scope_id = :scope_id
  AND m.memory_artifact_id IN :memory_artifact_ids
ORDER BY l2_distance(m.embedding, :query_vector) APPROXIMATE
LIMIT :candidate_limit
"""
# One hybrid candidate statement: the lexical branch keeps the relational
# FULLTEXT access path and the semantic branch keeps the HNSW vector access
# path, both inside the same statement. Each branch keeps the filtering, the
# ordering and the candidate limit of the statement it replaces, so the shared
# fusion still sees the same channel rankings.
_OCEANBASE_HYBRID_SEARCH_SQL = """
(
  SELECT
    f.memory_artifact_id AS memory_artifact_id,
    f.head_revision AS head_revision,
    f.entry_id AS entry_id,
    f.entry_version_id AS entry_version_id,
    v.text AS text,
    NULL AS distance,
    'fts' AS channel
  FROM pc_memory_entry_heads AS f
  JOIN pc_memory_entry_versions AS v
    ON v.scope_id = f.scope_id
   AND v.memory_artifact_id = f.memory_artifact_id
   AND v.entry_version_id = f.entry_version_id
  WHERE f.scope_id = :scope_id
    AND f.memory_artifact_id IN :memory_artifact_ids
    AND MATCH (f.searchable_text) AGAINST (:analyzed_query)
__FTS_TAG__
  ORDER BY MATCH (f.searchable_text) AGAINST (:analyzed_query) DESC,
           f.memory_artifact_id,
           f.entry_id,
           f.entry_version_id
  LIMIT :candidate_limit
)
UNION ALL
(
  SELECT
    m.memory_artifact_id AS memory_artifact_id,
    m.head_revision AS head_revision,
    m.entry_id AS entry_id,
    m.entry_version_id AS entry_version_id,
    v.text AS text,
    l2_distance(m.embedding, :query_vector) AS distance,
    'vector' AS channel
  FROM pc_memory_vector_entries AS m
  JOIN pc_memory_entry_versions AS v
    ON v.scope_id = m.scope_id
   AND v.memory_artifact_id = m.memory_artifact_id
   AND v.entry_version_id = m.entry_version_id
  WHERE m.scope_id = :scope_id
    AND m.memory_artifact_id IN :memory_artifact_ids
__VECTOR_TAG__
  ORDER BY l2_distance(m.embedding, :query_vector) __VECTOR_ORDER__
  LIMIT :candidate_limit
)
"""


def _hybrid_search_sql(*, tagged: bool) -> str:
    """Render the one-statement hybrid candidate query.

    The tagged variant mirrors the frozen tagged vector query: the tag
    predicate is inserted before the branch ordering and the approximate
    vector ordering is dropped, because a tag pre-filter cannot narrow an
    approximate index scan.
    """

    sql = _OCEANBASE_HYBRID_SEARCH_SQL
    sql = sql.replace("__FTS_TAG__", memory_tag_sql("f") if tagged else "")
    sql = sql.replace("__VECTOR_TAG__", memory_tag_sql("m") if tagged else "")
    sql = sql.replace("__VECTOR_ORDER__", "" if tagged else "APPROXIMATE")
    return sql


class OceanBaseMemoryFTSIndex:
    """OceanBase FULLTEXT strategy over the relational active-head projection."""

    capabilities = MemoryCapabilities(fts=True, tag_filter=True)
    tables: tuple[Table, ...] = ()

    async def initialize(self, connection: AsyncConnection, /) -> None:
        if connection.dialect.name != "mysql":
            raise CapabilityNotSupportedError("oceanbase-fts")
        count = await connection.scalar(
            _OCEANBASE_FTS_INDEX_EXISTS_SQL,
            {"index_name": _OCEANBASE_FTS_INDEX_NAME},
        )
        if count == 0:
            await connection.exec_driver_sql(_OCEANBASE_CREATE_FTS_SQL)
        probe = match(MEMORY_ENTRY_HEADS_TABLE.c.searchable_text, against="powercontext")
        await connection.execute(select(MEMORY_ENTRY_HEADS_TABLE.c.entry_version_id).where(probe).limit(1))

    async def replace(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        del connection, scope_id, memory_ref, projections

    async def delete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        entry_ids: tuple[str, ...],
        /,
    ) -> None:
        del connection, scope_id, memory_ref, entry_ids

    async def upsert(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        del connection, scope_id, memory_ref, projections

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode not in {"fts", "hybrid"}:
            return MemorySearchChannels()
        score = match(MEMORY_ENTRY_HEADS_TABLE.c.searchable_text, against=request.analyzed_query)
        rows = (
            await connection.execute(
                select(
                    MEMORY_ENTRY_HEADS_TABLE.c.memory_artifact_id,
                    MEMORY_ENTRY_HEADS_TABLE.c.head_revision,
                    MEMORY_ENTRY_HEADS_TABLE.c.entry_id,
                    MEMORY_ENTRY_HEADS_TABLE.c.entry_version_id,
                    MEMORY_ENTRY_VERSIONS_TABLE.c.text,
                )
                .join(
                    MEMORY_ENTRY_VERSIONS_TABLE,
                    (MEMORY_ENTRY_VERSIONS_TABLE.c.scope_id == MEMORY_ENTRY_HEADS_TABLE.c.scope_id)
                    & (
                        MEMORY_ENTRY_VERSIONS_TABLE.c.memory_artifact_id
                        == MEMORY_ENTRY_HEADS_TABLE.c.memory_artifact_id
                    )
                    & (MEMORY_ENTRY_VERSIONS_TABLE.c.entry_version_id == MEMORY_ENTRY_HEADS_TABLE.c.entry_version_id),
                )
                .where(
                    MEMORY_ENTRY_HEADS_TABLE.c.scope_id == scope_id,
                    MEMORY_ENTRY_HEADS_TABLE.c.memory_artifact_id.in_(
                        tuple(memory.artifact_id for memory in request.memories)
                    ),
                    score,
                    *(
                        ()
                        if request.tag_filter is None
                        else (
                            tag_predicate(
                                scope_id,
                                "memory",
                                MEMORY_ENTRY_HEADS_TABLE.c.memory_artifact_id,
                                "memory_entry",
                                MEMORY_ENTRY_HEADS_TABLE.c.entry_id,
                                request.tag_filter,
                            ),
                        )
                    ),
                )
                .order_by(
                    score.desc(),
                    MEMORY_ENTRY_HEADS_TABLE.c.memory_artifact_id,
                    MEMORY_ENTRY_HEADS_TABLE.c.entry_id,
                    MEMORY_ENTRY_HEADS_TABLE.c.entry_version_id,
                )
                .limit(request.candidate_limit)
            )
        ).mappings()
        return MemorySearchChannels(fts=memory_channel_hits(rows, request.memories))

    async def vector_complete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memories: tuple[ArtifactRef, ...],
        profile: EmbeddingProfile,
        /,
    ) -> bool:
        del connection, scope_id, memories, profile
        return False

    async def hydrate(
        self,
        connection: AsyncConnection,
        scope_id: str,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> tuple[MemoryProjection, ...]:
        del connection, scope_id
        return projections


class OceanBaseMemoryVectorIndex:
    """OceanBase HNSW strategy over rebuildable active-head embeddings."""

    def __init__(self, profile: EmbeddingProfile) -> None:
        if profile.dimension < 1 or profile.distance != "l2" or profile.normalization != "unit":
            raise CapabilityNotSupportedError(
                "vector",
                "OceanBase requires a positive unit-normalized L2 embedding profile",
            )
        self.profile = profile
        self.capabilities = MemoryCapabilities(
            tag_filter=True,
            fts=False,
            vector=True,
            embedding_profile=profile,
        )
        metadata = MetaData()
        self.table = Table(
            _OCEANBASE_VECTOR_TABLE_NAME,
            metadata,
            Column("vector_id", BigInteger, primary_key=True, autoincrement=True),
            Column("scope_id", identity_string(MAX_SCOPE_ID_LENGTH), nullable=False),
            Column("memory_artifact_id", identity_string(MAX_ARTIFACT_ID_LENGTH), nullable=False),
            Column("head_revision", BigInteger, nullable=False),
            Column("entry_id", identity_string(MAX_MEMORY_ENTRY_ID_LENGTH), nullable=False),
            Column("entry_version_id", identity_string(MAX_MEMORY_ENTRY_ID_LENGTH), nullable=False),
            Column("entry_content_hash", identity_string(MAX_MEMORY_HASH_LENGTH), nullable=False),
            Column("embedding_content_hash", identity_string(MAX_MEMORY_HASH_LENGTH), nullable=False),
            Column("embedding", VECTOR(profile.dimension), nullable=False),
            UniqueConstraint(
                "scope_id",
                "memory_artifact_id",
                "entry_id",
                name="uq_pc_memory_vector_entries_head",
            ),
        )
        self.tables: tuple[Table, ...] = (self.table,)
        self._vector_index = VectorIndex(
            _OCEANBASE_VECTOR_INDEX_NAME,
            self.table.c.embedding,
            params="distance=l2,type=hnsw",
        )
        self._search_sql = text(_OCEANBASE_VECTOR_SEARCH_SQL).bindparams(
            bindparam("memory_artifact_ids", expanding=True),
            bindparam("query_vector", type_=VECTOR(profile.dimension)),
        )
        self._tagged_search_sql = text(
            _OCEANBASE_VECTOR_SEARCH_SQL.replace("ORDER BY", memory_tag_sql("m") + "ORDER BY").replace(
                " APPROXIMATE", ""
            )
        ).bindparams(
            bindparam("memory_artifact_ids", expanding=True),
            bindparam("tag_keys", expanding=True),
            bindparam("tag_hashes", expanding=True),
            bindparam("query_vector", type_=VECTOR(profile.dimension)),
        )

    async def initialize(self, connection: AsyncConnection, /) -> None:
        if connection.dialect.name != "mysql":
            raise CapabilityNotSupportedError("oceanbase-vector")
        column_type = await connection.scalar(
            _OCEANBASE_VECTOR_TYPE_SQL,
            {"table_name": _OCEANBASE_VECTOR_TABLE_NAME},
        )
        expected = f"VECTOR({self.profile.dimension})"
        if str(column_type).upper() != expected:
            raise CapabilityNotSupportedError(
                "vector",
                f"OceanBase projection uses {column_type!r}; expected {expected}",
            )
        await connection.run_sync(lambda sync_connection: self._vector_index.create(sync_connection, checkfirst=True))

    async def replace(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        await connection.execute(
            delete(self.table).where(
                self.table.c.scope_id == scope_id,
                self.table.c.memory_artifact_id == memory_ref.artifact_id,
            )
        )
        await self.upsert(connection, scope_id, memory_ref, projections)

    async def delete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        entry_ids: tuple[str, ...],
        /,
    ) -> None:
        if not entry_ids:
            return
        await connection.execute(
            delete(self.table).where(
                self.table.c.scope_id == scope_id,
                self.table.c.memory_artifact_id == memory_ref.artifact_id,
                self.table.c.entry_id.in_(entry_ids),
            )
        )

    async def upsert(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        values = []
        for projection in projections:
            if projection.embedding is None or projection.embedding_content_hash is None:
                continue
            entry = projection.entry_version
            values.append({
                "scope_id": scope_id,
                "memory_artifact_id": memory_ref.artifact_id,
                "head_revision": memory_ref.revision,
                "entry_id": entry.entry_id,
                "entry_version_id": entry.entry_version_id,
                "entry_content_hash": entry.entry_content_hash,
                "embedding_content_hash": projection.embedding_content_hash,
                "embedding": validate_embedding(
                    projection.embedding,
                    dimension=self.profile.dimension,
                ),
            })
        if values:
            await connection.execute(insert(self.table), values)

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode not in {"vector", "hybrid"}:
            return MemorySearchChannels()
        if request.embedding_profile != self.profile or request.query_vector is None:
            raise CapabilityNotSupportedError("embedding-profile")
        if not await self.vector_complete(connection, scope_id, request.memories, self.profile):
            raise CapabilityNotSupportedError("vector")
        rows = (
            await connection.execute(
                self._search_sql if request.tag_filter is None else self._tagged_search_sql,
                {
                    "scope_id": scope_id,
                    "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
                    "query_vector": validate_embedding(
                        request.query_vector,
                        dimension=self.profile.dimension,
                    ),
                    "candidate_limit": request.candidate_limit,
                    **memory_tag_parameters(request.tag_filter),
                },
            )
        ).mappings()
        return MemorySearchChannels(vector=memory_channel_hits(rows, request.memories))

    async def vector_complete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memories: tuple[ArtifactRef, ...],
        profile: EmbeddingProfile,
        /,
    ) -> bool:
        if profile != self.profile:
            return False
        for memory in memories:
            current = await connection.scalar(
                select(ARTIFACT_HEADS_TABLE.c.revision).where(
                    ARTIFACT_HEADS_TABLE.c.scope_id == scope_id,
                    ARTIFACT_HEADS_TABLE.c.family == "memory",
                    ARTIFACT_HEADS_TABLE.c.artifact_id == memory.artifact_id,
                )
            )
            if current is None or int(current) != memory.revision:
                # The caller validated this head before the search. If it moved while
                # this query ran, report a stale head so the runtime can retry with it.
                raise CapabilityNotSupportedError("head")
            heads = (
                await connection.execute(
                    select(
                        MEMORY_ENTRY_HEADS_TABLE.c.entry_id,
                        MEMORY_ENTRY_HEADS_TABLE.c.entry_version_id,
                        MEMORY_ENTRY_HEADS_TABLE.c.entry_content_hash,
                    ).where(
                        MEMORY_ENTRY_HEADS_TABLE.c.scope_id == scope_id,
                        MEMORY_ENTRY_HEADS_TABLE.c.memory_artifact_id == memory.artifact_id,
                    )
                )
            ).all()
            metadata = (
                await connection.execute(
                    select(
                        self.table.c.entry_id,
                        self.table.c.entry_version_id,
                        self.table.c.entry_content_hash,
                        self.table.c.embedding_content_hash,
                    ).where(
                        self.table.c.scope_id == scope_id,
                        self.table.c.memory_artifact_id == memory.artifact_id,
                    )
                )
            ).all()
            expected = {(str(row[0]), str(row[1]), str(row[2])) for row in heads}
            actual = {(str(row[0]), str(row[1]), str(row[2])) for row in metadata}
            if actual != expected:
                return False
            if any(str(row[3]) != _embedding_hash(self.profile, str(row[2])) for row in metadata):
                return False
        return True

    async def hydrate(
        self,
        connection: AsyncConnection,
        scope_id: str,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> tuple[MemoryProjection, ...]:
        hydrated: list[MemoryProjection] = []
        for projection in projections:
            entry = projection.entry_version
            row = (
                await connection.execute(
                    select(
                        self.table.c.entry_content_hash,
                        self.table.c.embedding_content_hash,
                        self.table.c.embedding,
                    ).where(
                        self.table.c.scope_id == scope_id,
                        self.table.c.memory_artifact_id == entry.memory_artifact_id,
                        self.table.c.entry_id == entry.entry_id,
                        self.table.c.entry_version_id == entry.entry_version_id,
                    )
                )
            ).one_or_none()
            if (
                row is None
                or str(row[0]) != entry.entry_content_hash
                or str(row[1]) != _embedding_hash(self.profile, entry.entry_content_hash)
            ):
                hydrated.append(projection)
                continue
            embedding = validate_embedding(
                tuple(float(value) for value in row[2]),
                dimension=self.profile.dimension,
            )
            hydrated.append(
                MemoryProjection(
                    entry_version=entry,
                    searchable_text=projection.searchable_text,
                    embedding=embedding,
                    embedding_content_hash=str(row[1]),
                )
            )
        return tuple(hydrated)


class OceanBaseHybridMemoryIndex:
    """Answer one hybrid candidate query with a single statement.

    The two channels keep their own native access paths — the relational
    FULLTEXT index for lexical candidates and the HNSW vector index for
    semantic ones — but they now travel in one statement, so a hybrid
    retrieval costs one candidate query instead of two. Each branch keeps the
    filtering, the ordering and the candidate limit of the statement it
    replaces, and the returned rows carry the channel they came from, so the
    shared fusion still sees exactly the channel rankings it saw before.

    Single-channel retrieval is delegated to the unchanged indexes.
    """

    def __init__(
        self,
        fts_index: OceanBaseMemoryFTSIndex,
        vector_index: OceanBaseMemoryVectorIndex | None,
    ) -> None:
        self._fts = fts_index
        self._vector = vector_index
        vector_capabilities = vector_index.capabilities if vector_index is not None else None
        self.capabilities = MemoryCapabilities(
            fts=fts_index.capabilities.fts,
            vector=bool(vector_capabilities and vector_capabilities.vector),
            hybrid=bool(fts_index.capabilities.fts and vector_capabilities and vector_capabilities.vector),
            tag_filter=fts_index.capabilities.tag_filter
            and (vector_capabilities is None or vector_capabilities.tag_filter),
            embedding_profile=None if vector_capabilities is None else vector_capabilities.embedding_profile,
        )
        self.tables = fts_index.tables + (() if vector_index is None else vector_index.tables)
        self._hybrid_sql = None
        self._tagged_hybrid_sql = None
        if vector_index is not None:
            dimension = vector_index.profile.dimension
            self._hybrid_sql = text(_hybrid_search_sql(tagged=False)).bindparams(
                bindparam("memory_artifact_ids", expanding=True),
                bindparam("query_vector", type_=VECTOR(dimension)),
            )
            self._tagged_hybrid_sql = text(_hybrid_search_sql(tagged=True)).bindparams(
                bindparam("memory_artifact_ids", expanding=True),
                bindparam("tag_keys", expanding=True),
                bindparam("tag_hashes", expanding=True),
                bindparam("query_vector", type_=VECTOR(dimension)),
            )

    async def initialize(self, connection: AsyncConnection, /) -> None:
        await self._fts.initialize(connection)
        if self._vector is not None:
            await self._vector.initialize(connection)

    async def replace(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        await self._fts.replace(connection, scope_id, memory_ref, projections)
        if self._vector is not None:
            await self._vector.replace(connection, scope_id, memory_ref, projections)

    async def delete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        entry_ids: tuple[str, ...],
        /,
    ) -> None:
        await self._fts.delete(connection, scope_id, memory_ref, entry_ids)
        if self._vector is not None:
            await self._vector.delete(connection, scope_id, memory_ref, entry_ids)

    async def upsert(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        await self._fts.upsert(connection, scope_id, memory_ref, projections)
        if self._vector is not None:
            await self._vector.upsert(connection, scope_id, memory_ref, projections)

    async def vector_complete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memories: tuple[ArtifactRef, ...],
        profile: EmbeddingProfile,
        /,
    ) -> bool:
        if self._vector is None:
            return False
        return await self._vector.vector_complete(connection, scope_id, memories, profile)

    async def hydrate(
        self,
        connection: AsyncConnection,
        scope_id: str,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> tuple[MemoryProjection, ...]:
        hydrated = await self._fts.hydrate(connection, scope_id, projections)
        if self._vector is not None:
            hydrated = await self._vector.hydrate(connection, scope_id, hydrated)
        return hydrated

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode == "fts":
            return await self._fts.search(connection, scope_id, request)
        if request.mode == "vector":
            if self._vector is None:
                return MemorySearchChannels()
            return await self._vector.search(connection, scope_id, request)
        if self._vector is None:
            # Without a vector projection the lexical channel is the whole
            # answer, exactly as the composite index reported it.
            return await self._fts.search(connection, scope_id, request)
        return await self._hybrid_search(connection, scope_id, request)

    async def _hybrid_search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        """Return both channels' candidates from one statement."""

        if self._vector is None or self._hybrid_sql is None:
            return MemorySearchChannels()
        if request.embedding_profile != self._vector.profile or request.query_vector is None:
            raise CapabilityNotSupportedError("embedding-profile")
        if not await self._vector.vector_complete(connection, scope_id, request.memories, self._vector.profile):
            raise CapabilityNotSupportedError("vector")
        statement = self._hybrid_sql if request.tag_filter is None else self._tagged_hybrid_sql
        rows = (
            await connection.execute(
                statement,
                {
                    "scope_id": scope_id,
                    "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
                    "analyzed_query": request.analyzed_query,
                    "query_vector": validate_embedding(
                        request.query_vector,
                        dimension=self._vector.profile.dimension,
                    ),
                    "candidate_limit": request.candidate_limit,
                    **memory_tag_parameters(request.tag_filter),
                },
            )
        ).mappings()
        fts_rows: list[Mapping[Any, Any]] = []
        vector_rows: list[Mapping[Any, Any]] = []
        for row in rows:
            if row["channel"] == "vector":
                vector_rows.append(row)
            else:
                fts_rows.append(row)
        return MemorySearchChannels(
            fts=memory_channel_hits(fts_rows, request.memories),
            vector=memory_channel_hits(vector_rows, request.memories),
        )


def _embedding_hash(profile: EmbeddingProfile, entry_hash: str) -> str:
    return embedding_content_hash(
        profile_id=profile.profile_id,
        model=profile.model,
        dimension=profile.dimension,
        distance=profile.distance,
        normalization=profile.normalization,
        entry_content_hash=entry_hash,
    )
