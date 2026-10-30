"""把检索到的上下文喂给 MiniMax，把模型返回的修改真正落进工程文件。

流程：

1. 读入 ``ctx.json``（规格 / 代码 / 测试 / 任务状态，全部来自 seekdb + PowerContext）；
2. 组装提示词并调用 MiniMax；
3. 解析模型返回（完整文件内容或 unified diff）；
4. **语法校验**通过后写入目标文件；
5. 记录「任务 / 上下文 / 模型返回 / 最终 diff」到 ``logs/minimax.md``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import patch as patch_mod
from .minimax_api import ChatTurn, MiniMaxAPI

__all__ = ["FixResult", "run_fix", "build_prompt"]


@dataclass
class FixResult:
    """一次修复的结果。"""

    task: str
    model: str
    target: str
    before: str
    after: str
    diff: str
    turn: ChatTurn
    prompt: str
    context: dict[str, Any] = field(default_factory=dict)
    applied: bool = False
    baseline: str = ""
    baseline_diff: str = ""

    @property
    def changed(self) -> bool:
        return self.before != self.after


def _render_sections(items: list[dict[str, Any]], label_key: str, title_key: str) -> str:
    blocks: list[str] = []
    for item in items:
        path = item.get("path", "")
        title = item.get(title_key, "")
        text = item.get("text", "")
        blocks.append(f"### {path}" + (f" · {title}" if title else ""))
        blocks.append("```python" if path.endswith(".py") else "```markdown")
        blocks.append(text.rstrip("\n"))
        blocks.append("```")
        blocks.append("")
    return "\n".join(blocks) if blocks else "（无）\n"


def build_prompt(context: dict[str, Any], target: str) -> str:
    """组装给模型的提示词。"""
    task = context.get("task", "")
    state = context.get("state") or {}

    state_lines: list[str] = []
    for kind in ("completed", "todo", "verification"):
        for item in state.get(kind) or []:
            state_lines.append(f"- [{kind}] {item}")
    if state.get("task"):
        state_lines.insert(0, f"- [当前任务] {state['task']}")

    prompt = f"""你是资深 Python 工程师。请依据下面的规格修改代码，使目标测试全部通过。

## 任务
{task}

## 规格（来自 seekdb 检索，是唯一权威来源）
{_render_sections(context.get('specs') or [], 'section', 'section')}
## 当前代码（来自 seekdb 检索，按符号抽取）
{_render_sections(context.get('code') or [], 'symbol', 'symbol')}
## 当前文件完整内容（来自 seekdb 检索，**这是你改写的基准**）
{_render_sections(context.get('files') or [], 'symbol', 'symbol')}
## 目标测试（必须全部通过；禁止修改、删除、跳过任何断言，也禁止改预期值）
{_render_sections(context.get('tests') or [], 'case', 'case')}
## 已有任务状态（来自 PowerContext）
{chr(10).join(state_lines) if state_lines else '（无）'}

## 输出要求（务必严格遵守）
1. 以上面「当前文件完整内容」为**基准**做改写，其余部分一律保持原样。
2. 只输出目标文件 `{target}` 修改后的**完整内容**，一个都不能少。
3. 用**一个** ```python 代码块包裹，代码块之外不要输出任何文字、解释或标题。
4. **必须原样保留**文件顶部的模块级 docstring、函数 docstring 和全部原有注释。
5. 不要改动函数名与签名 `total(items, is_member)`。
6. 不要改动测试文件，不要改动规格文件。
7. 保留文件原有的代码风格；金额结果用 `round(x, 2)` 保留两位小数。
8. 只做让规格与测试通过的**最小改动**：不要删注释、不要重排代码、不要重命名变量、
   不要顺手重构或新增功能。改动应当集中在优惠计算那几行。
"""
    return prompt


SYSTEM_PROMPT = (
    "你是一个严格遵守规格的 Python 工程师。你只输出修改后的完整源文件，"
    "输出被一个 ```python 代码块包裹，代码块外没有任何多余文字。"
)


