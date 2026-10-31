#!/usr/bin/env python3
"""把 PowerContext 的 JSON 响应渲染成人类可读的文本。

用法：
    pc_render.py entries  <response.json> [--inactive]
    pc_render.py prepare <response.json>
    pc_render.py search   <response.json>
    pc_render.py scope    <response.json>
    pc_render.py remember <response.json>

之所以单独做一个渲染器：bash 里 `cmd | python3 - <<'PY'` 会让 heredoc
抢占 stdin，管道里的 JSON 就丢了。这里统一走"先落盘、再渲染"。
"""
from __future__ import annotations

import json
import sys


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def render_entries(data: dict, show_inactive: bool = False) -> None:
    entries = data.get("entries", [])
    print("scope memory head:", data.get("memory"))
    print("条目数:", len(entries))
    print("-" * 78)
    for entry in entries:
        citation = entry.get("citation", {})
        print("[{}] state={} version={}".format(
            entry.get("kind"), entry.get("state"), entry.get("version")))
        print("  entry_id      :", citation.get("entry_id"))
        print("  entry_version :", citation.get("entry_version_id"))
        print("  text          :", entry.get("text"))
        print("-" * 78)


def render_prepare(data: dict) -> None:
    print("  status =", data.get("status"))
    print("  bytes  =", data.get("content_bytes"))
    print("  ----- 召回内容 -----")
    print(data.get("content") or "(空)")
    print("  -------------------")


def render_search(data: dict) -> None:
    print("  mode =", data.get("mode"))
    for hit in data.get("hits", []):
        citation = hit.get("citation", {})
        print("- score={:.6f} matched_by={}".format(
            hit.get("score") or 0.0, hit.get("matched_by")))
        print("  entry_id :", citation.get("entry_id"))
        print("  text     :", hit.get("text"))


def render_scope(data: dict) -> None:
    print("  scope_id :", data.get("scope_id"))
    print("  title    :", data.get("title"))
    print("  summary  :", data.get("summary"))
    print("  version  :", data.get("version"))


def render_remember(data: dict) -> None:
    entry = data.get("entry", {})
    citation = entry.get("citation", {})
    print("  entry_id      :", citation.get("entry_id"))
    print("  entry_version :", citation.get("entry_version_id"))
    print("  kind          :", entry.get("kind"))
    print("  text          :", entry.get("text"))
    print("  state         :", entry.get("state"))


RENDERERS = {
    "entries": render_entries,
    "prepare": render_prepare,
    "search": render_search,
    "scope": render_scope,
    "remember": render_remember,
}


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    command, path = argv[1], argv[2]
    flags = argv[3:]
    renderer = RENDERERS.get(command)
    if renderer is None:
        print("未知命令:", command)
        return 2
    data = load(path)
    if command == "entries":
        renderer(data, show_inactive="--inactive" in flags)
    else:
        renderer(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
