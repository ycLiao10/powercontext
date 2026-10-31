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

"""Source withdrawal lifecycle over the Memory and Topic Memory dependency graph.

A Source that stops being used must also withdraw the Memory entries and the
Topic Memory artifacts that depend on it: content whose evidence is gone may
not keep entering the current search and Context, while content that still has
independent support must stay usable. Deactivating a Memory entry keeps its
body, so an older reference keeps pointing at the version that was current at
the time.

Each Scope owns one dependency graph. Every registered node names the parents
that support it and whether it needs all of them or any of them. A Source
event changes one root; the graph recomputes availability in registration
order, and the difference is applied to the native Memory and Topic Memory
surfaces.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from powercontext.artifacts.models import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.source_lifecycle import (
    SourceLifecycleEventRecord,
    SourceLifecycleNodeRecord,
    SourceLifecycleRepository,
)
from powercontext.builtin.sources import validate_scope_id
from powercontext.limits import MAX_SOURCE_LIFECYCLE_NODE_ID_LENGTH
from powercontext.sources.models import SourceRef

if TYPE_CHECKING:
    from collections.abc import Sequence

    from powercontext.builtin.persistence.database import AsyncDatabase

__all__ = [
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "SourceLifecycleRegistry",
    "SourceLifecycleService",
]

_SOURCE_KIND = "source"
_MEMORY_KIND = "memory"
_TOPIC_KIND = "topic"
_ALL_POLICY = "all"
_ANY_POLICY = "any"
_NATIVE_REASON = "source-lifecycle"


class SourceLifecycleError(ValueError):
    """Report a lifecycle request the dependency graph cannot accept."""


class SourceLifecycleEvent(BaseModel):
    """One Source availability change at an exact lifecycle version and sequence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    reason: str
    revision: StrictInt = Field(gt=0)
    sequence: StrictInt = Field(gt=0)
    available: StrictBool

    @field_validator("node_id", "reason")
    @classmethod
    def require_text(cls, value: str) -> str:
        """Reject empty and blank-only text."""

        if not value.strip():
            raise ValueError("must not be blank")  # noqa: TRY003
        return value


@dataclass(slots=True)
class _Node:
    """One registered node of the in-memory dependency graph."""

    node_id: str
    kind: str
    policy: str
    revision: int
    available: bool
    parents: tuple[str, ...]
    reference: dict[str, Any]
    applied_sequence: int
    registration_order: int

    def record(self) -> SourceLifecycleNodeRecord:
        """Return the storage form of this node."""

        return SourceLifecycleNodeRecord(
            node_id=self.node_id,
            kind=self.kind,
            policy=self.policy,
            revision=self.revision,
            available=self.available,
            parents=self.parents,
            reference=self.reference,
            applied_sequence=self.applied_sequence,
            registration_order=self.registration_order,
        )


class SourceLifecycleRegistry:
    """Runtime-level registry of the per-Scope lifecycle services."""

    def __init__(self, database: AsyncDatabase) -> None:
        self.repository = SourceLifecycleRepository(database)
        self._runtime: Any | None = None
        self._services: dict[str, SourceLifecycleService] = {}

    def bind(self, runtime: Any) -> None:
        """Attach the Runtime that owns the native Memory and Topic Memory surfaces."""

        self._runtime = runtime

    @property
    def runtime(self) -> Any:
        """Return the bound Runtime."""

        if self._runtime is None:
            raise SourceLifecycleError("runtime-unbound")
        return self._runtime

    def for_scope(self, scope_id: str) -> SourceLifecycleService:
        """Return the lifecycle service of one Scope."""

        scope = validate_scope_id(scope_id)
        service = self._services.get(scope)
        if service is None:
            service = SourceLifecycleService(scope_id=scope, registry=self)
            self._services[scope] = service
        return service

    async def withdrawn_topic_artifact_ids(self, scope_id: str) -> frozenset[str]:
        """Return the Topic artifacts that lost their support in one Scope."""

        service = self.for_scope(scope_id)
        await service.load()
        return service.withdrawn_topic_artifact_ids()