def run_fix(
    ctx_path: str | Path,
    *,
    out_log: str | Path | None = None,
    target: str | None = None,
    repo_root: str | Path | None = None,
    dry_run: bool = False,
    append: bool = True,
) -> FixResult:
    """调用 MiniMax 生成修改并落地。"""
    ctx_file = Path(ctx_path)
    context = json.loads(ctx_file.read_text(encoding="utf-8"))

    code_items = context.get("code") or []
    if not code_items:
        raise ValueError("ctx.json 里没有 code 条目，无法确定修改目标")
    if target:
        target_path = target
    else:
        targets = sorted({item["path"] for item in code_items})
        if len(targets) > 1:
            raise ValueError(
                f"ctx.json 里有多个代码文件 {targets}，请用 --target 明确指定要改哪一个"
            )
        target_path = targets[0]

    root = Path(repo_root) if repo_root else Path.cwd()
    absolute = root / target_path

    before = absolute.read_text(encoding="utf-8") if absolute.exists() else ""

    # 基线：第一次调用前把原文件内容存下来，后续多轮调用都对着同一基线算「累积 diff」
    baseline_path = (
        Path(out_log).parent / ".baseline" / target_path.replace("/", "__")
        if out_log
        else None
    )
    if baseline_path is not None:
        if baseline_path.exists():
            baseline = baseline_path.read_text(encoding="utf-8")
        else:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(before, encoding="utf-8")
            baseline = before
    else:
        baseline = before

    api = MiniMaxAPI()
    turn = api.chat(build_prompt(context, target_path), system_prompt=SYSTEM_PROMPT)

    patches = patch_mod.parse_model_output(turn.response_text, target_path)
    applied_patches: list[dict[str, str]] = []

    for item in patches:
        path = item.path if item.path else target_path
        target_abs = root / path
        current = target_abs.read_text(encoding="utf-8") if target_abs.exists() else ""
        if item.kind == "full":
            new_text = item.content
        else:
            new_text = patch_mod.apply_unified_diff(current, item.content)
        if path.endswith(".py"):
            patch_mod.validate_python(new_text, label=path)
        if not dry_run:
            target_abs.parent.mkdir(parents=True, exist_ok=True)
            target_abs.write_text(new_text, encoding="utf-8")
        applied_patches.append({"path": path, "kind": item.kind})

    after = absolute.read_text(encoding="utf-8") if (absolute.exists() and not dry_run) else before
    diff = patch_mod.unified_diff(before, after, target_path)
    baseline_diff = patch_mod.unified_diff(baseline, after, target_path)

    result = FixResult(
        task=context.get("task", ""),
        model=turn.model,
        target=target_path,
        before=before,
        after=after,
        diff=diff,
        turn=turn,
        prompt=turn.user,
        context=context,
        applied=not dry_run,
        baseline=baseline,
        baseline_diff=baseline_diff,
    )

    if out_log:
        _write_log(Path(out_log), result, applied_patches, append=append)
    _record_in_powercontext(result)
    return result


def _record_in_powercontext(result: FixResult) -> None:
    """把这次改动记进 PowerContext 的任务状态（可选，失败不影响主流程）。"""
    try:
        from .state import PowerContextError, save_progress

        added = sum(1 for line in result.diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in result.diff.splitlines() if line.startswith("-") and not line.startswith("---"))
        save_progress(
            "completed",
            f"MiniMax 已生成并落地 {result.target} 的修改（+{added}/-{removed} 行）",
        )
    except Exception:  # noqa: BLE001 - 状态回写属于尽力而为
        return


def _write_log(
    path: Path, result: FixResult, applied_patches: list[dict[str, str]], *, append: bool
) -> None:
    """把这次调用写进 logs/minimax.md。"""
    header = "" if append and path.exists() else (
        "# MiniMax 调用记录\n\n"
        "本文件记录每次调用 MiniMax 的**任务描述、提供的上下文、模型返回、最终落地的 diff**。\n"
        "MiniMax API Key 只从环境变量 `MINIMAX_API_KEY` 读取，不出现在本文件中。\n"
    )
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    call_no = existing.count("## 调用 #") + 1

    ctx = result.context
    state = ctx.get("state") or {}
    state_lines = [
        f"- [{kind}] {item}"
        for kind in ("completed", "todo", "verification")
        for item in (state.get(kind) or [])
    ]

    chunks = [header]
    chunks.append(f"\n## 调用 #{call_no} MiniMax 生成代码修改\n")
    chunks.append(f"- 模型：`{result.model}`")
    chunks.append(f"- 目标文件：`{result.target}`")
    chunks.append(f"- 落地方式：{', '.join(p['kind'] for p in applied_patches)}")
    chunks.append(f"- 本次改动：{'有' if result.diff.strip() else '无'}；"
                  f"相对基线的累计改动：{'有' if result.baseline_diff.strip() else '无'}\n")

    chunks.append("### 1. 任务描述\n")
    chunks.append("```\n" + result.task + "\n```\n")

    chunks.append("### 2. 提供的上下文\n")
    chunks.append("#### 2.1 规格（来自 seekdb 全文检索）\n")
    chunks.append(_render_sections(ctx.get("specs") or [], "section", "section"))
    chunks.append("#### 2.2 代码（来自 seekdb 全文检索，按符号抽取）\n")
    chunks.append(_render_sections(ctx.get("code") or [], "symbol", "symbol"))
    chunks.append("#### 2.3 当前文件完整内容（来自 seekdb 全文检索，改写基准）\n")
    chunks.append(_render_sections(ctx.get("files") or [], "symbol", "symbol"))
    chunks.append("#### 2.4 测试（来自 seekdb 全文检索）\n")
    chunks.append(_render_sections(ctx.get("tests") or [], "case", "case"))
    chunks.append("#### 2.5 任务状态（来自 PowerContext）\n")
    chunks.append("\n".join(state_lines) if state_lines else "（无）")
    chunks.append("")

    chunks.append("### 3. 提示词（实际发给模型的内容）\n")
    chunks.append("```\n" + result.prompt + "\n```\n")

    chunks.append("### 4. 模型返回（原样）\n")
    chunks.append("```\n" + result.turn.response_text + "\n```\n")

    chunks.append("### 5. 最终落地的 diff（本次调用）\n")
    chunks.append("```diff\n" + (result.diff or "（无变化）") + "\n```\n")

    chunks.append("### 6. 相对基线的累积 diff（任务开始时的原文件 → 当前文件）\n")
    chunks.append("```diff\n" + (result.baseline_diff or "（无变化）") + "\n```\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(chunks))
