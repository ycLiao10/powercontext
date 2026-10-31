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

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, PrepareContextRequest, open_builtin_runtime
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
    infer_chunk_size,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource


class _ContentCandidatePipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(kind="conversation-turn", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


def test_pack_wire_payload_matches_the_documented_encoding() -> None:
    items = (b"alpha\n", "海洋".encode())
    unsigned = {
        "schema": "powercontext.context-pack.v1",
        "items": [base64.b64encode(item).decode("ascii") for item in items],
    }
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()

    pack = ContextPack.build(items)
    payload = json.loads(pack.to_bytes())

    assert payload == {
        **unsigned,
        "digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "byte_count": len(pack.to_bytes()),
    }
    assert pack.to_bytes() == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert pack.byte_count == len(pack.to_bytes())
    assert ContextPack.from_bytes(pack.to_bytes()) == pack


def test_pack_rejects_a_missing_or_unexpected_field() -> None:
    pack = ContextPack.build((b"alpha",))
    payload = json.loads(pack.to_bytes())

    for field in payload:
        incomplete = {key: value for key, value in payload.items() if key != field}
        with pytest.raises(ContextPackError):
            ContextPack.from_bytes(json.dumps(incomplete).encode())

    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "extra": 1}).encode())


def test_pack_rejects_a_tampered_digest_or_byte_count() -> None:
    pack = ContextPack.build((b"alpha",))

    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(pack.to_bytes().replace(pack.digest.encode(), b"sha256:" + b"0" * 64))

    payload = json.loads(pack.to_bytes())
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "byte_count": payload["byte_count"] + 1}).encode())


def test_chunks_are_bounded_valid_utf8_and_reassemble_the_body() -> None:
    body = "alpha 海洋 beta"

    pack = ContextPack.from_content(body, chunk_bytes=7)

    assert b"".join(pack.items).decode("utf-8") == body
    assert all(len(item) <= 7 for item in pack.items)
    assert all(item.decode("utf-8") for item in pack.items)
    assert ContextPack.from_content(body, chunk_bytes=7).items == pack.items


def test_chunking_fails_when_no_valid_boundary_fits() -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content("海", chunk_bytes=2)

    with pytest.raises(ContextPackError):
        ContextPack.from_content("body", chunk_bytes=0)


def test_delta_rebuilds_the_target_from_the_base_and_the_appended_suffix() -> None:
    base = ContextPack.build((b"alpha", b"beta"))
    target = ContextPack.build((b"alpha", b"beta", b"gamma"))

    delta = ContextPackDelta.between(base, target)
    rebuilt = apply_context_pack_delta(
        ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
    )

    assert rebuilt.to_bytes() == target.to_bytes()
    assert delta.byte_count == len(delta.to_bytes())
    assert delta.base_digest == base.digest
    assert delta.target_digest == target.digest


@pytest.mark.parametrize(
    "target_items", [(b"a", b"b", b"new", b"tail"), (b"new", b"b", b"old"), (b"a", b"new", b"old"), ()]
)
def test_delta_reconstructs_a_change_at_any_position(target_items: tuple[bytes, ...]) -> None:
    base = ContextPack.build((b"a", b"b", b"old"))
    target = ContextPack.build(target_items)

    delta = ContextPackDelta.between(base, target)
    rebuilt = apply_context_pack_delta(
        ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
    )

    assert rebuilt.to_bytes() == target.to_bytes()


def test_delta_rejects_a_wrong_base_a_wrong_target_and_an_impossible_prefix() -> None:
    base = ContextPack.build((b"one",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"one", b"two")))

    with pytest.raises(ContextPackError):
        apply_context_pack_delta(ContextPack.build((b"other",)), delta)

    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, base.digest, 5, ()))

    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, "sha256:" + "0" * 64, 0, (b"x",)))


