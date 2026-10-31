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

"""Context Pack wire protocol for incremental context transfer.

A :class:`ContextPack` is the transport form of one prepared context body: the body is split
into deterministic byte chunks, each chunk is carried as standard Base64, and the whole pack is
sealed with a SHA-256 digest of its canonical ``schema``/``items`` JSON.

A :class:`ContextPackDelta` carries only the difference between two packs. It names the base pack
it was computed against, states how many leading chunks of that base the receiver may keep, and
carries the target's remaining chunks. The receiver therefore rebuilds the target pack from the
base pack it already holds plus the delta bytes alone, without any sender-side state.

The wire format is an interoperability contract, so every field name, encoding rule, and digest
definition here is fixed. Changing any of them changes the schema identifier.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

PACK_SCHEMA: Final = "powercontext.context-pack.v1"
DELTA_SCHEMA: Final = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES: Final = 512
INCREMENTAL_CHUNK_BYTES: Final = 128
"""Chunk size used for the packs this runtime sends.

A delta may only keep a *prefix* of the base pack's chunks, so the reusable bytes stop at the
first byte that changed. A prepared body changes early whenever the Memory revision moves in a
citation, so wide chunks would throw away everything after that point. Narrower chunks keep more
of the body reusable, and the extra Base64 and JSON cost per chunk stays small compared with the
bytes a wide chunk would resend.
"""

_CHUNK_SIZE_CANDIDATES: Final = (INCREMENTAL_CHUNK_BYTES, 64, 96, 192, 256, 512, 1024)

_PACK_FIELDS: Final = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS: Final = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})
_DIGEST_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")


class ContextPackError(Exception):
    """Raised when a pack or delta violates the Context Pack wire contract."""


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    """Encode one protocol object exactly as the contract defines it."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_items(items: Sequence[bytes]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: object) -> tuple[bytes, ...]:
    """Decode a wire ``items``/``appended`` array, rejecting anything off-contract."""

    if not isinstance(value, list):
        raise ContextPackError("pack-items-not-an-array")
    decoded: list[bytes] = []
    for element in value:
        if not isinstance(element, str):
            raise ContextPackError("pack-item-not-text")
        try:
            raw = base64.b64decode(element.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError, ValueError) as error:
            raise ContextPackError("pack-item-not-base64") from error
        if base64.b64encode(raw).decode("ascii") != element:
            raise ContextPackError("pack-item-not-canonical-base64")
        decoded.append(raw)
    return tuple(decoded)


