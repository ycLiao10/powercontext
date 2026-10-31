#!/usr/bin/env python3
"""把 Dify DSL 里的工作流图渲染成一张 SVG 编排示意图。

用法：
    python3 scripts/render_workflow_svg.py dsl/grudge-npc-chatflow.yml evidence/workflow-graph.svg

为什么需要它：
    Dify 控制台的画布是前端渲染的，评审未必能起一套 Dify；
    这里直接读 DSL（导出的真实工作流定义），按节点坐标画一张可读的图，
    保证"图 = 导出的 DSL"，不是手绘的示意图。

依赖：PyYAML（python3 -m pip install pyyaml）。只用到 yaml + 标准库。
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("需要 PyYAML：python3 -m pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

COLORS = {
    "start": ("#E8F4EA", "#3F8C4A", "开始"),
    "http-request": ("#E8EFFB", "#3167C6", "HTTP 请求"),
    "code": ("#F2EBFB", "#6C3FB5", "代码执行"),
    "if-else": ("#FDF3E2", "#C77C1B", "条件分支"),
    "llm": ("#FBEAEA", "#C0392B", "模型调用"),
    "variable-aggregator": ("#E9F6F5", "#178A80", "变量聚合"),
    "answer": ("#EDF1F4", "#4A5B68", "回答"),
}
DEFAULT_COLOR = ("#F5F5F5", "#666666", "节点")

FONT = "font-family=\"Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif\""


def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def wrap(text: str, width: int) -> list[str]:
    """按显示宽度换行（中文按 2 个宽度算）。"""
    lines, cur, width_now = [], "", 0
    for ch in text:
        w = 2 if ord(ch) > 0x2E80 else 1
        if width_now + w > width:
            lines.append(cur)
            cur, width_now = "", 0
        cur += ch
        width_now += w
    if cur:
        lines.append(cur)
    return lines


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    dsl_path = Path(argv[1])
    out_path = Path(argv[2])

    raw = dsl_path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    graph = (doc.get("workflow") or {}).get("graph") or {}
    if not graph:
        print("在 DSL 里找不到 workflow.graph", file=sys.stderr)
        return 1

    nodes = {}
    for node in graph.get("nodes") or []:
        nid = node.get("id")
        data = node.get("data") or {}
        pos = node.get("position") or {}
        nodes[nid] = {
            "title": data.get("title") or nid,
            "desc": data.get("desc") or "",
            "type": data.get("type") or "unknown",
            "x": float(pos.get("x") or 0),
            "y": float(pos.get("y") or 0),
            "w": float(node.get("width") or 244),
            "h": float(node.get("height") or 116),
        }

    edges = []
    for edge in graph.get("edges") or []:
        edges.append((edge.get("source"), edge.get("target"),
                      edge.get("sourceHandle"), edge.get("targetHandle")))

    scale = 0.46
    pad_x, pad_top, pad_bottom = 40, 70, 60
    xs = [n["x"] for n in nodes.values()] or [0]
    ys = [n["y"] for n in nodes.values()] or [0]
    min_x, min_y = min(xs), min(ys)

    # 先算每个节点要画几行文字，据此决定方框高度（避免文字溢出方框）
    for n in nodes.values():
        n["lines"] = wrap(n["desc"], 24)[:3]
        n["bw"] = n["w"] * scale
        n["bh"] = max(n["h"] * scale, 26 + 12 * (len(n["lines"]) + 1))
        n["bx"] = pad_x + (n["x"] - min_x) * scale
        n["by"] = pad_top + (n["y"] - min_y) * scale

    max_x = max(n["bx"] + n["bw"] for n in nodes.values())
    max_y = max(n["by"] + n["bh"] for n in nodes.values())
    legend_y = max_y + 28
    width = max_x + pad_x
    height = legend_y + 30

    def px(n: dict) -> float:
        return n["bx"]

    def py(n: dict) -> float:
        return n["by"]

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}">')
    out.append('<rect width="100%" height="100%" fill="#FFFFFF"/>')
    out.append(f'<text x="{pad_x}" y="34" {FONT} font-size="22" font-weight="bold" fill="#1F2A37">'
               '记仇的 NPC：铁匠阿铁 —— Dify Chatflow 编排（由 dsl/grudge-npc-chatflow.yml 渲染）</text>')
    out.append(f'<text x="{pad_x}" y="56" {FONT} font-size="13" fill="#6B7280">'
               '图中每个方框都对应 DSL 里的一个节点，箭头对应 DSL 里的连线；'
               '蓝色方框表示直接访问 PowerContext 的 HTTP 节点。</text>')

    # 连线（先画，压在节点下面）
    for src, tgt, shandle, thandle in edges:
        if src not in nodes or tgt not in nodes:
            continue
        s, t = nodes[src], nodes[tgt]
        x1 = px(s) + s["bw"]
        y1 = py(s) + s["bh"] / 2
        x2 = px(t)
        y2 = py(t) + t["bh"] / 2
        if shandle == "bottom":
            x1, y1 = px(s) + s["bw"] / 2, py(s) + s["bh"]
        if thandle == "top":
            x2, y2 = px(t) + t["bw"] / 2, py(t)
        if shandle == "bottom" and thandle == "top":
            mid = (y1 + y2) / 2
            d = (f"M {x1:.1f} {y1:.1f} C {x1:.1f} {mid:.1f}, {x2:.1f} {mid:.1f}, {x2:.1f} {y2:.1f}")
        else:
            mid = (x1 + x2) / 2
            d = (f"M {x1:.1f} {y1:.1f} C {mid:.1f} {y1:.1f}, {mid:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}")
        out.append(f'<path d="{d}" fill="none" stroke="#8A94A6" stroke-width="1.4" marker-end="url(#arrow)"/>')

    # 节点
    for nid, n in nodes.items():
        fill, stroke, _ = COLORS.get(n["type"], DEFAULT_COLOR)
        x, y, w, h = px(n), py(n), n["bw"], n["bh"]
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="6" '
                   f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
        out.append(f'<text x="{x + 8:.1f}" y="{y + 15:.1f}" {FONT} font-size="11.5" font-weight="bold" '
                   f'fill="#1F2A37">{esc(n["title"])}</text>')
        for i, line in enumerate(n["lines"]):
            out.append(f'<text x="{x + 8:.1f}" y="{y + 29 + i * 11:.1f}" {FONT} font-size="9" '
                       f'fill="#4B5563">{esc(line)}</text>')
        if n["type"] == "http-request":
            out.append(f'<text x="{x + 8:.1f}" y="{y + h - 6:.1f}" {FONT} font-size="8.5" '
                       f'fill="{stroke}">↳ PowerContext HTTP</text>')

    # 图例
    out.append(f'<text x="{pad_x}" y="{legend_y - 14:.0f}" {FONT} font-size="12" fill="#1F2A37">图例</text>')
    lx = pad_x
    for ntype, (fill, stroke, label) in COLORS.items():
        out.append(f'<rect x="{lx}" y="{legend_y - 10:.0f}" width="14" height="14" rx="3" '
                   f'fill="{fill}" stroke="{stroke}" stroke-width="1.2"/>')
        out.append(f'<text x="{lx + 19}" y="{legend_y + 1:.0f}" {FONT} font-size="11" '
                   f'fill="#374151">{esc(label)}</text>')
        lx += 34 + len(label) * 13
    out.append('</svg>')

    defs = ('<defs><marker id="arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" '
            'orient="auto"><path d="M0,0 L10,4 L0,8 z" fill="#8A94A6"/></marker></defs>')
    out.insert(1, defs)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")
    print(f"已生成 {out_path}（{len(nodes)} 个节点 / {len(edges)} 条连线）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
