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

"""Whole-document PDF capture with page-level identity and evidence retrieval."""

from __future__ import annotations

import hashlib
import io
from itertools import combinations
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.artifacts import MemoryCitation
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import pdf_ranking
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"
PDF_PAGE_SOURCE_VERSION = "1"
PDF_PAGE_IDENTITY_PREFIX = "pdf"

# Retrieval budget. A question is expanded into a handful of lexical queries so the
# Memory backend sees each distinctive question term on its own, and every query is
# read deep enough that the candidate pool covers the whole document. The returned
# pages are then re-ranked offline by :mod:`powercontext.sources.pdf_ranking`, which
# is what turns a flat lexical pool into page-accurate evidence.
_SEARCH_FETCH_LIMIT = 200
_MAX_QUERY_TERMS = 10
_MAX_PAIR_QUERIES = 12
_MAX_EXPANSION_QUERIES = 6
_CANDIDATE_MULTIPLIER = 2


class PDFDocumentInput(BaseModel):
    """One complete original PDF document addressed by a caller-stable identity."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of a PDF document with its whole-document digest."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """Captured PDF page that can be used as Artifact evidence."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Return the stable identity of one page inside one document revision.

    The identity is derived from ``(document_id, document_sha256, page_number)``,
    so a revised PDF yields a new identity while references to the previous
    revision keep resolving to it. The document id is percent-encoded so the
    separator can never collide with document content; an over-long document id
    falls back to its digest, which keeps the identity bounded and collision-free.
    """

    _require_document_id(document_id)
    if page_number < 1:
        raise ValueError("PDF page number must be positive")  # noqa: TRY003
    identity = f"{PDF_PAGE_IDENTITY_PREFIX}:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(identity) <= MAX_SOURCE_ID_LENGTH:
        return identity
    digest = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
    bounded = f"{PDF_PAGE_IDENTITY_PREFIX}:{digest}:{document_sha256}:{page_number}"
    if len(bounded) > MAX_SOURCE_ID_LENGTH:  # pragma: no cover - constant bound
        raise ValueError(f"PDF page source id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return bounded


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse one complete PDF document into every physical page, in order.

    The whole document is parsed before any page is returned, so a malformed
    input never yields a partial result. Page numbers start at ``1`` and cover
    every physical page, including pages without extractable text.
    """

    document_id = value.document_id
    _require_document_id(document_id)
    content = value.content
    if not content:
        raise ValueError("PDF document must not be empty")  # noqa: TRY003
    document_sha256 = hashlib.sha256(content).hexdigest()
    texts = _extract_page_texts(content)
    return tuple(
        PDFPageInput(
            document_id=document_id,
            document_sha256=document_sha256,
            page_number=page_number,
            text=text,
        )
        for page_number, text in enumerate(texts, start=1)
    )


class PDFPageSourceAdapter:
    """Resolve and read the runtime's captured PDF page Source."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one captured page without coupling consumers to ``PDFPageSource``."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return exact Memory citations for the pages that best answer ``question``.

    The question is expanded into several lexical queries that are issued through
    the public Memory search API; every query is read deep enough that the pool
    covers the whole document. That pool is then re-ranked offline by page
    structure, because lexical order alone rewards pages that repeat a few common
    words over the one page that carries the answer. Only pages Memory itself
    returned are cited, and only one citation per physical page is reported.
    """

    from powercontext.builtin.runtime import SearchMemoryRequest

    if limit < 1 or not question.strip():
        return ()
    hits: dict[tuple[str, str], Any] = {}
    for query in _build_queries(question):
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=_SEARCH_FETCH_LIMIT))
        for hit in page.hits:
            hits.setdefault((hit.entry_id, hit.entry_version_id), hit)
    if not hits:
        return ()
    by_key = {f"{entry_id}|{entry_version_id}": hit for (entry_id, entry_version_id), hit in hits.items()}
    by_key = _focus_on_named_document(question, by_key)
    pages = [pdf_ranking.prepare(key, hit.text or "") for key, hit in by_key.items()]
    order = pdf_ranking.rank(question, pages, limit=min(len(pages), max(limit, 1) * _CANDIDATE_MULTIPLIER))
    citations: list[MemoryCitation] = []
    cited_pages: set[tuple[str, str]] = set()
    for key, _score in order:
        hit = by_key[key]
        identity = _page_identity(hit.text or "")
        if identity is not None:
            if identity in cited_pages:
                continue
            cited_pages.add(identity)
        citations.append(
            MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
        )
        if len(citations) >= limit:
            break
    return tuple(citations)


