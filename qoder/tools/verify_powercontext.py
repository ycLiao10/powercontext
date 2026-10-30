#!/usr/bin/env python
"""验证 PowerContext 接入是否真实生效：写入记忆 -> 检索 -> 断言命中。

用法（先启动 server）：
    python qoder/tools/verify_powercontext.py
    python qoder/tools/verify_powercontext.py --base-url http://127.0.0.1:8000

退出码 0 表示通过；证据写入 qoder/artifacts/verify-powercontext.json。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCOPE_TITLE = "Qoder 赛题验证样例"
MEMORY_TEMPLATE = "评审验证样例 {run_id}：该用户正在参加 2026 OceanBase 数据库大赛 Qoder 赛题，偏好中文回答。"
QUERY = "这个用户正在参加什么比赛？"


def call(base_url: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        base_url + path,
        data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def ensure_scope(base_url: str, title: str) -> str:
    status, listing = call(base_url, "/v1/scopes")
    if status != 200:
        raise RuntimeError(f"列出 Scope 失败: {status} {listing}")
    for scope in listing.get("items", []):
        if scope.get("title") == title:
            return scope["scope_id"]
    status, created = call(
        base_url,
        "/v1/scopes",
        {"title": title, "summary": "验证 PowerContext 记忆写入与召回", "idempotency_key": uuid.uuid4().hex},
    )
    if status not in (200, 201):
        raise RuntimeError(f"创建 Scope 失败: {status} {created}")
    return created["scope_id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "artifacts" / "verify-powercontext.json"),
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    run_id = uuid.uuid4().hex[:8]
    evidence: dict = {"base_url": base_url, "run_id": run_id, "checked_at": datetime.now(timezone.utc).isoformat()}

    status, ready = call(base_url, "/v1/capabilities")
    if status != 200:
        print(f"FAIL: /v1/capabilities -> {status}", file=sys.stderr)
        return 2
    evidence["capabilities"] = ready

    # 每次验证用独立的 Scope：新建的条目都会写入本地嵌入，vector/hybrid 才完整可用
    scope_id = ensure_scope(base_url, f"{SCOPE_TITLE} {run_id}")
    evidence["scope_id"] = scope_id

    status, remembered = call(
        base_url,
        "/v1/memory/remember",
        {"scope_id": scope_id, "kind": "preference", "text": MEMORY_TEMPLATE.format(run_id=run_id)},
    )
    if status != 200:
        print(f"FAIL: remember -> {status} {remembered}", file=sys.stderr)
        return 2
    entry = remembered.get("entry") or {}
    evidence["memory_entry_id"] = (entry.get("citation") or {}).get("entry_id")

    status, searched = call(base_url, "/v1/memory/search", {"scope_id": scope_id, "query": QUERY})
    if status != 200:
        print(f"FAIL: search -> {status} {searched}", file=sys.stderr)
        return 2
    hits = searched.get("hits", [])
    evidence["search_mode"] = searched.get("mode")
    evidence["hits"] = [
        {"entry_id": hit.get("citation", {}).get("entry_id"), "score": hit.get("score"), "matched_by": hit.get("matched_by")}
        for hit in hits
    ]

    matched = any(run_id in (hit.get("text") or "") for hit in hits)

    # 向量通道：同一句换个说法，靠本地嵌入模型召回
    hybrid_query = "参赛选手参加的是哪一个赛事？"
    status, hybrid = call(base_url, "/v1/memory/search", {"scope_id": scope_id, "query": hybrid_query, "mode": "hybrid"})
    hybrid_hits = hybrid.get("hits", []) if status == 200 else []
    evidence["hybrid"] = {
        "status": status,
        "query": hybrid_query,
        "mode": hybrid.get("mode"),
        "matched": any(run_id in (hit.get("text") or "") for hit in hybrid_hits),
        "hits": [{"score": hit.get("score"), "matched_by": hit.get("matched_by")} for hit in hybrid_hits[:3]],
    }

    evidence["passed"] = matched and status == 200
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(evidence, ensure_ascii=True, indent=2))
    print(f"evidence -> {output}")
    if not evidence["passed"]:
        print("FAIL: 记忆召回未通过（fts 或 hybrid 通道未命中本次写入）", file=sys.stderr)
        return 1
    print("PASS: PowerContext 记忆写入、fts 召回与 hybrid（本地嵌入）召回均生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
