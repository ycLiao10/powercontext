"""把业务工程里的规格 / 代码 / 测试抽成可检索的文档。

抽取规则（按 ``--root`` 指向的工程根目录）：

============  ==========================  ==================================
``kind``      来源                        ``symbol`` 的含义
============  ==========================  ==================================
``spec``      ``specs/**/*.md``           Markdown 小节标题
``code``      ``src/**/*.py``             函数名 / 类名（类方法为 ``Cls.meth``）
              同上，另存一份              ``<file>`` —— 整个文件的内容
``test``      ``tests/**/*.py``           测试函数名（即测试用例名）
============  ==========================  ==================================

``path`` 统一写成 ``<工程根目录名>/<相对路径>``，例如
``sample_project/specs/pricing.md``，这样不论从哪里导入都能对上同一份资料。

代码与测试用标准库 :mod:`ast` 解析，不依赖任何第三方解析器，
抽出来的是**源码原文**（可直接喂给 MiniMax）。

为什么要额外的 ``<file>`` 文档：按符号抽取只会拿到函数体，
文件顶部的模块级 docstring 属于模块而不是任何符号，
如果不单独索引，模型就看不到它——生成的完整文件会把它丢掉，
改动就变成「顺手删了注释」而不是最小改动。
``<file>`` 文档保存整份原文，作为模型改写的**基准**。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

__all__ = [
    "Document",
    "iter_documents",
    "classify",
    "split_markdown_sections",
    "extract_python_symbols",
    "WHOLE_FILE",
]

#: 整份文件在 ``eng_docs`` 里用的 symbol 标记
WHOLE_FILE = "<file>"

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


@dataclass(frozen=True)
class Document:
    """一条待写入 ``eng_docs`` 的记录。"""

    kind: str
    path: str
    symbol: str
    content: str


def classify(rel_path: str) -> str | None:
    """判断相对路径属于哪一类；不属于工程资料则返回 ``None``。"""
    parts = rel_path.replace("\\", "/").split("/")
    name = parts[-1]
    if name.endswith(".md"):
        return "spec" if "specs" in parts else None
    if name.endswith(".py"):
        # 测试脚手架不是被改动的代码，不进索引
        if name in ("conftest.py", "setup.py"):
            return None
        if "tests" in parts or name.startswith("test_"):
            return "test"
        return "code"
    return None


def split_markdown_sections(text: str) -> Iterator[tuple[str, str]]:
    """把 Markdown 切成 ``(小节标题, 小节正文)``。

    标题层级不限；正文为标题之后、下一个标题之前的全部内容。
    没有标题时，整篇文件作为一个小节，标题取文件名。
    """
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    preamble: list[str] = []

    for line in lines:
        match = _HEADING.match(line)
        if match:
            if current is not None:
                sections.append(current)
            current = (match.group(2).strip(), [])
        elif current is None:
            preamble.append(line)
        else:
            current[1].append(line)

    if current is not None:
        sections.append(current)

    if not sections:
        yield "", text.strip()
        return

    if any(line.strip() for line in preamble):
        yield "", "\n".join(preamble).strip()

    for title, body in sections:
        body_text = "\n".join(body).strip()
        if not title and not body_text:
            continue
        yield title, body_text


def extract_python_symbols(source: str) -> Iterator[tuple[str, str]]:
    """用 AST 抽出顶层函数、类及其方法，返回 ``(符号名, 源码)``。"""
    tree = ast.parse(source)
    lines = source.splitlines()

    def segment(node: ast.AST) -> str:
        try:
            text = ast.get_source_segment(source, node)
        except Exception:  # pragma: no cover - 极端情况
            text = None
        if text:
            return text
        start = getattr(node, "lineno", 1) - 1
        end = getattr(node, "end_lineno", start + 1)
        return "\n".join(lines[start:end])

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, segment(node)
        elif isinstance(node, ast.ClassDef):
            yield node.name, segment(node)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f"{node.name}.{child.name}", segment(child)


def _iter_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.name.startswith("."):
            continue
        yield path


def iter_documents(root: Path) -> Iterator[Document]:
    """遍历工程根目录，产出全部可索引文档。"""
    root = root.resolve()
    root_label = root.name
    for file_path in _iter_files(root):
        rel = file_path.relative_to(root).as_posix()
        kind = classify(rel)
        if kind is None:
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        full_path = f"{root_label}/{rel}"

        if kind == "spec":
            for title, body in split_markdown_sections(text):
                content = body
                if title:
                    content = f"# {title}\n{body}".strip()
                if not content:
                    continue
                yield Document(kind="spec", path=full_path, symbol=title or rel, content=content)
        else:
            try:
                symbols = list(extract_python_symbols(text))
            except SyntaxError:
                continue
            if kind == "code":
                # 整份文件单独存一份，作为模型改写的基准（模块级 docstring 不属于任何符号）
                yield Document(kind="code", path=full_path, symbol=WHOLE_FILE, content=text)
            for symbol, snippet in symbols:
                if kind == "test" and not symbol.split(".")[-1].startswith("test_"):
                    continue
                yield Document(kind=kind, path=full_path, symbol=symbol, content=snippet)
