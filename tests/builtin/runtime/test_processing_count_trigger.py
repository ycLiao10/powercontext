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

"""Count admission starts a Family once enough new Source identities arrive."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.processing_intents import ArtifactProcessingIntentRepository
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile
from powercontext.builtin.persistence.supervision import ArtifactProcessingLeaseRepository
from powercontext.builtin.persistence.tables import SHARED_TABLES
from powercontext.builtin.runtime.artifact_processing import ArtifactProcessingBinding, ArtifactProcessingSupervisor
from powercontext.builtin.runtime.config import RuntimeConfig
from powercontext.builtin.runtime.processing_contracts import ArtifactProcessingWorkerCompletion
from powercontext.builtin.runtime.processing_count import pending_source_counts
from powercontext.builtin.sources import BUILTIN_SOURCE_REGISTRY, ContentSource, SourceCursor
from powercontext.sources.models import SourceMaterialization

MEMORY_BINDING = "memory-source-window"
TOPIC_BINDING = "topic-memory-source-window"


async def _wait(predicate, timeout_seconds=10):
    async with asyncio.timeout(timeout_seconds):
        while not predicate():  # noqa: ASYNC110 - bounded observation of worker events
            await asyncio.sleep(0.01)


def _profile(tmp_path, name):
    return SQLiteProfile.open(
        SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / (name + '.db')}"), tables=SHARED_TABLES
    )


async def _capture(database, scope_id, source_ids):
    repository = SourceRepository(BUILTIN_SOURCE_REGISTRY)
    async with database.transaction() as connection:
        for source_id in source_ids:
            await repository.add(
                connection,
                scope_id,
                ContentSource(
                    name=source_id,
                    materialization=SourceMaterialization.CAPTURED,
                    content=f"body of {source_id}",
                ),
            )


class _Launcher:
    """Accept one assignment and acknowledge it without consuming input."""

    def __init__(self, database):
        self.database = database
        self.assignments = []

    async def start(self, assignment):
        self.assignments.append(assignment)
        return _Handle(self, assignment)


class _Handle:
    def __init__(self, launcher, assignment):
        self.launcher = launcher
        self.assignment = assignment

    async def wait(self):
        work = self.assignment
        async with self.launcher.database.transaction() as connection:
            await ArtifactProcessingLeaseRepository().require_fence(connection, work.fence)
            intents = ArtifactProcessingIntentRepository()
            row = await intents.load(connection, work.scope_id, work.binding_name, for_update=True)
            assert row is not None
            await intents.acknowledge(
                connection,
                work.scope_id,
                work.binding_name,
                work.claimed_request_generation,
                clean_generation=row.dirty_generation,
            )
        return ArtifactProcessingWorkerCompletion()

    async def terminate(self):
        return None


def test_source_count_thresholds_accept_only_a_strict_positive_integer():
    for field in ("memory_source_count_threshold", "topic_memory_source_count_threshold"):
        assert field in RuntimeConfig.model_fields
        assert getattr(RuntimeConfig(), field) is None
        for value in (None, 1, 3, 100):
            assert getattr(RuntimeConfig(**{field: value}), field) == value
        for value in (True, False, 0, -1, 1.0, "3"):
            with pytest.raises(ValidationError):
                RuntimeConfig(**{field: value})


def test_binding_rejects_a_non_positive_count_threshold():
    with pytest.raises(ValueError, match="source count threshold"):
        ArtifactProcessingBinding(
            MEMORY_BINDING,
            "memory",
            _Launcher(None),
            source_count_threshold=0,
        )


def test_the_environment_reads_a_count_threshold_as_an_integer(monkeypatch):
    from powercontext.server.settings import ServerSettings

    monkeypatch.setenv("POWERCONTEXT_SERVER_RUNTIME_MEMORY_SOURCE_COUNT_THRESHOLD", "5")
    monkeypatch.setenv("POWERCONTEXT_SERVER_RUNTIME_TOPIC_MEMORY_SOURCE_COUNT_THRESHOLD", "7")
    settings = ServerSettings()
    assert settings.runtime.memory_source_count_threshold == 5
    assert settings.runtime.topic_memory_source_count_threshold == 7
    monkeypatch.setenv("POWERCONTEXT_SERVER_RUNTIME_MEMORY_SOURCE_COUNT_THRESHOLD", "0")
    with pytest.raises(ValidationError):
        ServerSettings()


def test_pending_source_counts_follow_the_journal_after_each_cursor(tmp_path):
    async def scenario():
        async with _profile(tmp_path, "counts") as profile:
            await _capture(profile.database, "left", ("one", "two", "three"))
            await _capture(profile.database, "right", ("only",))
            async with profile.database.transaction() as connection:
                counts = await pending_source_counts(connection, ("left", "right", "absent"), MEMORY_BINDING)
            assert counts == {"left": 3, "right": 1, "absent": 0}
            async with profile.database.transaction() as connection:
                await SourceCursorRepository().save(
                    connection, "left", MEMORY_BINDING, SourceCursor(sequence=2), expected_generation=None
                )
            async with profile.database.transaction() as connection:
                counts = await pending_source_counts(connection, ("left", "right"), MEMORY_BINDING)
            assert counts == {"left": 1, "right": 1}
            # A repeated identity keeps its journal position and adds no entry.
            await _capture(profile.database, "left", ("two",))
            async with profile.database.transaction() as connection:
                counts = await pending_source_counts(connection, ("left",), MEMORY_BINDING)
            assert counts == {"left": 1}

    asyncio.run(scenario())


def test_count_admission_waits_for_the_threshold_in_every_scope(tmp_path):
    async def scenario():
        async with _profile(tmp_path, "admission") as profile:
            await _capture(profile.database, "left", ("one", "two"))
            await _capture(profile.database, "right", ("only",))
            launcher = _Launcher(profile.database)
            async with ArtifactProcessingSupervisor(
                database=profile.database,
                bindings=(
                    ArtifactProcessingBinding(
                        MEMORY_BINDING,
                        "memory",
                        launcher,
                        source_count_threshold=2,
                    ),
                ),
                lease_mode="single-process",
            ) as supervisor:
                await _wait(lambda: [work.scope_id for work in launcher.assignments] == ["left"])
                await asyncio.sleep(0.6)
                assert [work.scope_id for work in launcher.assignments] == ["left"]
                await _capture(profile.database, "right", ("second",))
                await _wait(lambda: len(launcher.assignments) == 2)
                assert {work.scope_id for work in launcher.assignments} == {"left", "right"}
                await _wait(lambda: supervisor.family_status["memory"]["completed"] == 2)

    asyncio.run(scenario())


def test_memory_and_topic_memory_keep_independent_counts(tmp_path):
    async def scenario():
        async with _profile(tmp_path, "families") as profile:
            await _capture(profile.database, "scope", ("one", "two"))
            memory = _Launcher(profile.database)
            topic = _Launcher(profile.database)
            async with ArtifactProcessingSupervisor(
                database=profile.database,
                bindings=(
                    ArtifactProcessingBinding(MEMORY_BINDING, "memory", memory, source_count_threshold=2),
                    ArtifactProcessingBinding(TOPIC_BINDING, "topic-memory", topic, source_count_threshold=3),
                ),
                lease_mode="single-process",
            ):
                await _wait(lambda: len(memory.assignments) == 1)
                await asyncio.sleep(0.6)
                assert len(topic.assignments) == 0
                await _capture(profile.database, "scope", ("three",))
                await _wait(lambda: len(topic.assignments) == 1)

    asyncio.run(scenario())
