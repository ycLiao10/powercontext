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

"""Pi session branch capture with exact event identity and resumable history."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast
from urllib.parse import quote

from pydantic import BaseModel, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_EVENT_SOURCE_VERSION = "1"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"
PI_SESSION_FORMAT_VERSION = 3


class PiSessionInput(BaseModel):
    """One offline Pi session archive and the branch leaf to select."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event captured from a Pi session branch."""

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """Captured Pi session event that can be used as Artifact evidence."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Return the stable identity of one event inside one Pi session.

    The identity depends only on the session id and the event id, so the same
    event keeps one reference across branch switches, appends and restarts.
    Both parts are percent-encoded, which keeps the separator unambiguous and
    the result inside the shared source-id length limit.
    """

    encoded = f"{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(PI_EVENT_SOURCE_NAME) + 1 + len(encoded) <= MAX_SOURCE_ID_LENGTH:
        return f"{PI_EVENT_SOURCE_NAME}:{encoded}"
    digest = hashlib.sha256(f"{session_id}\x00{event_id}".encode()).hexdigest()
    return f"{PI_EVENT_SOURCE_NAME}:{digest}"


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Parse one Pi session archive into the visible events of the selected branch.

    The whole archive is validated before any event is returned, so a malformed
    archive never yields a partial result.
    """

    return _parse_session(value)[1]


def _parse_session(value: PiSessionInput) -> tuple[str, tuple[PiEventInput, ...]]:
    """Return the session id and the visible events of the selected branch."""

    records = _read_records(value.content)
    session_id, nodes = _read_graph(records)
    chain = _ancestor_chain(nodes, _select_leaf(nodes, value.leaf_id))
    events = tuple(
        event for record in chain if (event := _visible_event(session_id, record)) is not None
    )
    return session_id, events


class PiEventSourceAdapter:
    """Resolve and read the runtime's captured Pi session event Source."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one captured event without coupling consumers to ``PiEventSource``."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version=PI_EVENT_SOURCE_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)


class PiSessionConnector:
    """Import the visible events of one Pi session branch without duplicates.

    The Connector re-parses the whole archive on every run and submits only the
    visible events of the selected branch that were not imported yet. Progress
    travels through the ConnectorLifecycle checkpoint, so a restart, a branch
    switch and an appended archive all resume from the committed progress.
    """

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        session_id, events = _parse_session(
            PiSessionInput(content=self._read_archive(), leaf_id=self._leaf_id)
        )
        imported = _imported_events(session.checkpoint)
        checksums = {event.event_id: _record_checksum(event.record) for event in events}
        for event in events:
            known = imported.get(event.event_id)
            if known is not None and known != checksums[event.event_id]:
                raise ValueError(  # noqa: TRY003
                    f"Pi session event {event.event_id!r} was rewritten after it was imported"
                )
        for event in events:
            if event.event_id in imported:
                continue
            item_id = pi_event_source_id(event.session_id, event.event_id)
            await session.submit(item_id, PI_EVENT_SOURCE_NAME, event)
        merged = {**imported, **checksums}
        checkpoint = {
            "version": PI_EVENT_SOURCE_VERSION,
            "session_id": session_id,
            "events": {
                event_id: {"checksum": checksum} for event_id, checksum in sorted(merged.items())
            },
        }
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=checkpoint)

    def _read_archive(self) -> bytes:
        return self._path.read_bytes()


def _read_records(content: bytes) -> list[dict[str, JsonValue]]:
    """Decode one JSONL archive into its records."""

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session archive must be valid UTF-8") from error
    records: list[dict[str, JsonValue]] = []
    for number, line in enumerate(text.split("\n"), start=1):
        stripped = line.rstrip("\r")
        if not stripped.strip():
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise ValueError(f"Pi session archive line {number} is not valid JSON") from error
        if not isinstance(record, dict):
            raise ValueError(f"Pi session archive line {number} must be a JSON object")
        records.append(cast("dict[str, JsonValue]", record))
    if not records:
        raise ValueError("Pi session archive must contain a session header")
    return records


def _read_graph(records: list[dict[str, JsonValue]]) -> tuple[str, dict[str, dict[str, JsonValue]]]:
    """Validate the session header and the parent links of every record."""

    header = records[0]
    if header.get("type") != "session":
        raise ValueError("Pi session archive must start with a session header")  # noqa: TRY003
    if header.get("version") != PI_SESSION_FORMAT_VERSION:
        raise ValueError(  # noqa: TRY003
            f"Pi session archive must use version {PI_SESSION_FORMAT_VERSION}"
        )
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Pi session header must declare a non-empty string id")  # noqa: TRY003
    nodes: dict[str, dict[str, JsonValue]] = {}
    for record in records[1:]:
        record_id = _record_id(record)
        if record_id in nodes:
            raise ValueError(f"Pi session event id {record_id!r} appears more than once")  # noqa: TRY003
        parent_id = _record_parent(record)
        if parent_id is not None and parent_id not in nodes:
            raise ValueError(  # noqa: TRY003
                f"Pi session event {record_id!r} references the unknown parent {parent_id!r}"
            )
        nodes[record_id] = record
    return session_id, nodes


