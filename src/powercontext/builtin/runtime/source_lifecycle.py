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

"""Withdraw and restore derived evidence when the Sources behind it change.

Every Scope owns a small dependency graph: captured Sources at the roots, Memory
entries and Topic Memories above them, each declaring whether it needs all of its
parents (``all``) or any one of them (``any``). An event changes one Source's
availability and the change propagates to every descendant.

Only usage eligibility moves. Memory entries keep their exact body version and
Topic Memories keep their exact Revision, so an audit reference still resolves to
what was written at the time; what changes is whether the node may still be
recalled for generation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

import rfc8785
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.artifact_governance import (
    ArtifactGovernanceRepository,
    ArtifactLifecycleState,
)
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.tables import (
    SOURCE_LIFECYCLE_EDGES_TABLE,
    SOURCE_LIFECYCLE_NODES_TABLE,
)
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.sources import SourceRef

SOURCE_NODE = "source"
MEMORY_NODE = "memory"
TOPIC_NODE = "topic"
POLICIES = ("all", "any")
PURPOSES = ("generation", "audit")
_POLICY_TYPES = frozenset({MEMORY_NODE, TOPIC_NODE})


class SourceLifecycleError(ValueError):
    """Raised when a registration, event, or reference breaks the lifecycle contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SourceLifecycleEvent(BaseModel):
    """One explicit availability change for a registered Source node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    revision: int
    sequence: int
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be non-empty text")  # noqa: TRY003
        return value

    @field_validator("revision", "sequence", mode="before")
    @classmethod
    def validate_counter(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("must be a positive integer")  # noqa: TRY003
        return value

    @field_validator("available", mode="before")
    @classmethod
    def validate_available(cls, value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValueError("must be a boolean")  # noqa: TRY003
        return value


@dataclass(frozen=True)
class _LifecycleNode:
    """One registered graph node with its persisted eligibility."""

    node_id: str
    kind: str
    policy: str | None
    revision: int
    sequence: int
    available: bool
    payload: dict[str, Any]

    def reference(self) -> SourceRef | MemoryCitation | ArtifactRef:
        if self.kind == SOURCE_NODE:
            return SourceRef.model_validate(self.payload)
        if self.kind == MEMORY_NODE:
            return MemoryCitation.model_validate(self.payload)
        return ArtifactRef.model_validate(self.payload)


class SourceLifecycleApplication:
    """Select the per-Scope lifecycle service."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, scope_id)