@pytest.mark.parametrize("prefix", [True, 1.5, -1])
def test_delta_rejects_a_prefix_that_is_not_a_non_negative_integer(prefix: object) -> None:
    delta = ContextPackDelta.between(ContextPack.build((b"alpha",)), ContextPack.build((b"alpha", b"beta")))
    payload = json.loads(delta.to_bytes())
    payload["prefix_items"] = prefix

    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def test_delta_wire_payload_carries_only_the_contract_fields() -> None:
    base = ContextPack.build((b"alpha",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"alpha", b"beta")))
    payload = json.loads(delta.to_bytes())

    assert set(payload) == {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
    assert payload["schema"] == "powercontext.context-pack-delta.v1"
    assert payload["appended"] == [base64.b64encode(b"beta").decode("ascii")]
    assert "byte_count" not in payload


def test_empty_pack_is_valid_and_round_trips() -> None:
    empty = ContextPack.build(())

    assert ContextPack.from_bytes(empty.to_bytes()) == empty
    assert b"".join(empty.items) == b""


def test_runtime_prepare_incremental_rebuilds_the_plain_prepare_body() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_ContentCandidatePipeline()) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Incremental", summary="Runtime integration", idempotency_key="context-pack-test")
            )
            sources = runtime.sources.for_scope(scope.scope_id)
            memory = runtime.memory.for_scope(scope.scope_id)
            context = runtime.context.for_scope(scope.scope_id)
            request = PrepareContextRequest(query="remote control", max_bytes=4096)

            await sources.capture(
                CaptureSource(source_id="turn-1", content="A remote control for the stereo.", metadata={})
            )
            while (await memory.flush()).processed:
                pass

            first = await context.prepare_incremental(request)
            assert isinstance(first, ContextPack)
            assert b"".join(first.items).decode("utf-8")

            await sources.capture(
                CaptureSource(source_id="turn-2", content="Another remote control discussion.", metadata={})
            )
            while (await memory.flush()).processed:
                pass

            second = await context.prepare_incremental(request, base_pack=first)
            assert isinstance(second, ContextPackDelta)
            expected = await context.prepare(request)

            rebuilt = apply_context_pack_delta(
                ContextPack.from_bytes(first.to_bytes()), ContextPackDelta.from_bytes(second.to_bytes())
            )

            assert b"".join(rebuilt.items).decode("utf-8") == expected.content
            assert expected.content is not None

    asyncio.run(scenario())


def test_infer_chunk_size_recognises_the_chunking_of_a_base_pack() -> None:
    body = "x" * 1000

    assert infer_chunk_size(ContextPack.from_content(body, chunk_bytes=64)) == 64
    assert infer_chunk_size(ContextPack.from_content(body, chunk_bytes=512)) == 512
    assert infer_chunk_size(ContextPack.build(())) is not None
    assert infer_chunk_size(ContextPack.build((b"\xff\xfe",))) is None


def test_runtime_prepare_incremental_matches_the_base_pack_chunking() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_ContentCandidatePipeline()) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Chunking", summary="Base chunking", idempotency_key="context-pack-chunking")
            )
            sources = runtime.sources.for_scope(scope.scope_id)
            memory = runtime.memory.for_scope(scope.scope_id)
            context = runtime.context.for_scope(scope.scope_id)
            request = PrepareContextRequest(query="remote control", max_bytes=4096)

            await sources.capture(
                CaptureSource(source_id="turn-1", content="A remote control for the stereo.", metadata={})
            )
            while (await memory.flush()).processed:
                pass
            first = await context.prepare(request)
            assert first.content is not None

            # A receiver that rebuilt the base with the documented default chunking still gets a
            # delta that applies to the pack it actually holds.
            base = ContextPack.from_content(first.content)
            await sources.capture(
                CaptureSource(source_id="turn-2", content="Another remote control discussion.", metadata={})
            )
            while (await memory.flush()).processed:
                pass

            delta = await context.prepare_incremental(request, base_pack=base)
            assert isinstance(delta, ContextPackDelta)
            expected = await context.prepare(request)

            rebuilt = apply_context_pack_delta(base, ContextPackDelta.from_bytes(delta.to_bytes()))

            assert b"".join(rebuilt.items).decode("utf-8") == expected.content

    asyncio.run(scenario())


def test_runtime_prepare_incremental_returns_an_empty_pack_for_an_empty_result() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_ContentCandidatePipeline()) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Empty", summary="Empty result", idempotency_key="context-pack-empty")
            )
            context = runtime.context.for_scope(scope.scope_id)

            pack = await context.prepare_incremental(PrepareContextRequest(query="nothing matches this term"))

            assert isinstance(pack, ContextPack)
            assert pack.items == ()
            assert b"".join(pack.items) == b""

    asyncio.run(scenario())
