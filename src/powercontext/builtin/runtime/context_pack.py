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

"""Context Pack: a chunked, checksummed context payload plus its incremental delta.

A *Context Pack* is the complete wire form of one prepared context. It carries the
chunks of the prepared body, a SHA-256 digest over its own canonical ``schema`` and
``items``, and the exact byte count of its own serialization. A *Context Pack Delta*
carries the reusable prefix of a previous Pack and the suffix that completes a new
one, so a receiver holding the base Pack can rebuild the target Pack from the
serialized bytes alone.

The wire format is an interoperability contract:

* the Pack JSON holds exactly ``schema``, ``items``, ``digest`` and ``byte_count``;
* the Delta JSON holds exactly ``schema``, ``base_digest``, ``target_digest``,
  ``prefix_items`` and ``appended``;
* ``items`` and ``appended`` are arrays of standard Base64 strings on the wire and
  tuples of ``bytes`` in memory;
* JSON is UTF-8 encoded with sorted keys and ``,``/``:`` separators, without extra
  whitespace;
* the Pack digest is ``sha256:<hex>`` over the JSON of ``schema`` and ``items`` only;
* ``byte_count`` equals the length of the serialized Pack, including the field itself.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

CONTEXT_PACK_SCHEMA = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512

_DIGEST_PREFIX = "sha256:"
_DIGEST_HEX_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")
_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})


class ContextPackError(ValueError):
    """Raised when a context pack or delta violates the wire contract."""


@dataclass(frozen=True)
class ContextPack:
    """One complete context payload, split into deterministic chunks."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    def __post_init__(self) -> None:
        items = _normalize_items(self.items)
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "digest", _validate_digest(self.digest, "digest"))
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ContextPackError("byte-count-invalid")
        if self.byte_count != len(self.to_bytes()):
            raise ContextPackError("byte-count-mismatch")
        if self.digest != _compute_digest(items):
            raise ContextPackError("digest-mismatch")

    @classmethod
    def build(cls, items: Iterable[bytes]) -> ContextPack:
        """Build a complete pack from the given chunks."""
        normalized = _normalize_items(items)
        digest = _compute_digest(normalized)
        byte_count = _resolve_byte_count(
            lambda count: {
                "byte_count": count,
                "digest": digest,
                "items": _encode_items(normalized),
                "schema": CONTEXT_PACK_SCHEMA,
            }
        )
        return cls(items=normalized, digest=digest, byte_count=byte_count)

    @classmethod
    def from_content(
        cls,
        content: str | bytes | None,
        *,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> ContextPack:
        """Chunk one prepared body deterministically and pack it.

        The chunks preserve the original bytes, every chunk is valid UTF-8, and no
        chunk exceeds ``chunk_bytes``. The same content and limit always produce the
        same chunks.
        """
        return cls.build(_chunk_content(_encode_content(content), chunk_bytes))

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Decode and validate a serialized complete pack."""
        payload = _load_object(data)
        if set(payload) != _PACK_FIELDS:
            raise ContextPackError("pack-fields-invalid")
        if payload["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("pack-schema-invalid")
        items = _decode_items(payload["items"])
        digest = _validate_digest(payload["digest"], "digest")
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count < 0:
            raise ContextPackError("byte-count-invalid")
        if byte_count != len(_canonical_json(payload)):
            raise ContextPackError("byte-count-mismatch")
        return cls(items=items, digest=digest, byte_count=byte_count)

    def to_bytes(self) -> bytes:
        """Serialize the pack to its canonical wire form."""
        return _canonical_json(
            {
                "byte_count": self.byte_count,
                "digest": self.digest,
                "items": _encode_items(self.items),
                "schema": CONTEXT_PACK_SCHEMA,
            }
        )


@dataclass(frozen=True)
class ContextPackDelta:
    """The reusable prefix of a base pack plus the suffix that completes a target."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_digest", _validate_digest(self.base_digest, "base-digest"))
        object.__setattr__(self, "target_digest", _validate_digest(self.target_digest, "target-digest"))
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("prefix-items-invalid")
        object.__setattr__(self, "appended", _normalize_items(self.appended))

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Describe how ``target`` extends ``base``.

        The retained prefix is the longest run of chunks the two packs share. Any
        valid common prefix is allowed, and the longest one keeps the transfer small.
        """
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("delta-endpoint-invalid")
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        """Decode and validate a serialized delta."""
        payload = _load_object(data)
        if set(payload) != _DELTA_FIELDS:
            raise ContextPackError("delta-fields-invalid")
        if payload["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("delta-schema-invalid")
        base_digest = _validate_digest(payload["base_digest"], "base-digest")
        target_digest = _validate_digest(payload["target_digest"], "target-digest")
        prefix_items = payload["prefix_items"]
        if type(prefix_items) is not int or prefix_items < 0:
            raise ContextPackError("prefix-items-invalid")
        appended = _decode_items(payload["appended"])
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=appended,
        )

    @property
    def byte_count(self) -> int:
        """The exact size of the serialized delta; it is not a wire field."""
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Serialize the delta to its canonical wire form."""
        return _canonical_json(
            {
                "appended": _encode_items(self.appended),
                "base_digest": self.base_digest,
                "prefix_items": self.prefix_items,
                "schema": CONTEXT_PACK_DELTA_SCHEMA,
                "target_digest": self.target_digest,
            }
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from the base pack and one delta."""
    if not isinstance(base, ContextPack):
        raise ContextPackError("base-pack-invalid")
    if not isinstance(delta, ContextPackDelta):
        raise ContextPackError("delta-invalid")
    if base.digest != delta.base_digest:
        raise ContextPackError("base-digest-mismatch")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("prefix-items-out-of-range")
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("target-digest-mismatch")
    return target


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Encode one payload as UTF-8 JSON with sorted keys and no extra whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _compute_digest(items: tuple[bytes, ...]) -> str:
    """Digest the canonical JSON of the schema and the items only."""
    canonical = _canonical_json({"items": _encode_items(items), "schema": CONTEXT_PACK_SCHEMA})
    return _DIGEST_PREFIX + hashlib.sha256(canonical).hexdigest()


def _resolve_byte_count(build: Any) -> int:
    """Find the byte count whose own serialization is exactly that long.

    The count is part of the serialized pack, so the only consistent value is the
    fixed point of the serialization length.
    """
    count = 0
    for _ in range(16):
        length = len(_canonical_json(build(count)))
        if length == count:
            return count
        count = length
    raise ContextPackError("byte-count-unstable")


def _encode_items(items: Iterable[bytes]) -> list[str]:
    """Render chunks as standard Base64 strings."""
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: Any) -> tuple[bytes, ...]:
    """Decode standard Base64 strings into chunks, rejecting malformed input."""
    if not isinstance(value, list):
        raise ContextPackError("items-not-a-list")
    decoded: list[bytes] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextPackError("item-not-a-string")
        try:
            raw = base64.b64decode(entry, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ContextPackError("item-not-base64") from error
        if base64.b64encode(raw).decode("ascii") != entry:
            raise ContextPackError("item-not-canonical-base64")
        decoded.append(raw)
    return tuple(decoded)


def _normalize_items(items: Iterable[bytes]) -> tuple[bytes, ...]:
    """Freeze the chunks into a tuple and reject anything that is not valid UTF-8."""
    if isinstance(items, (str, bytes, bytearray, memoryview)):
        raise ContextPackError("items-not-a-sequence")
    normalized: list[bytes] = []
    for item in items:
        if not isinstance(item, bytes):
            raise ContextPackError("item-not-bytes")
        try:
            item.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextPackError("item-not-utf8") from error
        normalized.append(item)
    return tuple(normalized)


def _validate_digest(value: Any, field: str) -> str:
    """Accept only a ``sha256:<hex>`` digest."""
    if not isinstance(value, str) or not value.startswith(_DIGEST_PREFIX):
        raise ContextPackError(f"{field}-malformed")
    hexdigest = value[len(_DIGEST_PREFIX) :]
    if len(hexdigest) != _DIGEST_HEX_LENGTH or not set(hexdigest) <= _HEX_DIGITS:
        raise ContextPackError(f"{field}-malformed")
    return value


def _load_object(data: bytes) -> dict[str, Any]:
    """Decode one JSON object, rejecting any other input."""
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise ContextPackError("payload-not-bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextPackError("payload-not-utf8") from error
    try:
        payload = json.loads(text)
    except ValueError as error:
        raise ContextPackError("payload-not-json") from error
    if not isinstance(payload, dict):
        raise ContextPackError("payload-not-object")
    return payload


def _encode_content(content: str | bytes | None) -> bytes:
    """Render the prepared body as the bytes the chunks preserve."""
    if content is None:
        return b""
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, (bytes, bytearray, memoryview)):
        return bytes(content)
    raise ContextPackError("content-type-invalid")


def _chunk_content(data: bytes, chunk_bytes: int) -> tuple[bytes, ...]:
    """Split one body into chunks that are valid UTF-8 and within the byte limit."""
    if type(chunk_bytes) is not int or chunk_bytes <= 0:
        raise ContextPackError("chunk-bytes-invalid")
    chunks: list[bytes] = []
    position = 0
    total = len(data)
    while position < total:
        end = min(position + chunk_bytes, total)
        while end > position and not _is_utf8(data[position:end]):
            end -= 1
        if end <= position:
            raise ContextPackError("chunk-boundary-invalid")
        chunks.append(data[position:end])
        position = end
    return tuple(chunks)


def _is_utf8(raw: bytes) -> bool:
    """Report whether one byte range is a complete UTF-8 sequence."""
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


__all__ = [
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "DEFAULT_CHUNK_BYTES",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