class ScopedSourceLifecycle:
    """Own one Scope's dependency graph and reconcile it with native evidence."""

    def __init__(self, runtime: Any, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = scope_id

    # ------------------------------------------------------------------ registration

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        node_id = _require_node_id(node_id)
        revision = _require_positive("revision", revision)
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source-reference")
        await self._require_source(source_ref)
        node = _LifecycleNode(
            node_id=node_id,
            kind=SOURCE_NODE,
            policy=None,
            revision=revision,
            sequence=0,
            available=True,
            payload=source_ref.model_dump(mode="json"),
        )
        await self._register(node, parents=())

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        node_id = _require_node_id(node_id)
        policy = _require_policy(policy)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory-reference")
        await self._require_citation(citation)
        node = _LifecycleNode(
            node_id=node_id,
            kind=MEMORY_NODE,
            policy=policy,
            revision=0,
            sequence=0,
            available=True,
            payload=citation.model_dump(mode="json"),
        )
        await self._register(node, parents=_require_parents(parents, node_id))

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        node_id = _require_node_id(node_id)
        policy = _require_policy(policy)
        if not isinstance(artifact_ref, ArtifactRef):
            raise SourceLifecycleError("topic-reference")
        await self._require_topic(artifact_ref)
        node = _LifecycleNode(
            node_id=node_id,
            kind=TOPIC_NODE,
            policy=policy,
            revision=0,
            sequence=0,
            available=True,
            payload=artifact_ref.model_dump(mode="json"),
        )
        await self._register(node, parents=_require_parents(parents, node_id))

    # ------------------------------------------------------------------------ events

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Propagate one Source event and return every usable node id, sorted."""

        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event")
        nodes, edges = await self._graph()
        node = nodes.get(event.node_id)
        if node is None or node.kind != SOURCE_NODE:
            raise SourceLifecycleError("unknown-source")
        if event.revision != node.revision:
            raise SourceLifecycleError("revision-mismatch")
        if event.sequence < node.sequence:
            raise SourceLifecycleError("sequence-regression")
        if event.sequence == node.sequence:
            if event.available != node.available:
                raise SourceLifecycleError("sequence-conflict")
            # A replayed event already reached every descendant.
            replayed = _propagate(nodes, edges)
            return sorted(node_id for node_id, available in replayed.items() if available)

        updated = dict(nodes)
        updated[event.node_id] = _LifecycleNode(
            node_id=node.node_id,
            kind=node.kind,
            policy=node.policy,
            revision=node.revision,
            sequence=event.sequence,
            available=event.available,
            payload=node.payload,
        )
        active = _propagate(updated, edges)
        await self._settle_change(nodes, updated, active)
        await self._persist_availability(event, updated, active)
        return sorted(node_id for node_id, available in active.items() if available)

    # --------------------------------------------------------------------- references

    async def resolve(self, node_id: str, /, *, purpose: str) -> SourceRef | MemoryCitation | ArtifactRef:
        """Return the node's original reference, or refuse a generation request."""

        if purpose not in PURPOSES:
            raise SourceLifecycleError("purpose")
        node_id = _require_node_id(node_id)
        nodes, _edges = await self._graph()
        node = nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError("unavailable")
        return node.reference()

    # ------------------------------------------------------------------------ internals

    async def _register(self, node: _LifecycleNode, parents: tuple[str, ...]) -> None:
        async with self._database.transaction() as connection:
            existing = await self._load_node(connection, node.node_id)
            if existing is not None:
                stored_parents = await self._load_parents(connection, node.node_id)
                if stored_parents != parents or _binding_key(existing) != _binding_key(node):
                    raise SourceLifecycleError("registration-conflict")
                node = existing
            else:
                known = await self._load_nodes(connection)
                missing = [parent for parent in parents if parent not in known]
                if missing:
                    raise SourceLifecycleError("unknown-parent")
                available = _evaluate(node.policy, [known[parent].available for parent in parents])
                node = _LifecycleNode(
                    node_id=node.node_id,
                    kind=node.kind,
                    policy=node.policy,
                    revision=node.revision,
                    sequence=node.sequence,
                    available=available,
                    payload=node.payload,
                )
                await connection.execute(
                    insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                        scope_id=self.scope_id,
                        node_id=node.node_id,
                        kind=node.kind,
                        policy=node.policy,
                        revision=node.revision,
                        sequence=node.sequence,
                        available=node.available,
                        payload=rfc8785.dumps(node.payload),
                    )
                )
                for ordinal, parent in enumerate(parents):
                    await connection.execute(
                        insert(SOURCE_LIFECYCLE_EDGES_TABLE).values(
                            scope_id=self.scope_id,
                            node_id=node.node_id,
                            ordinal=ordinal,
                            parent_id=parent,
                        )
                    )
        # A registration fixes eligibility immediately instead of waiting for the
        # next event, and repeating an identical registration completes an
        # interrupted reconciliation.
        await self._settle_nodes((node,))

    async def _graph(self) -> tuple[dict[str, _LifecycleNode], dict[str, tuple[str, ...]]]:
        async with self._database.transaction() as connection:
            nodes = await self._load_nodes(connection)
            edges = await self._load_edges(connection)
        return nodes, edges

    async def _load_nodes(self, connection: AsyncConnection) -> dict[str, _LifecycleNode]:
        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE).where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id)
            )
        ).mappings()
        return {str(row["node_id"]): _decode_node(row) for row in rows}

    async def _load_node(self, connection: AsyncConnection, node_id: str) -> _LifecycleNode | None:
        row = (
            (
                await connection.execute(
                    select(SOURCE_LIFECYCLE_NODES_TABLE).where(
                        SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id,
                        SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _decode_node(row)

    async def _load_edges(self, connection: AsyncConnection) -> dict[str, tuple[str, ...]]:
        table = SOURCE_LIFECYCLE_EDGES_TABLE
        rows = (
            await connection.execute(
                select(table.c.node_id, table.c.parent_id)
                .where(table.c.scope_id == self.scope_id)
                .order_by(table.c.node_id, table.c.ordinal)
            )
        ).all()
        edges: dict[str, list[str]] = {}
        for node_id, parent_id in rows:
            edges.setdefault(str(node_id), []).append(str(parent_id))
        return {node_id: tuple(parents) for node_id, parents in edges.items()}

    async def _load_parents(self, connection: AsyncConnection, node_id: str) -> tuple[str, ...]:
        table = SOURCE_LIFECYCLE_EDGES_TABLE
        rows = (
            await connection.execute(
                select(table.c.parent_id)
                .where(table.c.scope_id == self.scope_id, table.c.node_id == node_id)
                .order_by(table.c.ordinal)
            )
        ).all()
        return tuple(str(row[0]) for row in rows)

    async def _persist_availability(
        self,
        event: SourceLifecycleEvent,
        updated: Mapping[str, _LifecycleNode],
        active: Mapping[str, bool],
    ) -> None:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._database.transaction() as connection:
            await connection.execute(
                update(table)
                .where(table.c.scope_id == self.scope_id, table.c.node_id == event.node_id)
                .values(available=event.available, sequence=event.sequence)
            )
            for node_id, available in active.items():
                node = updated[node_id]
                if node.kind == SOURCE_NODE or node.available == available:
                    continue
                await connection.execute(
                    update(table)
                    .where(table.c.scope_id == self.scope_id, table.c.node_id == node_id)
                    .values(available=available)
                )

    # ------------------------------------------------------------------- native writes

    async def _settle_change(
        self,
        previous: Mapping[str, _LifecycleNode],
        updated: Mapping[str, _LifecycleNode],
        active: Mapping[str, bool],
    ) -> None:
        """Move native evidence for every derived node whose eligibility flipped."""

        changed = tuple(
            replace(updated[node_id], available=available)
            for node_id, available in active.items()
            if updated[node_id].kind in _POLICY_TYPES and previous[node_id].available != available
        )
        await self._settle_nodes(changed)

    async def _settle_nodes(self, nodes: Iterable[_LifecycleNode]) -> None:
        targets = tuple(nodes)
        memories = tuple(node for node in targets if node.kind == MEMORY_NODE)
        topics = tuple(node for node in targets if node.kind == TOPIC_NODE)
        if memories:
            await self._settle_memory(memories)
        if topics:
            await self._settle_topics(topics)

    async def _settle_memory(self, nodes: Sequence[_LifecycleNode]) -> None:
        async with self._runtime._context(self.scope_id) as context:
            async with self._runtime._locked(self.scope_id):
                service = context.artifacts.memory
                artifact_id = context.artifacts.memory_artifact_id
                for node in nodes:
                    try:
                        memory, entries = await service.head_entries(artifact_id)
                    except Exception as error:  # noqa: BLE001 - surfaced as a lifecycle failure
                        raise SourceLifecycleError("memory-unavailable") from error
                    manifest = {item.entry_id: item for item in memory.content.manifest.entries}
                    citation = MemoryCitation.model_validate(node.payload)
                    item = manifest.get(citation.entry_id)
                    if item is None:
                        raise SourceLifecycleError("memory-entry-missing")
                    target = "active" if node.available else "inactive"
                    if item.state == target:
                        continue
                    entry = next((entry for entry in entries if entry.entry_id == citation.entry_id), None)
                    if entry is None:
                        raise SourceLifecycleError("memory-entry-missing")
                    if target == "active":
                        await service.reactivate(memory, entries=(entry,), reason="source-lifecycle")
                    else:
                        await service.forget(memory, entries=(entry,), reason="source-lifecycle")

    async def _settle_topics(self, nodes: Sequence[_LifecycleNode]) -> None:
        repository = ArtifactGovernanceRepository()
        async with self._database.transaction() as connection:
            for node in nodes:
                reference = ArtifactRef.model_validate(node.payload)
                current = await repository.get(
                    connection, self.scope_id, reference.family, reference.artifact_id
                )
                target = ArtifactLifecycleState.ACTIVE if node.available else ArtifactLifecycleState.DEPRECATED
                if current.lifecycle_state is target:
                    continue
                if (
                    target is ArtifactLifecycleState.ACTIVE
                    and current.lifecycle_state is ArtifactLifecycleState.RETIRED
                ):
                    raise SourceLifecycleError("topic-retired")
                await repository.transition(
                    connection,
                    self.scope_id,
                    reference.family,
                    reference.artifact_id,
                    current.governance_generation,
                    target,
                    None,
                )

    # ---------------------------------------------------------------------- validation

    @property
    def _database(self) -> AsyncDatabase:
        database = getattr(self._runtime._provider, "database", None)
        if not isinstance(database, AsyncDatabase):
            raise SourceLifecycleError("relational-runtime")
        return database

    async def _require_source(self, reference: SourceRef) -> None:
        try:
            await self._runtime.records.for_scope(self.scope_id).get_source(
                reference.source_type, reference.source_id
            )
        except Exception as error:  # noqa: BLE001 - any read failure means the reference is unusable
            raise SourceLifecycleError("source-missing") from error

    async def _require_citation(self, citation: MemoryCitation) -> None:
        try:
            await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=citation))
        except Exception as error:  # noqa: BLE001 - any read failure means the citation is unusable
            raise SourceLifecycleError("memory-missing") from error

    async def _require_topic(self, reference: ArtifactRef) -> None:
        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                reference.family, reference.artifact_id, reference.revision
            )
        except Exception as error:  # noqa: BLE001 - any read failure means the revision is unusable
            raise SourceLifecycleError("topic-missing") from error


def _decode_node(row: Mapping[str, Any]) -> _LifecycleNode:
    return _LifecycleNode(
        node_id=str(row["node_id"]),
        kind=str(row["kind"]),
        policy=None if row["policy"] is None else str(row["policy"]),
        revision=int(row["revision"]),
        sequence=int(row["sequence"]),
        available=bool(row["available"]),
        payload=cast(dict[str, Any], json.loads(bytes(row["payload"]))),
    )


def _binding_key(node: _LifecycleNode) -> tuple[str, str | None, int, dict[str, Any]]:
    return (node.kind, node.policy, node.revision, node.payload)


def _evaluate(policy: str | None, support: Sequence[bool]) -> bool:
    if not support:
        return True
    return all(support) if policy == "all" else any(support)


def _propagate(
    nodes: Mapping[str, _LifecycleNode],
    edges: Mapping[str, tuple[str, ...]],
) -> dict[str, bool]:
    """Resolve every node from its parents in dependency order."""

    resolved: dict[str, bool] = {
        node_id: node.available for node_id, node in nodes.items() if node.kind == SOURCE_NODE
    }
    pending = [node for node in nodes.values() if node.kind != SOURCE_NODE]
    progressed = True
    while progressed and pending:
        progressed = False
        waiting: list[_LifecycleNode] = []
        for node in pending:
            parents = edges.get(node.node_id, ())
            support = [resolved[parent] for parent in parents if parent in resolved]
            if len(support) != len(parents):
                waiting.append(node)
                continue
            resolved[node.node_id] = _evaluate(node.policy, support)
            progressed = True
        pending = waiting
    return resolved


def _require_node_id(node_id: Any) -> str:
    if not isinstance(node_id, str) or not node_id.strip() or node_id != node_id.strip():
        raise SourceLifecycleError("node-id")
    return node_id


def _require_positive(field: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourceLifecycleError(field)
    return value


def _require_policy(policy: Any) -> str:
    if policy not in POLICIES:
        raise SourceLifecycleError("policy")
    return cast(str, policy)


def _require_parents(parents: Any, node_id: str) -> tuple[str, ...]:
    if isinstance(parents, (str, bytes)) or not isinstance(parents, Sequence):
        raise SourceLifecycleError("parents")
    resolved = tuple(parents)
    if not resolved:
        raise SourceLifecycleError("parents-required")
    if len(set(resolved)) != len(resolved):
        raise SourceLifecycleError("parents-duplicate")
    if node_id in resolved:
        raise SourceLifecycleError("parents-cycle")
    return resolved


async def clear_scope_lifecycle(connection: AsyncConnection, scope_id: str, /) -> None:
    """Remove a Scope's graph; used by Scope deletion rather than by lifecycle calls."""

    for table in (SOURCE_LIFECYCLE_EDGES_TABLE, SOURCE_LIFECYCLE_NODES_TABLE):
        await connection.execute(delete(table).where(table.c.scope_id == scope_id))


__all__ = [
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "ScopedSourceLifecycle",
    "clear_scope_lifecycle",
]
