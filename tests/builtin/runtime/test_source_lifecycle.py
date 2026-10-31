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

"""Tests for the Source withdrawal lifecycle over Memory and Topic Memory."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.artifacts.memory.service import MemoryService
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    ContextAssembly,
    ContextAssemblySection,
    PrepareContextRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources.models import SourceRef

REVISION = 3


def _event(node_id: str, sequence: int, available: bool, *, revision: int = REVISION, reason: str = "fixture") -> SourceLifecycleEvent:
    return SourceLifecycleEvent(
        node_id=node_id,
        revision=revision,
        sequence=sequence,
        available=available,
        reason=reason,
    )


async def _scope(runtime, suffix: str = "fixture"):
    return await runtime.scopes.create(
        ScopeDraft(title="Lifecycle", summary="Dependency evidence", idempotency_key=suffix),
    )


async def _source(runtime, scope_id: str, source_id: str):
    receipt = await runtime.sources.for_scope(scope_id).capture(
        CaptureSource(source_id=source_id, content=f"Origin {source_id}", metadata={}),
    )
    return receipt.source_ref


async def _memory(runtime, scope_id: str, text: str):
    result = await runtime.memory.for_scope(scope_id).remember(
        RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=text),)),
    )
    return result.entry.citation


async def _topic(runtime, scope_id: str, title: str):
    created = await runtime.records.for_scope(scope_id).create_artifact(
        "topic-memory",
        ArtifactWrite(
            content={
                "title": title,
                "summary": f"{title} supported summary",
                "detail": f"{title} detailed evidence",
            },
        ),
    )
    return ArtifactRef(family="topic-memory", artifact_id=created.artifact_id, revision=created.revision)


async def _states(runtime, scope_id: str) -> dict[str, str]:
    page = await runtime.memory.for_scope(scope_id).list(include_inactive=True)
    return {record.entry.text.split()[0]: record.state for record in page.entries}


async def _build(runtime, suffix: str = "fixture"):
    """Register two roots, two derived Memory entries and one Topic Memory artifact."""

    scope = await _scope(runtime, suffix)
    scope_id = scope.scope_id
    lifecycle = runtime.lifecycle.for_scope(scope_id)
    for name in ("rootA", "rootB"):
        await lifecycle.register_source(name, await _source(runtime, scope_id, name), revision=REVISION)
    await lifecycle.register_memory("allfact", await _memory(runtime, scope_id, "allfact evidence"), parents=("rootA",), policy="all")
    await lifecycle.register_memory("anyfact", await _memory(runtime, scope_id, "anyfact evidence"), parents=("rootA", "rootB"), policy="any")
    await lifecycle.register_topic("topic", await _topic(runtime, scope_id, "topic"), parents=("allfact",), policy="all")
    return scope_id, lifecycle


def test_withdrawal_propagates_by_policy_and_restore_revives_content(tmp_path):
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope_id, lifecycle = await _build(runtime)
            assert await lifecycle.apply(_event("rootA", 1, False)) == ["anyfact", "rootB"]
            assert await _states(runtime, scope_id) == {"allfact": "inactive", "anyfact": "active"}
            page = await runtime.memory.for_scope(scope_id).search(SearchMemoryRequest(query="allfact", mode="fts", limit=10))
            assert page.hits == ()
            topics = await runtime.topic_memory.for_scope(scope_id).search(SearchTopicMemoryRequest(query="topic", limit=10))
            assert topics.hits == ()
            prepared = await runtime.context.for_scope(scope_id).prepare(
                PrepareContextRequest(
                    query="allfact",
                    max_bytes=4096,
                    assembly=ContextAssembly(
                        sections=(
                            ContextAssemblySection(family="memory", limit=6),
                            ContextAssemblySection(family="topic-memory", limit=2),
                        ),
                    ),
                ),
            )
            assert "allfact evidence" not in (prepared.content or "")
            assert await lifecycle.apply(_event("rootA", 2, True)) == ["allfact", "anyfact", "rootA", "rootB", "topic"]
            assert await _states(runtime, scope_id) == {"allfact": "active", "anyfact": "active"}
            page = await runtime.memory.for_scope(scope_id).search(SearchMemoryRequest(query="allfact", mode="fts", limit=10))
            assert [hit.text for hit in page.hits] == ["allfact evidence"]
            topics = await runtime.topic_memory.for_scope(scope_id).search(SearchTopicMemoryRequest(query="topic", limit=10))
            assert len(topics.hits) == 1
            prepared = await runtime.context.for_scope(scope_id).prepare(
                PrepareContextRequest(
                    query="allfact",
                    max_bytes=4096,
                    assembly=ContextAssembly(
                        sections=(
                            ContextAssemblySection(family="memory", limit=6),
                            ContextAssemblySection(family="topic-memory", limit=2),
                        ),
                    ),
                ),
            )
            assert "allfact evidence" in (prepared.content or "")

    asyncio.run(scenario())


def test_event_validation_rejects_unknown_source_revision_and_sequence(tmp_path):
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'events.db'}"))
        async with open_builtin_runtime(config) as runtime:
            _, lifecycle = await _build(runtime, "events")
            await lifecycle.apply(_event("rootA", 4, False))
            for invalid in (
                _event("rootA", 3, True),
                _event("rootA", 4, True),
                _event("missing", 1, True),
                _event("rootA", 5, True, revision=2),
                _event("allfact", 6, True),
            ):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.apply(invalid)

    asyncio.run(scenario())


def test_identical_event_replay_is_idempotent_and_adds_no_memory_revision(tmp_path):
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'replay.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope_id, lifecycle = await _build(runtime, "replay")
            first = await lifecycle.apply(_event("rootA", 1, False))
            before = (await runtime.memory.for_scope(scope_id).list()).memory_ref
            again = await lifecycle.apply(_event("rootA", 1, False))
            assert again == first
            assert (await runtime.memory.for_scope(scope_id).list()).memory_ref == before
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(_event("rootA", 1, True, reason="other"))

    asyncio.run(scenario())


def test_registration_is_idempotent_and_conflicting_bindings_are_rejected(tmp_path):
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'register.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope_id, lifecycle = await _build(runtime, "register")
            citation = await _memory(runtime, scope_id, "repeat evidence")
            await lifecycle.register_memory("repeat", citation, parents=("rootB",), policy="all")
            await lifecycle.register_memory("repeat", citation, parents=("rootB",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("repeat", citation, parents=("rootA",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("other", citation, parents=("rootB",), policy="all")

    asyncio.run(scenario())


def test_parents_must_exist_and_form_no_cycle(tmp_path):
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'parents.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope_id, lifecycle = await _build(runtime, "parents")
            citation = await _memory(runtime, scope_id, "candidate evidence")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("empty", citation, parents=(), policy="any")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("missing", citation, parents=("ghost",), policy="any")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("duplicate", citation, parents=("rootA", "rootA"), policy="any")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("selfcycle", citation, parents=("selfcycle",), policy="any")

    asyncio.run(scenario())


def test_derived_node_registered_under_withdrawn_parent_starts_inactive(tmp_path):
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'late.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope_id, lifecycle = await _build(runtime, "late")
            await lifecycle.apply(_event("rootA", 1, False))
            citation = await _memory(runtime, scope_id, "late evidence")
            await lifecycle.register_memory("late", citation, parents=("rootA",), policy="all")
            assert (await _states(runtime, scope_id))["late"] == "inactive"
            reference = await _topic(runtime, scope_id, "latetopic")
            await lifecycle.register_topic("latetopic", reference, parents=("late",), policy="all")
            topics = await runtime.topic_memory.for_scope(scope_id).search(SearchTopicMemoryRequest(query="latetopic", limit=10))
            assert topics.hits == ()
            assert await lifecycle.apply(_event("rootA", 2, True)) == [
                "allfact",
                "anyfact",
                "late",
                "latetopic",
                "rootA",
                "rootB",
                "topic",
            ]
            assert (await _states(runtime, scope_id))["late"] == "active"
            topics = await runtime.topic_memory.for_scope(scope_id).search(SearchTopicMemoryRequest(query="latetopic", limit=10))
            assert len(topics.hits) == 1

    asyncio.run(scenario())


def test_resolve_honours_generation_and_audit_purposes(tmp_path):
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'resolve.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope_id, lifecycle = await _build(runtime, "resolve")
            citation = await _memory(runtime, scope_id, "audited evidence")
            await lifecycle.register_memory("audited", citation, parents=("rootA",), policy="all")
            await lifecycle.apply(_event("rootA", 1, False))
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("audited", purpose="generation")
            assert await lifecycle.resolve("audited", purpose="audit") == citation
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("unknown", purpose="audit")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("audited", purpose="unknown")
            await lifecycle.apply(_event("rootA", 2, True))
            assert await lifecycle.resolve("audited", purpose="generation") == citation

    asyncio.run(scenario())


def test_scope_isolation_keeps_identical_node_ids_apart(tmp_path):
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'isolation.db'}"))
        async with open_builtin_runtime(config) as runtime:
            first_scope, first = await _build(runtime, "isolation-one")
            assert first_scope
            second_scope = await _scope(runtime, "isolation-two")
            second = runtime.lifecycle.for_scope(second_scope.scope_id)
            await second.register_source("rootA", await _source(runtime, second_scope.scope_id, "rootA"), revision=REVISION)
            await first.apply(_event("rootA", 1, False))
            assert await second.apply(_event("rootA", 1, False)) == []
            assert await first.apply(_event("rootA", 2, True)) == ["allfact", "anyfact", "rootA", "rootB", "topic"]
            assert await second.apply(_event("rootA", 2, True)) == ["rootA"]

    asyncio.run(scenario())


def test_state_survives_a_restart(tmp_path):
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'restart.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope_id, lifecycle = await _build(runtime, "restart")
            await lifecycle.apply(_event("rootA", 1, False))
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope_id)
            assert await lifecycle.apply(_event("rootA", 1, False)) == ["anyfact", "rootB"]
            assert await lifecycle.apply(_event("rootA", 2, True)) == ["allfact", "anyfact", "rootA", "rootB", "topic"]

    asyncio.run(scenario())


def test_interrupted_propagation_completes_when_the_same_event_is_retried(tmp_path, monkeypatch):
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}"))
        original_forget = MemoryService.forget
        calls = {"count": 0}

        async def flaky_forget(self, memory, *, entries, reason=None):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("native memory write failed")  # noqa: TRY003
            return await original_forget(self, memory, entries=entries, reason=reason)

        async with open_builtin_runtime(config) as runtime:
            scope_id, lifecycle = await _build(runtime, "retry")
            monkeypatch.setattr(MemoryService, "forget", flaky_forget)
            with pytest.raises(RuntimeError):
                await lifecycle.apply(_event("rootA", 1, False))
            monkeypatch.setattr(MemoryService, "forget", original_forget)
            assert await lifecycle.apply(_event("rootA", 1, False)) == ["anyfact", "rootB"]
            assert await _states(runtime, scope_id) == {"allfact": "inactive", "anyfact": "active"}
            page = await runtime.memory.for_scope(scope_id).search(SearchMemoryRequest(query="allfact", mode="fts", limit=10))
            assert page.hits == ()

    asyncio.run(scenario())


def test_unreadable_references_are_rejected(tmp_path):
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'unreadable.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope_id, lifecycle = await _build(runtime, "unreadable")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source(
                    "ghost",
                    SourceRef(source_type="content", source_id="ghost"),
                    revision=REVISION,
                )
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory(
                    "ghostmemory",
                    MemoryCitation(
                        memory_ref=ArtifactRef(family="memory", artifact_id="memory", revision=99),
                        entry_id="ghost",
                        entry_version_id="ghost",
                    ),
                    parents=("rootA",),
                    policy="all",
                )
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_topic(
                    "ghosttopic",
                    ArtifactRef(family="topic-memory", artifact_id="ghost", revision=1),
                    parents=("rootA",),
                    policy="all",
                )

    asyncio.run(scenario())


def test_event_model_rejects_extra_and_invalid_fields():
    with pytest.raises(ValidationError):
        SourceLifecycleEvent(node_id="x", revision=0, sequence=0, available=False, reason="", extra=True)
    with pytest.raises(ValidationError):
        SourceLifecycleEvent(node_id="   ", revision=1, sequence=1, available=True, reason="reason")
    with pytest.raises(ValidationError):
        SourceLifecycleEvent(node_id="x", revision=1, sequence=1, available=True, reason="   ")
