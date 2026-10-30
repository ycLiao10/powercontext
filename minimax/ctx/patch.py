"""解析并落地模型返回的代码修改。

模型可能返回两种形态，本模块都能处理：

1. **完整文件内容** —— ```` ```python ```` 代码块，直接覆盖目标文件；
2. **统一 diff** —— ```` ```diff ```` 代码块，用内置的 unified diff 应用器合并。

无论哪种形态，落地前都会做**语法校验**（Python 文件必须能被 ``ast.parse`` 解析），
解析失败直接报错，绝不把坏代码写进工程。
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass

__all__ = [
    "PatchError",
    "FilePatch",
    "parse_model_output",
    "apply_unified_diff",
    "unified_diff",
    "extract_fences",
]

_FENCE = re.compile(r"```([A-Za-z0-9_+-]*)\r?\n(.*?)(?:```|\Z)", re.DOTALL)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_FILE = re.compile(r"^---\s+(?:a/)?(\S+)\s*$", re.MULTILINE)


class PatchError(RuntimeError):
    """模型返回的内容无法落地。"""


@dataclass(frozen=True)
class FilePatch:
    """一处待落地的文件修改。"""

    path: str
    kind: str  # "full" 或 "diff"
    content: str


def extract_fences(text: str) -> list[tuple[str, str]]:
    """抽出所有围栏代码块，返回 ``(语言标签, 内容)``。"""
    return [(m.group(1).strip().lower(), m.group(2)) for m in _FENCE.finditer(text or "")]


def _parse_diff_blocks(text: str) -> list[FilePatch]:
    """把 unified diff 文本切成若干 ``FilePatch``。"""
    header = re.search(r"^---\s+(?:a/)?(\S+)\s*\n\+\+\+\s+(?:b/)?(\S+)\s*$", text, re.MULTILINE)
    if not header:
        raise PatchError("diff 缺少 --- / +++ 文件头")
    path = header.group(2)
    body = text[header.end():]
    return [FilePatch(path=path, kind="diff", content=body)]


def parse_model_output(text: str, default_path: str) -> list[FilePatch]:
    """把模型返回解析成待落地的文件修改列表。

    优先识别 diff / patch 围栏；否则在代码围栏里挑一个**能被 Python 解析**的
    （模型偶尔会先吐一个残缺的草稿块），全都解析不了才退回第一个，
    由调用方报错——宁可失败也不写坏代码。
    """
    fences = extract_fences(text)
    if not fences:
        raise PatchError("模型返回里没有找到任何 ``` 代码块")

    for language, content in fences:
        if language in ("diff", "patch"):
            return _parse_diff_blocks(content)

    candidates = [
        (language, content)
        for language, content in fences
        if language in ("", "python", "py")
    ] or fences

    fallback: FilePatch | None = None
    for _language, content in candidates:
        cleaned = content.strip("\n") + "\n"
        patch = FilePatch(path=default_path, kind="full", content=cleaned)
        if fallback is None:
            fallback = patch
        if not default_path.endswith(".py"):
            return [patch]
        try:
            ast.parse(cleaned)
        except SyntaxError:
            continue
        return [patch]

    if fallback is None:  # pragma: no cover - fences 非空时不会走到
        raise PatchError("模型返回里没有可用的代码块")
    return [fallback]


def apply_unified_diff(original: str, diff_body: str) -> str:
    """把 unified diff 合并到原文上，返回新内容。

    只依赖标准库，实现最小可用的 hunk 应用（支持 ``\\ No newline at end of file``）。
    """
    source = original.splitlines(keepends=True)
    result: list[str] = []
    cursor = 0
    lines = diff_body.splitlines(keepends=True)
    index = 0
    pending_strip_newline = False

    while index < len(lines):
        line = lines[index]
        match = _HUNK.match(line)
        if not match:
            index += 1
            continue
        old_start = int(match.group(1))
        # 把 hunk 之前的原样内容拷进去
        result.extend(source[cursor : old_start - 1])
        cursor = old_start - 1
        index += 1
        while index < len(lines) and not lines[index].startswith("@@"):
            hunk_line = lines[index]
            if hunk_line.startswith("\\"):  # "\ No newline at end of file"
                pending_strip_newline = True
            elif hunk_line.startswith("+"):
                result.append(hunk_line[1:])
                pending_strip_newline = False
            elif hunk_line.startswith("-"):
                cursor += 1
                pending_strip_newline = False
            elif hunk_line.startswith(" "):
                result.append(hunk_line[1:])
                cursor += 1
                pending_strip_newline = False
            index += 1
        if pending_strip_newline and result:
            result[-1] = result[-1].rstrip("\n")
            pending_strip_newline = False

    result.extend(source[cursor:])
    return "".join(result)


def validate_python(source: str, *, label: str = "") -> None:
    """语法校验：解析失败直接抛 :class:`PatchError`。"""
    try:
        ast.parse(source)
    except SyntaxError as exc:
        where = f"（{label}）" if label else ""
        raise PatchError(f"生成的代码语法错误{where}：第 {exc.lineno} 行 {exc.msg}") from exc


def unified_diff(before: str, after: str, path: str) -> str:
    """生成 unified diff 文本（用于留证）。"""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