def _focus_on_named_document(question: str, by_key: dict[str, Any]) -> dict[str, Any]:
    """Keep only the filing a question names, when the pool holds several.

    A scoped Memory may carry more than one document. If the question names a
    company that exactly one of those documents belongs to, pages of the other
    filings cannot be evidence and would only take up the citation budget.
    """

    documents: dict[str, list[str]] = {}
    for key, hit in by_key.items():
        identity = _page_identity(hit.text or "")
        if identity is not None:
            documents.setdefault(identity[0], []).append(key)
    if len(documents) < 2:
        return by_key
    flat = pdf_ranking.normalize(question)
    compact = flat.replace(" ", "")
    named = [document for document in documents if _names_document(flat, compact, document)]
    if len(named) != 1:
        return by_key
    return {key: by_key[key] for key in documents[named[0]]}


def _names_document(flat_question: str, compact_question: str, document_id: str) -> bool:
    """Return whether a question names the company a document id belongs to."""

    name = pdf_ranking.normalize(document_id.split("_", 1)[0])
    if len(name) < 4:
        return False
    return f" {name} " in f" {flat_question} " or name in compact_question


def _page_identity(text: str) -> tuple[str, str] | None:
    """Return the ``(document, page text)`` a Memory entry was captured from.

    Two entries that resolve to the same physical page must not both be cited, so
    the caller needs an identity that does not depend on entries being unique.
    """

    document, _, body = text.partition("\n")
    if not document or not body:
        return None
    return document.strip(), body.strip()


def _extract_page_texts(content: bytes) -> list[str]:
    """Return the extractable body text of every physical page, in order."""

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as error:
        raise ValueError(f"PDF document could not be parsed: {error}") from error  # noqa: TRY003
    # An empty user password unlocks a PDF that only carries a permission flag;
    # anything else stays unreadable for this capture.
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF document requires a user password")  # noqa: TRY003
    try:
        return [page.extract_text() or "" for page in reader.pages]
    except Exception as error:
        raise ValueError(f"PDF document could not be parsed: {error}") from error  # noqa: TRY003


def _require_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document id must be a non-empty string")  # noqa: TRY003


def _build_queries(question: str) -> tuple[str, ...]:
    """Expand one question into the lexical queries used to collect candidates.

    A single wide query is admitted by the backend's lexical floor only when a
    page repeats enough question words, which hides single-term evidence pages.
    Issuing each distinctive word on its own, plus the adjacent word pairs that
    carry the actual financial phrase, keeps those pages in the candidate pool;
    the offline ranker then decides the order. The last group asks for the
    finance phrases the question implies, because the page that answers a
    liquidity or capex question names current assets or purchases of property
    and equipment instead of the question's own words.
    """

    terms = list(pdf_ranking.query_terms(question, limit=_MAX_QUERY_TERMS))
    queries: list[str] = [question.strip()]
    queries.extend(terms)
    for left, right in list(combinations(terms, 2))[:_MAX_PAIR_QUERIES]:
        queries.append(f"{left} {right}")
    queries.extend(pdf_ranking.expansion_queries(question, limit=_MAX_EXPANSION_QUERIES))
    return tuple(dict.fromkeys(queries))


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version=PDF_PAGE_SOURCE_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)


__all__ = [
    "PDF_PAGE_IDENTITY_PREFIX",
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDF_PAGE_SOURCE_VERSION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceAdapter",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