def _require_fields(payload: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    present = set(payload)
    missing = sorted(expected - present)
    if missing:
        raise ContextPackError(f"{label}-missing-field:{','.join(missing)}")
    unexpected = sorted(present - expected)
    if unexpected:
        raise ContextPackError(f"{label}-unexpected-field:{','.join(unexpected)}")


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_PATTERN.match(value):
        raise ContextPackError(f"{label}-not-a-digest")
    return value


def _require_prefix(value: object) -> int:
    # ``bool`` is an ``int`` subclass, and a float is not an integer at all.
    if type(value) is not int or value < 0:
        raise ContextPackError("delta-prefix-not-a-non-negative-integer")
    return value


def _load_payload(data: object, label: str) -> Mapping[str, object]:
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
    elif isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raise ContextPackError(f"{label}-not-bytes")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ContextPackError(f"{label}-not-json") from error
    if not isinstance(payload, dict):
        raise ContextPackError(f"{label}-not-an-object")
    return payload


def _pack_digest(items: Sequence[str]) -> str:
    """Digest the canonical ``schema``/``items`` JSON of one pack."""

    canonical = _canonical_json({"schema": PACK_SCHEMA, "items": list(items)})
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class ContextPack:
    """One complete prepared context body in transferable chunks."""

    items: tuple[bytes, ...]
    digest: str = field(init=False)
    byte_count: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        encoded = _encode_items(self.items)
        object.__setattr__(self, "digest", _pack_digest(encoded))
        object.__setattr__(self, "byte_count", len(self._encode()))

    @classmethod
    def build(cls, items: Iterable[bytes]) -> ContextPack:
        """Build a complete pack from chunk bytes."""

        if isinstance(items, (str, bytes, bytearray)):
            raise ContextPackError("pack-items-not-a-chunk-sequence")
        chunks: list[bytes] = []
        for item in items:
            if not isinstance(item, (bytes, bytearray)):
                raise ContextPackError("pack-item-not-bytes")
            chunks.append(bytes(item))
        return cls(tuple(chunks))

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Split one prepared body into deterministic chunks.

        Chunks never split a UTF-8 character and never exceed ``chunk_bytes``. The chunk
        boundaries depend only on the leading bytes of the body, so two bodies that share a
        leading byte range also share their leading chunks. That is what lets a delta keep a
        prefix of the previous pack.
        """

        if not isinstance(content, str):
            raise ContextPackError("pack-content-not-text")
        if type(chunk_bytes) is not int or chunk_bytes < 1:
            raise ContextPackError("pack-chunk-bytes-not-positive")
        data = content.encode("utf-8")
        total = len(data)
        if total == 0:
            return cls(())
        chunks: list[bytes] = []
        start = 0
        while start < total:
            end = min(start + chunk_bytes, total)
            if end < total:
                # Back off to the nearest character boundary: a continuation byte means the
                # character it belongs to starts before ``end`` and would be split.
                while end > start and (data[end] & 0xC0) == 0x80:
                    end -= 1
                if end == start:
                    raise ContextPackError("pack-chunk-boundary-impossible")
            chunks.append(data[start:end])
            start = end
        return cls(tuple(chunks))

    def to_bytes(self) -> bytes:
        """Serialize the pack, including its own byte count."""

        return self._encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Decode and verify one complete pack."""

        payload = _load_payload(data, "pack")
        _require_fields(payload, _PACK_FIELDS, "pack")
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("pack-unknown-schema")
        items = _decode_items(payload["items"])
        digest = _require_digest(payload["digest"], "pack")
        expected_digest = _pack_digest(_encode_items(items))
        if digest != expected_digest:
            raise ContextPackError("pack-digest-mismatch")
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count < 0:
            raise ContextPackError("pack-byte-count-not-an-integer")
        pack = cls(items)
        if pack.byte_count != byte_count:
            raise ContextPackError("pack-byte-count-mismatch")
        return pack

    def _encode(self) -> bytes:
        encoded = _encode_items(self.items)
        payload: dict[str, object] = {
            "schema": PACK_SCHEMA,
            "items": encoded,
            "digest": self.digest,
            "byte_count": 0,
        }
        # ``byte_count`` counts itself, so its own digits move the encoded length. Settle on the
        # fixed point where the declared count equals the length it produces.
        count = len(_canonical_json(payload))
        for _ in range(8):
            payload["byte_count"] = count
            actual = len(_canonical_json(payload))
            if actual == count:
                break
            count = actual
        else:  # pragma: no cover - the fixed point is reached within one or two steps
            raise ContextPackError("pack-byte-count-not-stable")
        return _canonical_json(payload)


@dataclass(frozen=True)
class ContextPackDelta:
    """The difference between a base pack and the target pack it should become."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_digest", _require_digest(self.base_digest, "delta-base"))
        object.__setattr__(self, "target_digest", _require_digest(self.target_digest, "delta-target"))
        object.__setattr__(self, "prefix_items", _require_prefix(self.prefix_items))
        if isinstance(self.appended, (str, bytes, bytearray)):
            raise ContextPackError("delta-appended-not-a-chunk-sequence")
        chunks: list[bytes] = []
        for item in self.appended:
            if not isinstance(item, (bytes, bytearray)):
                raise ContextPackError("delta-appended-item-not-bytes")
            chunks.append(bytes(item))
        object.__setattr__(self, "appended", tuple(chunks))

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Describe how to turn ``base`` into ``target``.

        The longest shared leading chunk run is kept, so the delta carries only the chunks the
        receiver is missing.
        """

        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("delta-requires-context-packs")
        limit = min(len(base.items), len(target.items))
        prefix = 0
        while prefix < limit and base.items[prefix] == target.items[prefix]:
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        """Bytes this delta occupies on the wire."""

        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Serialize the delta."""

        payload = {
            "schema": DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        }
        return _canonical_json(payload)

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        """Decode and verify one delta."""

        payload = _load_payload(data, "delta")
        _require_fields(payload, _DELTA_FIELDS, "delta")
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("delta-unknown-schema")
        base_digest = _require_digest(payload["base_digest"], "delta-base")
        target_digest = _require_digest(payload["target_digest"], "delta-target")
        prefix_items = _require_prefix(payload["prefix_items"])
        appended = _decode_items(payload["appended"])
        return cls(base_digest, target_digest, prefix_items, appended)


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from the base pack the receiver holds and one delta."""

    if not isinstance(base, ContextPack):
        raise ContextPackError("delta-base-not-a-context-pack")
    if not isinstance(delta, ContextPackDelta):
        raise ContextPackError("delta-not-a-context-pack-delta")
    if delta.base_digest != base.digest:
        raise ContextPackError("delta-base-digest-mismatch")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta-prefix-out-of-range")
    target = ContextPack.build((*base.items[: delta.prefix_items], *delta.appended))
    if target.digest != delta.target_digest:
        raise ContextPackError("delta-target-digest-mismatch")
    return target


def infer_chunk_size(base: ContextPack) -> int | None:
    """Return the chunk size that reproduces ``base`` exactly, when it is a known one.

    A delta can only keep a prefix of the base pack's chunks, so the target pack has to be
    chunked the same way as the base. Rebuilding the base body and re-chunking it identifies the
    size that produced it; ``None`` means the base was chunked in a way this runtime does not
    use, and the caller has to fall back to its own default.
    """

    try:
        body = b"".join(base.items).decode("utf-8")
    except UnicodeDecodeError:
        return None
    for candidate in _CHUNK_SIZE_CANDIDATES:
        if ContextPack.from_content(body, chunk_bytes=candidate).items == base.items:
            return candidate
    return None
