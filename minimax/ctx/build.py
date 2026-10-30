"""按任务检索并拼装上下文（``ctx.json``）。

输出结构（``specs`` / ``code`` / ``tests`` / ``state`` 四个键是赛题要求的固定形状）::

    {
      "task": "<任务描述>",
      "specs": [{"path": ..., "section": ..., "text": ..., "score": ...}],
      "code":  [{"path": ..., "symbol": ..., "text": ..., "score": ...}],
      "tests": [{"path": ..., "case":   ..., "text": ..., "score": ...}],
      "state": {"completed": [...], "todo": [...], "verification": [...]},
      "retrieval": {"backend": "seekdb", "parser": "ngram", "mode": "fulltext", ...}
    }

``specs`` / ``code`` / ``tests`` 的内容**全部来自 seekdb 查询结果**，
``state`` 来自 PowerContext。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import ingest, retrieval, seekdb, state as state_mod
from .config import SeekDBSettings

__all__ = ["build_context", "CONTEXT_KEYS"]

CONTEXT_KEYS = ("specs", "code", "tests", "state")


def _file_entry(row: dict) -> dict[str, Any]:
    return {
        "path": row["path"],
        "symbol": row["symbol"],
        "text": row["content"] or "",
    }


def _spec_entry(hit: retrieval.Hit) -> dict[str, Any]:
    return {
        "path": hit.path,
        "section": hit.symbol,
        "text": hit.content,
        "score": round(hit.score, 6),
    }


def _code_entry(hit: retrieval.Hit) -> dict[str, Any]:
    return {
        "path": hit.path,
        "symbol": hit.symbol,
        "text": hit.content,
        "score": round(hit.score, 6),
    }


def _test_entry(hit: retrieval.Hit) -> dict[str, Any]:
    return {
        "path": hit.path,
        "case": hit.symbol,
        "text": hit.content,
        "score": round(hit.score, 6),
    }


def build_context(
    task: str,
    out_path: str | Path,
    *,
    limit: int = 8,
    include_state: bool = True,
    prefer: str = "auto",
    settings: SeekDBSettings | None = None,
) -> dict[str, Any]:
    """检索并写出 ``ctx.json``，返回写出的字典。"""
    settings = settings or SeekDBSettings.from_env()
    conn = seekdb.connect_database(settings)
    try:
        parser = seekdb.ensure_schema(conn)
        specs_hits, specs_mode = retrieval.search(
            conn, task, ["spec"], limit, prefer=prefer
        )
        code_hits, code_mode = retrieval.search(
            conn, task, ["code"], limit, prefer=prefer
        )
        test_hits, test_mode = retrieval.search(
            conn, task, ["test"], limit, prefer=prefer
        )
        # 代码文件的「整份原文」必须原样带给模型，不能被相关度排序淘汰
        code_paths = sorted({hit.path for hit in code_hits})
        whole_files = seekdb.fetch_documents(conn, "code", ingest.WHOLE_FILE, code_paths)
        counts = seekdb.count_documents(conn)
    finally:
        conn.close()

    if include_state:
        task_state = state_mod.load_state()
    else:
        task_state = {"completed": [], "todo": [], "verification": [], "available": False}

    modes = {specs_mode, code_mode, test_mode} - {"none"}
    if not modes:
        mode = "none"
    elif modes == {"fulltext"}:
        mode = "fulltext"
    elif modes == {"keyword"}:
        mode = "keyword"
    else:
        mode = "fulltext+keyword"

    context: dict[str, Any] = {
        "task": task,
        "specs": [_spec_entry(hit) for hit in specs_hits],
        "code": [_code_entry(hit) for hit in code_hits],
        "files": [_file_entry(row) for row in whole_files],
        "tests": [_test_entry(hit) for hit in test_hits],
        "state": {
            "completed": list(task_state.get("completed") or []),
            "todo": list(task_state.get("todo") or []),
            "verification": list(task_state.get("verification") or []),
        },
        "retrieval": {
            "backend": "seekdb",
            "database": settings.database,
            "dsn": settings.dsn,
            "parser": parser,
            "mode": mode,
            "query": task,
            "limit": limit,
            "doc_counts": counts,
            "powercontext_available": bool(task_state.get("available")),
        },
    }
    if task_state.get("task"):
        context["state"]["task"] = task_state["task"]

    out = Path(out_path)
    if out.parent and str(out.parent) not in ("", "."):
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return context