class SourceLifecycleService:
    """Register dependencies and propagate Source withdrawal inside one Scope."""

    def __init__(self, *, scope_id: str, registry: SourceLifecycleRegistry) -> None:
        self.scope_id = scope_id
        self._registry = registry
        self._nodes: dict[str, _Node] = {}
        self._events: dict[str, dict[int, SourceLifecycleEventRecord]] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Read the persisted graph of this Scope exactly once."""

        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            nodes = await self._registry.repository.load_nodes(self.scope_id)
            events = await self._registry.repository.load_events(self.scope_id)
            self._nodes = {
                node.node_id: _Node(
                    node_id=node.node_id,
                    kind=node.kind,
                    policy=node.policy,
                    revision=node.revision,
                    available=node.available,
                    parents=node.parents,
                    reference=node.reference,
                    applied_sequence=node.applied_sequence,
                    registration_order=node.registration_order,
                )
                for node in nodes
            }
            self._events = {}
            for event in events:
                self._events.setdefault(event.node_id, {})[event.sequence] = event
            self._loaded = True

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        """Register one Source as a lifecycle root at an exact lifecycle version."""

        await self.load()
        identifier = _require_node_id(node_id)
        if revision < 1:
            raise SourceLifecycleError("revision")
        reference = source_ref.model_dump(mode="json")
        existing = self._nodes.get(identifier)
        if existing is not None:
            if existing.kind == _SOURCE_KIND and existing.revision == revision and existing.reference == reference:
                return
            raise SourceLifecycleError("node-conflict")
        await self._require_readable_source(source_ref)
        node = _Node(
            node_id=identifier,
            kind=_SOURCE_KIND,
            policy=_ALL_POLICY,
            revision=revision,
            available=True,
            parents=(),
            reference=reference,
            applied_sequence=0,
            registration_order=self._next_registration_order(),
        )
        await self._registry.repository.insert_node(self.scope_id, node.record())
        self._nodes[identifier] = node

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        """Register one Memory entry with the parents that support it."""

        await self.load()
        identifier = _require_node_id(node_id)
        parent_ids = _require_parents(parents, identifier, self._nodes)
        support = _require_policy(policy)
        reference = citation.model_dump(mode="json")
        existing = self._nodes.get(identifier)
        if existing is not None:
            if (
                existing.kind == _MEMORY_KIND
                and existing.parents == parent_ids
                and existing.policy == support
                and existing.reference == reference
            ):
                return
            raise SourceLifecycleError("node-conflict")
        for other in self._nodes.values():
            if other.kind == _MEMORY_KIND and other.reference == reference:
                raise SourceLifecycleError("reference-conflict")
        await self._require_readable_citation(citation)
        node = _Node(
            node_id=identifier,
            kind=_MEMORY_KIND,
            policy=support,
            revision=1,
            available=False,
            parents=parent_ids,
            reference=reference,
            applied_sequence=0,
            registration_order=self._next_registration_order(),
        )
        node.available = self._supported(parent_ids, support)
        await self._registry.repository.insert_node(self.scope_id, node.record())
        self._nodes[identifier] = node
        if not node.available:
            await self._apply_native_state((node,), target=False)

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        """Register one Topic Memory artifact with the parents that support it."""

        await self.load()
        identifier = _require_node_id(node_id)
        parent_ids = _require_parents(parents, identifier, self._nodes)
        support = _require_policy(policy)
        reference = artifact_ref.model_dump(mode="json")
        existing = self._nodes.get(identifier)
        if existing is not None:
            if (
                existing.kind == _TOPIC_KIND
                and existing.parents == parent_ids
                and existing.policy == support
                and existing.reference == reference
            ):
                return
            raise SourceLifecycleError("node-conflict")
        for other in self._nodes.values():
            if other.kind == _TOPIC_KIND and other.reference == reference:
                raise SourceLifecycleError("reference-conflict")
        await self._require_readable_topic(artifact_ref)
        node = _Node(
            node_id=identifier,
            kind=_TOPIC_KIND,
            policy=support,
            revision=1,
            available=False,
            parents=parent_ids,
            reference=reference,
            applied_sequence=0,
            registration_order=self._next_registration_order(),
        )
        node.available = self._supported(parent_ids, support)
        await self._registry.repository.insert_node(self.scope_id, node.record())
        self._nodes[identifier] = node

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        """Apply one Source availability event and return the available node ids."""

        await self.load()
        node = self._nodes.get(event.node_id)
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if node.kind != _SOURCE_KIND:
            raise SourceLifecycleError("not-a-source")
        if event.revision != node.revision:
            raise SourceLifecycleError("revision-mismatch")
        if event.sequence < node.applied_sequence:
            raise SourceLifecycleError("sequence-regression")
        if event.sequence == node.applied_sequence:
            self._require_same_event(node.node_id, event)
            return self._available_node_ids()
        async with self._lock:
            current = self._nodes[event.node_id]
            if event.sequence < current.applied_sequence:
                raise SourceLifecycleError("sequence-regression")
            if event.sequence == current.applied_sequence:
                self._require_same_event(current.node_id, event)
                return self._available_node_ids()
            await self._reconcile()
            before = {identifier: node.available for identifier, node in self._nodes.items()}
            planned = self._plan(event.node_id, event.available)
            changed = tuple(
                node
                for identifier, node in sorted(self._nodes.items(), key=lambda item: item[1].registration_order)
                if before.get(identifier) != planned[identifier]
            )
            await self._apply_native_state(changed, planned=planned)
            updated = tuple(
                SourceLifecycleNodeRecord(
                    node_id=node.node_id,
                    kind=node.kind,
                    policy=node.policy,
                    revision=node.revision,
                    available=planned[node.node_id],
                    parents=node.parents,
                    reference=node.reference,
                    applied_sequence=event.sequence if node.node_id == event.node_id else node.applied_sequence,
                    registration_order=node.registration_order,
                )
                for node in sorted(self._nodes.values(), key=lambda item: item.registration_order)
                if node.node_id == event.node_id or before.get(node.node_id) != planned[node.node_id]
            )
            record = SourceLifecycleEventRecord(
                node_id=event.node_id,
                sequence=event.sequence,
                revision=event.revision,
                available=event.available,
                reason=event.reason,
            )
            await self._registry.repository.save_event(self.scope_id, record, updated)
            for identifier, available in planned.items():
                self._nodes[identifier].available = available
            self._nodes[event.node_id].applied_sequence = event.sequence
            self._events.setdefault(event.node_id, {})[event.sequence] = record
        return self._available_node_ids()

    async def resolve(self, node_id: str, *, purpose: str) -> ArtifactRef | MemoryCitation | SourceRef:
        """Return the registered reference of one node for the requested purpose."""

        await self.load()
        node = self._nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if purpose == "audit":
            return _reference_value(node)
        if purpose != "generation":
            raise SourceLifecycleError("unknown-purpose")
        if not node.available:
            raise SourceLifecycleError("unavailable")
        return _reference_value(node)

    def withdrawn_topic_artifact_ids(self) -> frozenset[str]:
        """Return the Topic artifacts of this Scope that lost their support."""

        return frozenset(
            str(node.reference["artifact_id"])
            for node in self._nodes.values()
            if node.kind == _TOPIC_KIND and not node.available
        )

    def _require_same_event(self, node_id: str, event: SourceLifecycleEvent) -> None:
        """Accept only an exact replay of the event already applied to one Source."""

        recorded = self._events.get(node_id, {}).get(event.sequence)
        if (
            recorded is not None
            and recorded.revision == event.revision
            and recorded.available == event.available
            and recorded.reason == event.reason
        ):
            return
        raise SourceLifecycleError("sequence-conflict")

    def _available_node_ids(self) -> list[str]:
        """Return every available node id in node id order."""

        return sorted(node.node_id for node in self._nodes.values() if node.available)

    def _next_registration_order(self) -> int:
        """Return the next registration position, which is also the topological order."""

        return max((node.registration_order for node in self._nodes.values()), default=0) + 1

    def _supported(self, parents: tuple[str, ...], policy: str) -> bool:
        """Return whether the parents currently support one derived node."""

        support = [self._nodes[parent].available for parent in parents if parent in self._nodes]
        if len(support) != len(parents):
            return False
        return all(support) if policy == _ALL_POLICY else any(support)

    def _plan(self, source_id: str, available: bool) -> dict[str, bool]:
        """Compute the availability of every node after one Source change.

        The pass follows registration order, which is the topological order of
        the graph, so a parent is always resolved before its children.
        """

        planned: dict[str, bool] = {}
        for node in sorted(self._nodes.values(), key=lambda item: item.registration_order):
            if node.kind == _SOURCE_KIND:
                planned[node.node_id] = available if node.node_id == source_id else node.available
                continue
            support = [planned.get(parent, False) for parent in node.parents]
            planned[node.node_id] = all(support) if node.policy == _ALL_POLICY else any(support)
        return planned

    async def _reconcile(self) -> None:
        """Align the native Memory states with the registered graph before propagating."""

        memory_nodes = tuple(node for node in self._nodes.values() if node.kind == _MEMORY_KIND)
        if not memory_nodes:
            return
        runtime = self._registry.runtime
        async with runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            head = await service.head(context.artifacts.memory_artifact_id)
            states = {item.entry_id: item.state for item in head.content.manifest.entries}
            inactive = []
            active = []
            for node in memory_nodes:
                entry_id = str(node.reference["entry_id"])
                state = states.get(entry_id)
                if state is None:
                    continue
                if state == "active" and not node.available:
                    inactive.append(node)
                elif state == "inactive" and node.available:
                    active.append(node)
            await self._apply_native_state(tuple(inactive), target=False)
            await self._apply_native_state(tuple(active), target=True)

    async def _apply_native_state(
        self,
        nodes: tuple[_Node, ...],
        *,
        planned: dict[str, bool] | None = None,
        target: bool | None = None,
    ) -> None:
        """Apply Memory entry states, one native call per Memory and target state."""

        groups: dict[tuple[str, bool], list[_Node]] = {}
        for node in nodes:
            if node.kind != _MEMORY_KIND:
                continue
            if planned is not None:
                state = planned[node.node_id]
            elif target is not None:
                state = target
            else:
                state = node.available
            artifact_id = str(node.reference["memory_ref"]["artifact_id"])
            groups.setdefault((artifact_id, state), []).append(node)
        if not groups:
            return
        runtime = self._registry.runtime
        async with runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            async with runtime._locked(self.scope_id):
                for (artifact_id, state), group in groups.items():
                    entries = []
                    for node in group:
                        citation = MemoryCitation.model_validate(node.reference)
                        entries.append(await service.validate_citation(citation))
                    current = await service.head(artifact_id)
                    if state:
                        await service.reactivate(current, entries=entries, reason=_NATIVE_REASON)
                    else:
                        await service.forget(current, entries=entries, reason=_NATIVE_REASON)

    async def _require_readable_source(self, source_ref: SourceRef) -> None:
        """Require that the registered Source exists and can be read."""

        runtime = self._registry.runtime
        try:
            await runtime.records.for_scope(self.scope_id).get_source(source_ref.source_type, source_ref.source_id)
        except Exception as error:
            raise SourceLifecycleError("source-unreadable") from error

    async def _require_readable_citation(self, citation: MemoryCitation) -> None:
        """Require that the registered Memory citation exists and can be read."""

        runtime = self._registry.runtime
        try:
            async with runtime._context(self.scope_id) as context:
                await context.artifacts.memory.validate_citation(citation)
        except Exception as error:
            raise SourceLifecycleError("citation-unreadable") from error

    async def _require_readable_topic(self, artifact_ref: ArtifactRef) -> None:
        """Require that the registered Topic Memory revision exists and can be read."""

        runtime = self._registry.runtime
        try:
            await runtime.records.for_scope(self.scope_id).get_artifact_revision(
                artifact_ref.family,
                artifact_ref.artifact_id,
                artifact_ref.revision,
            )
        except Exception as error:
            raise SourceLifecycleError("topic-unreadable") from error


def _require_node_id(node_id: str) -> str:
    """Return one usable node identifier."""

    if not isinstance(node_id, str) or not node_id.strip():
        raise SourceLifecycleError("node-id")
    if len(node_id) > MAX_SOURCE_LIFECYCLE_NODE_ID_LENGTH:
        raise SourceLifecycleError("node-id")
    return node_id


def _require_policy(policy: str) -> str:
    """Return one supported dependency policy."""

    if policy not in {_ALL_POLICY, _ANY_POLICY}:
        raise SourceLifecycleError("policy")
    return policy


def _require_parents(
    parents: Sequence[str],
    node_id: str,
    nodes: dict[str, _Node],
) -> tuple[str, ...]:
    """Return the validated parents of one derived node."""

    parent_ids = tuple(parents)
    if not parent_ids:
        raise SourceLifecycleError("parents-empty")
    if len(set(parent_ids)) != len(parent_ids):
        raise SourceLifecycleError("parents-duplicate")
    if node_id in parent_ids:
        raise SourceLifecycleError("parents-cycle")
    for parent in parent_ids:
        if parent not in nodes:
            raise SourceLifecycleError("parents-missing")
    return parent_ids


def _reference_value(node: _Node) -> ArtifactRef | MemoryCitation | SourceRef:
    """Return the registered reference of one node in its declared type."""

    if node.kind == _SOURCE_KIND:
        return SourceRef.model_validate(node.reference)
    if node.kind == _MEMORY_KIND:
        return MemoryCitation.model_validate(node.reference)
    return ArtifactRef.model_validate(node.reference)
