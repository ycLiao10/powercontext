from __future__ import annotations

from .embedding import EmbeddingClient, EmbeddingError
from .pc_client import PowerContext, PowerContextError
from .store import Store

MEMORY_BOOST = 1.15
FUSION_K = 60


def search(store: Store, pc: PowerContext, embedder: EmbeddingClient | None, query: str, limit: int = 5) -> dict:
    query = query.strip()
    if not query:
        raise ValueError("查询不能为空")

    channels: dict[str, list[dict]] = {"lexical": store.search(query, limit=limit * 3)}
    if embedder is not None:
        try:
            vector = embedder.embed([query])[0]
            semantic = store.vector_search(vector, limit * 3)
            if semantic:
                channels["vector"] = semantic
        except EmbeddingError:
            pass

    fused = _fuse(channels)

    memory_hits: list[dict] = []
    pc_available = True
    try:
        memory_hits = pc.search_memory(query, limit=6)
    except PowerContextError:
        pc_available = False

    for hit in fused:
        hit["memory_boost"] = any(f"《{hit['document_title']}》" in memory["text"] for memory in memory_hits)
        hit["weighted_score"] = hit["fusion_score"] * (MEMORY_BOOST if hit["memory_boost"] else 1.0)

    fused.sort(key=lambda hit: hit["weighted_score"], reverse=True)
    top = max((hit["weighted_score"] for hit in fused), default=0.0)
    results = fused[:limit]
    for hit in results:
        hit["final_score"] = round(hit["weighted_score"] / top * 100, 1) if top > 0 else 0.0

    return {
        "query": query,
        "results": results,
        "memory": memory_hits,
        "pc_available": pc_available,
        "channels_used": sorted(channels),
    }


def _fuse(channels: dict[str, list[dict]]) -> list[dict]:
    """Reciprocal Rank Fusion：不同通道的打分量纲不可比，只按名次融合。"""
    merged: dict[int, dict] = {}
    for name, hits in channels.items():
        for rank, hit in enumerate(hits):
            entry = merged.setdefault(
                hit["id"],
                {
                    "id": hit["id"],
                    "document_id": hit["document_id"],
                    "document_title": hit["document_title"],
                    "ordinal": hit["ordinal"],
                    "start_offset": hit["start_offset"],
                    "end_offset": hit["end_offset"],
                    "text": hit["text"],
                    "fusion_score": 0.0,
                    "channels": [],
                },
            )
            entry["fusion_score"] += 1.0 / (FUSION_K + rank + 1)
            entry["channels"].append(name)
    return list(merged.values())
