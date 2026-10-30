from __future__ import annotations

import re
import time

from .embedding import EmbeddingClient, EmbeddingError
from .pc_client import PowerContext, PowerContextError
from .store import Store


def ingest_document(store: Store, pc: PowerContext, embedder: EmbeddingClient | None, title: str, text: str) -> dict:
    title = title.strip()
    text = text.strip()
    if not title or not text:
        raise ValueError("标题和正文都不能为空")

    document_id, chunks = store.add_document(title, text, time.time())
    if document_id is None:
        return {"duplicate": True, "title": title, "chunk_count": 0, "vector_count": 0, "warnings": []}

    warnings = []
    for chunk_id, chunk, ordinal, start in chunks:
        try:
            pc.capture_source(
                f"doc-{document_id}-chunk-{ordinal}",
                chunk,
                {"title": title, "ordinal": ordinal, "start_offset": start, "document_id": document_id},
            )
        except PowerContextError as error:
            warnings.append(f"来源写入失败 chunk#{ordinal}: {error}")

    try:
        pc.remember("document", f"文献《{title}》已导入，共 {len(chunks)} 段。摘要：{_summary(text)}", "导入文献")
    except PowerContextError as error:
        warnings.append(f"记忆写入失败: {error}")

    vector_count = 0
    if embedder is not None:
        try:
            vectors = embedder.embed([chunk for _, chunk, _, _ in chunks])
            store.save_vectors(embedder.model, [(chunk_id, vector) for (chunk_id, _, _, _), vector in zip(chunks, vectors)])
            vector_count = len(vectors)
        except EmbeddingError as error:
            warnings.append(f"向量写入失败，检索将降级为词法: {error}")

    return {"duplicate": False, "document_id": document_id, "title": title, "chunk_count": len(chunks),
            "vector_count": vector_count, "warnings": warnings}


def backfill_vectors(store: Store, embedder: EmbeddingClient) -> int:
    pending = store.chunks_without_vectors(embedder.model)
    if not pending:
        return 0
    vectors = embedder.embed([text for _, text in pending])
    store.save_vectors(embedder.model, [(chunk_id, vector) for (chunk_id, _), vector in zip(pending, vectors)])
    return len(vectors)


def _summary(text: str, limit: int = 160) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed[:limit]
