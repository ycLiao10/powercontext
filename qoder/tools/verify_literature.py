#!/usr/bin/env python
"""端到端验证文献阅读工作台：导入 -> 混合检索（词法+向量）-> 出处定位 -> PowerContext 记忆参与。

前置：嵌入服务（tools/embed_server.py）、PowerContext Server 与工作台均已启动。
    python tools/verify_literature.py
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DOCS = [
    (
        "Transformer 综述",
        "自注意力机制通过 query 与 key 的相似度对 value 加权聚合，避免了循环结构的长程依赖问题。"
        "位置编码为序列注入顺序信息，多头注意力让不同子空间关注不同模式。\n\n"
        "训练阶段使用残差连接与层归一化稳定深层网络，推理阶段可用 KV 缓存降低自回归解码的重复计算。",
    ),
    (
        "混合检索实践",
        "BM25 基于词频与逆文档频率，擅长关键词精确匹配；向量检索把文本映射到稠密空间，擅长语义近义与跨语言召回。\n\n"
        "混合检索把两路候选做融合重排（如 RRF、加权求和），通常能同时提升召回率与排序精度。"
        "融合前需要统一候选数量与打分量纲，否则容易出现某一路完全压制另一路的情况。",
    ),
]

LEXICAL_CASES = [
    ("混合检索如何融合排序", "混合检索实践"),
    ("自注意力如何缓解长程依赖", "Transformer 综述"),
]

# 换一种说法、几乎不共享原词：词法通道排不上，只能靠向量通道召回
SEMANTIC_CASES = [
    ("两种检索方式的结果怎么合到一起排序", "混合检索实践"),
    ("长距离依赖为什么不再是问题", "Transformer 综述"),
]


def call(base_url: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base_url + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "artifacts" / "verify-literature.json"),
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    evidence: dict = {"base_url": base, "checked_at": datetime.now(timezone.utc).isoformat(), "steps": []}
    failures: list[str] = []

    status, health = call(base, "/api/health")
    component = health.get("powercontext", {})
    embedding = health.get("embedding", {})
    evidence["health"] = health
    if status != 200 or not component.get("available"):
        failures.append("PowerContext 不可用，工作台未就绪")
    if not embedding.get("available"):
        failures.append("本地嵌入服务不可用，混合检索会退化")
    if "vector" not in component.get("search_modes", []):
        failures.append("PowerContext 未启用向量检索模式（检查 .env 里的 embedding 配置）")
    evidence["steps"].append({"step": "health", "status": status, "powercontext": component, "embedding": embedding})

    for title, text in DOCS:
        status, result = call(base, "/api/documents", {"title": title, "text": text})
        evidence["steps"].append({"step": "ingest", "title": title, "status": status, "result": result})
        if status != 200:
            failures.append(f"导入《{title}》失败: {result}")
            continue
        if result.get("warnings"):
            failures.append(f"《{title}》写入 PowerContext 有告警: {result['warnings']}")
        if not result.get("duplicate") and result.get("vector_count") != result.get("chunk_count"):
            failures.append(f"《{title}》向量数 {result.get('vector_count')} 与分块数 {result.get('chunk_count')} 不一致")

    for kind, cases in (("lexical", LEXICAL_CASES), ("semantic", SEMANTIC_CASES)):
        for query, expected_title in cases:
            status, result = call(base, "/api/search?q=" + urllib.parse.quote(query))
            ranked = result.get("results", [])
            titles = [hit["document_title"] for hit in ranked]
            memory_texts = [hit["text"] for hit in result.get("memory", [])]
            top_hit = ranked[0] if ranked else None
            expected_hit = expected_title in titles[:3]
            memory_reused = any(f"《{expected_title}》" in text for text in memory_texts)
            channels = result.get("channels_used", [])
            evidence["steps"].append(
                {
                    "step": "search",
                    "case": kind,
                    "query": query,
                    "status": status,
                    "channels_used": channels,
                    "expected_title": expected_title,
                    "matched_title": expected_hit,
                    "memory_reused": memory_reused,
                    "results": [
                        {
                            "title": hit["document_title"],
                            "ordinal": hit["ordinal"],
                            "offset": [hit["start_offset"], hit["end_offset"]],
                            "score": round(hit["final_score"], 4),
                            "channels": hit["channels"],
                            "memory_boost": hit["memory_boost"],
                        }
                        for hit in ranked
                    ],
                }
            )
            if status != 200:
                failures.append(f"检索「{query}」返回 {status}")
                continue
            if "vector" not in channels:
                failures.append(f"检索「{query}」没有走向量通道，实际通道: {channels}")
            if not expected_hit:
                failures.append(f"检索「{query}」未在前 3 条命中《{expected_title}》，实际: {titles}")
            if not memory_reused:
                failures.append(f"检索「{query}」未复用《{expected_title}》的导入记忆")
            if kind == "semantic":
                if top_hit is None or top_hit["document_title"] != expected_title:
                    failures.append(f"语义检索「{query}」首位应为《{expected_title}》，实际: {titles[:1]}")
                elif "vector" not in top_hit["channels"]:
                    failures.append(f"语义检索「{query}」首位没有向量通道贡献")

    evidence["passed"] = not failures
    evidence["failures"] = failures
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"passed": evidence["passed"], "failures": failures}, ensure_ascii=True, indent=2))
    print(f"evidence -> {output}")
    if failures:
        return 1
    print("PASS: 导入、混合检索（词法+向量）、出处定位与 PowerContext 记忆复用均生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
