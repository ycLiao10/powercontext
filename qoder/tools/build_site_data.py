"""把验证证据（artifacts/verify-literature.json）和真实分块正文导出为站点演示数据。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "artifacts" / "verify-literature.json"
DB = ROOT / "data" / "literature.db"
OUT = ROOT / "site" / "data" / "demo.json"


def main() -> int:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "select d.title, c.ordinal, c.start_offset, c.end_offset, c.text "
        "from chunks c join documents d on d.id = c.document_id"
    ).fetchall()
    snippets = {(row["title"], row["ordinal"]): row["text"] for row in rows}

    cases = []
    for step in evidence["steps"]:
        if step.get("step") != "search":
            continue
        cases.append(
            {
                "case": step["case"],
                "query": step["query"],
                "channels_used": step["channels_used"],
                "memory_reused": step["memory_reused"],
                "results": [
                    {
                        **{key: item[key] for key in ("title", "ordinal", "offset", "score", "channels", "memory_boost")},
                        "text": snippets.get((item["title"], item["ordinal"]), ""),
                    }
                    for item in step["results"]
                ],
            }
        )

    payload = {
        "captured_from": "http://127.0.0.1:8080/api/search",
        "evidence_file": "qoder/artifacts/verify-literature.json",
        "embedding": evidence["health"]["embedding"],
        "search_modes": evidence["health"]["powercontext"]["search_modes"],
        "documents": evidence["health"]["documents"],
        "chunks": evidence["health"]["chunks"],
        "cases": cases,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"demo data -> {OUT} ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