def _select_leaf(nodes: dict[str, dict[str, JsonValue]], leaf_id: str | None) -> str | None:
    """Return the selected leaf, defaulting to the last recorded node."""

    if leaf_id is not None:
        if leaf_id not in nodes:
            raise ValueError(f"Pi session archive does not contain the leaf event {leaf_id!r}")  # noqa: TRY003
        return leaf_id
    if not nodes:
        return None
    return next(reversed(nodes))


def _ancestor_chain(
    nodes: dict[str, dict[str, JsonValue]],
    leaf_id: str | None,
) -> list[dict[str, JsonValue]]:
    """Return the ancestor chain of the selected leaf, ordered from root to leaf."""

    chain: list[dict[str, JsonValue]] = []
    visited: set[str] = set()
    current = leaf_id
    while current is not None:
        if current in visited:
            raise ValueError(f"Pi session history of {current!r} contains a cycle")  # noqa: TRY003
        visited.add(current)
        record = nodes[current]
        chain.append(record)
        current = _record_parent(record)
    chain.reverse()
    return chain


def _record_id(record: dict[str, JsonValue]) -> str:
    """Return the unique id of one record."""

    if "id" not in record:
        raise ValueError("Pi session records must declare an id")  # noqa: TRY003
    record_id = record["id"]
    if not isinstance(record_id, str) or not record_id:
        raise ValueError("Pi session record id must be a non-empty string")  # noqa: TRY003
    return record_id


def _record_parent(record: dict[str, JsonValue]) -> str | None:
    """Return the parent of one record, or ``None`` for a branch root."""

    if "parentId" not in record:
        raise ValueError("Pi session records must declare a parentId")  # noqa: TRY003
    parent_id = record["parentId"]
    if parent_id is None:
        return None
    if not isinstance(parent_id, str):
        raise ValueError("Pi session record parentId must be empty or a node id")  # noqa: TRY003
    return parent_id or None


def _visible_event(session_id: str, record: dict[str, JsonValue]) -> PiEventInput | None:
    """Return the visible event of one record, or ``None`` for a control node."""

    record_type = record.get("type")
    if not isinstance(record_type, str) or not record_type:
        return None
    body: str
    if record_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        if role == "toolResult":
            body = _tool_result_body(message)
        elif role == "bashExecution":
            body = _bash_body(message)
        else:
            body = _visible_body(message.get("content"))
    elif record_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        if not isinstance(summary, str):
            return None
        role = record_type
        body = summary
    elif record_type == "custom_message":
        role = "custom"
        body = _visible_body(record.get("content"))
    else:
        return None
    if not body:
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=_record_id(record),
        parent_id=_record_parent(record),
        event_type=record_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _visible_body(content: JsonValue) -> str:
    """Return the visible text of one message body."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type == "toolCall":
            parts.append(_tool_call_body(block))
    return "\n".join(parts)


def _tool_call_body(block: dict[str, JsonValue]) -> str:
    """Return the visible body of one tool call block."""

    prefix = f"[tool_call {_text(block.get('name'))} {_text(block.get('id'))}]"
    return f"{prefix} {_compact_arguments(block.get('arguments'))}"


def _tool_result_body(message: dict[str, JsonValue]) -> str:
    """Return the visible body of one tool result message."""

    prefix = f"[tool_result {_text(message.get('toolName'))} {_text(message.get('toolCallId'))}]"
    text = _visible_body(message.get("content"))
    return f"{prefix} {text}" if text else prefix


def _bash_body(message: dict[str, JsonValue]) -> str:
    """Return the visible body of one bash execution message."""

    command = message.get("command")
    output = message.get("output")
    parts: list[str] = []
    if isinstance(command, str) and command:
        parts.append(f"$ {command}")
    if isinstance(output, str) and output:
        parts.append(output)
    return "\n".join(parts)


def _compact_arguments(arguments: JsonValue) -> str:
    """Return the tool call arguments as compact sorted JSON."""

    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = cast("JsonValue", json.loads(arguments))
        except json.JSONDecodeError:
            return arguments
    try:
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def _text(value: JsonValue) -> str:
    """Return one value as text, without inventing a placeholder."""

    return value if isinstance(value, str) else ""


def _record_checksum(record: dict[str, JsonValue]) -> str:
    """Return a checksum of the complete original record."""

    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _imported_events(checkpoint: JsonValue | None) -> dict[str, str]:
    """Return the imported event identities and their record checksums."""

    if not isinstance(checkpoint, dict):
        return {}
    events = checkpoint.get("events")
    if not isinstance(events, dict):
        return {}
    imported: dict[str, str] = {}
    for event_id, entry in events.items():
        if not isinstance(entry, dict):
            continue
        checksum = entry.get("checksum")
        if isinstance(event_id, str) and isinstance(checksum, str):
            imported[event_id] = checksum
    return imported


__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_EVENT_SOURCE_VERSION",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PI_SESSION_FORMAT_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
