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

"""Count unconsumed Source journal entries for one processing Family.

Count admission reads persisted journal identities only. It never inspects a
Source body, a token estimate, a generated row, or a model output. Every Family
keeps its own cursor, so a Scope's Memory count and its Topic Memory count
never substitute for each other.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.tables import SOURCES_TABLE


async def pending_source_counts(
    connection: AsyncConnection,
    scope_ids: Sequence[str],
    binding_name: str,
    /,
) -> dict[str, int]:
    """Return each Scope's journal entries after its binding cursor.

    The count starts after the cursor last committed by that binding and ends
    at the current journal tail. A Server-owned lineage-only Source occupies a
    journal entry like any other, so it counts toward admission even though the
    generation rules keep it out of model evidence.
    """

    if not scope_ids:
        return {}
    ordered = tuple(dict.fromkeys(scope_ids))
    cursors = await SourceCursorRepository().load_many(connection, ordered, binding_name)
    bounds = {
        scope_id: (0 if cursors.get(scope_id) is None else cursors[scope_id].cursor.sequence) for scope_id in ordered
    }
    totals = {
        str(scope_id): int(total)
        for scope_id, total in (
            await connection.execute(
                select(SOURCES_TABLE.c.scope_id, func.count())
                .where(SOURCES_TABLE.c.scope_id.in_(ordered))
                .group_by(SOURCES_TABLE.c.scope_id)
            )
        ).all()
    }
    counts: dict[str, int] = {}
    for scope_id, bound in bounds.items():
        total = totals.get(scope_id, 0)
        if total <= bound:
            # Every persisted identity in this Scope is already consumed.
            counts[scope_id] = 0
            continue
        if bound <= 0:
            # The first processing pass counts from the journal start.
            counts[scope_id] = total
            continue
        value = await connection.scalar(
            select(func.count())
            .select_from(SOURCES_TABLE)
            .where(SOURCES_TABLE.c.scope_id == scope_id, SOURCES_TABLE.c.journal_position > bound)
        )
        counts[scope_id] = int(value or 0)
    return counts


__all__ = ["pending_source_counts"]
