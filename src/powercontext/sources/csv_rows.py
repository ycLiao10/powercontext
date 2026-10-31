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

"""Row-level CSV capture with stable per-record identity and exact lookup."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"
CSV_ROW_SOURCE_VERSION = "1"
CSV_ROW_IDENTITY_PREFIX = "csv"


class CSVDocumentInput(BaseModel):
    """One offline CSV document addressed by a stable primary key column."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One captured CSV row keeping the original column order and values."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """Captured CSV row that can be used as Artifact evidence."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Return the stable identity of one row inside one document.

    Both components are percent-encoded so that the ``:`` separator can never
    collide with document content, and the result stays inside the shared
    source-id length limit.
    """

    _require_document_id(document_id)
    if not primary_key_value or not primary_key_value.strip():
        raise ValueError("primary key value must be a non-empty string")  # noqa: TRY003
    source_id = f"{CSV_ROW_IDENTITY_PREFIX}:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"CSV row source id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse one offline CSV document into its validated rows.

    The whole document is parsed and validated before any row is returned, so
    a malformed input never yields a partial result.
    """

    document_id = value.document_id
    _require_document_id(document_id)
    primary_key = value.primary_key
    if not primary_key or not primary_key.strip():
        raise ValueError("primary key must be a non-empty string")  # noqa: TRY003

    records = _read_records(_decode_csv(value.content))
    if not records:
        raise ValueError("CSV document must contain a header record")  # noqa: TRY003
    header = records[0]
    _require_header(header)
    if primary_key not in header:
        raise ValueError("primary key must match one CSV header column exactly")  # noqa: TRY003

    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(header):
            raise ValueError("CSV record width must match the header width")  # noqa: TRY003
        primary_key_value = record[header.index(primary_key)]
        if not primary_key_value or not primary_key_value.strip():
            raise ValueError("CSV primary key values must not be blank")  # noqa: TRY003
        if primary_key_value in seen_keys:
            raise ValueError("CSV primary key values must be unique inside one document")  # noqa: TRY003
        seen_keys.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=document_id,
                primary_key=primary_key,
                primary_key_value=primary_key_value,
                columns=tuple(header),
                values=tuple(record),
            )
        )
    return tuple(rows)


class CSVRowSourceAdapter:
    """Resolve and read the runtime's captured CSV row Source."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one captured row without coupling consumers to ``CSVRowSource``."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


def _require_document_id(document_id: str) -> None:
    if not document_id or document_id != document_id.strip():
        raise ValueError("document id must be a non-empty string without surrounding whitespace")  # noqa: TRY003


def _require_header(header: list[str]) -> None:
    if not header:
        raise ValueError("CSV document must contain a header record")  # noqa: TRY003
    seen: set[str] = set()
    for column in header:
        if not column or not column.strip():
            raise ValueError("CSV header columns must be non-empty and not blank")  # noqa: TRY003
        if column in seen:
            raise ValueError("CSV header columns must be unique")  # noqa: TRY003
        seen.add(column)


def _decode_csv(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV document must be valid UTF-8") from error


def _read_records(text: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    records: list[list[str]] = []
    try:
        for record in reader:
            records.append(record)
    except csv.Error as error:
        raise ValueError(f"CSV document is malformed: {error}") from error
    return records


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    version=CSV_ROW_SOURCE_VERSION,
    projections=(CSVRowTextEvidenceProjection(),),
)


__all__ = [
    "CSV_ROW_IDENTITY_PREFIX",
    "CSV_ROW_SOURCE_ADAPTER",
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_NAME",
    "CSV_ROW_SOURCE_VERSION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "CSVRowSourceAdapter",
    "CSVRowTextEvidenceProjection",
    "csv_row_source_id",
    "parse_csv_document",
]
