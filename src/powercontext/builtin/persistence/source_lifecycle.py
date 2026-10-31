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

"""Relational storage for the Source lifecycle dependency graph."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, update

from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.supervision import database_utc_now
from powercontext.builtin.persistence.tables import (
    SOURCE_LIFECYCLE_EVENTS_TABLE,
    SOURCE_LIFECYCLE_NODES_TABLE,
)


@dataclass(frozen=True, slots=True)
class SourceLifecycleNodeRecord:
    """One registered node of the dependency graph."""

    node_id: str
    kind: str
    policy: str
    revision: int
    available: bool
    parents: tuple[str, ...]
    reference: dict[str, Any]
    applied_sequence: int
    registration_order: int


@dataclass(frozen=True, slots=True)
class SourceLifecycleEventRecord:
    """One applied Source availability event."""

    node_id: str
    sequence: int
    revision: int
    available: bool
    reason: str


class SourceLifecycleRepository:
    """Read and write the dependency graph of one Scope."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def load_nodes(self, scope_id: str) -> tuple[SourceLifecycleNodeRecord, ...]:
        """Return every registered node of one Scope in registration order."""

        async with self._database.transaction() as connection:
            rows = (
                await connection.execute(
                    select(SOURCE_LIFECYCLE_NODES_TABLE)
                    .where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id)
                    .order_by(SOURCE_LIFECYCLE_NODES_TABLE.c.registration_order)
                )
            ).mappings()
        return tuple(
            SourceLifecycleNodeRecord(
                node_id=row["node_id"],
                kind=row["kind"],
                policy=row["policy"],
                revision=int(row["revision"]),
                available=bool(row["available"]),
                parents=tuple(json.loads(row["parents"])),
                reference=json.loads(row["reference"]),
                applied_sequence=int(row["applied_sequence"]),
                registration_order=int(row["registration_order"]),
            )
            for row in rows
        )

    async def load_events(self, scope_id: str) -> tuple[SourceLifecycleEventRecord, ...]:
        """Return every applied event of one Scope."""

        async with self._database.transaction() as connection:
            rows = (
                await connection.execute(
                    select(SOURCE_LIFECYCLE_EVENTS_TABLE).where(
                        SOURCE_LIFECYCLE_EVENTS_TABLE.c.scope_id == scope_id,
                    )
                )
            ).mappings()
        return tuple(
            SourceLifecycleEventRecord(
                node_id=row["node_id"],
                sequence=int(row["sequence"]),
                revision=int(row["revision"]),
                available=bool(row["available"]),
                reason=row["reason"],
            )
            for row in rows
        )

    async def insert_node(self, scope_id: str, node: SourceLifecycleNodeRecord) -> None:
        """Store one newly registered node."""

        async with self._database.transaction() as connection:
            await connection.execute(
                insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                    scope_id=scope_id,
                    node_id=node.node_id,
                    kind=node.kind,
                    policy=node.policy,
                    revision=node.revision,
                    available=node.available,
                    parents=json.dumps(list(node.parents), ensure_ascii=False),
                    reference=json.dumps(node.reference, ensure_ascii=False, sort_keys=True),
                    applied_sequence=node.applied_sequence,
                    registration_order=node.registration_order,
                )
            )

    async def save_event(
        self,
        scope_id: str,
        event: SourceLifecycleEventRecord,
        nodes: tuple[SourceLifecycleNodeRecord, ...],
    ) -> None:
        """Store one applied event together with the node states it produced."""

        async with self._database.transaction() as connection:
            recorded_at: datetime = await database_utc_now(connection)
            for node in nodes:
                await connection.execute(
                    update(SOURCE_LIFECYCLE_NODES_TABLE)
                    .where(
                        SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                        SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node.node_id,
                    )
                    .values(available=node.available, applied_sequence=node.applied_sequence)
                )
            await connection.execute(
                insert(SOURCE_LIFECYCLE_EVENTS_TABLE).values(
                    scope_id=scope_id,
                    node_id=event.node_id,
                    sequence=event.sequence,
                    revision=event.revision,
                    available=event.available,
                    reason=event.reason,
                    recorded_at=recorded_at,
                )
            )
