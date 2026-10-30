"""``python -m ctx`` 的命令行入口。

::

    python -m ctx index  --root sample_project
    python -m ctx build  --task "让 total 按规格实现满减与会员折扣取优惠大的" --out ctx.json
    python -m ctx status
    python -m ctx save   --kind completed --text "已完成 xxx"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import PowerContextSettings, SeekDBSettings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ctx",
        description="SDD 上下文管理系统：规格/代码/测试索引、上下文检索、任务状态",
    )
    parser.add_argument("--version", action="version", version=f"ctx {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="把规格/代码/测试导入 seekdb")
    p_index.add_argument("--root", required=True, help="业务工程根目录，例如 sample_project")
    p_index.add_argument("--database", default=None, help="覆盖 SEEKDB_DATABASE")
    p_index.add_argument("--reset", action="store_true", help="先清空 eng_docs 再导入")
    p_index.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    p_build = sub.add_parser("build", help="按任务检索并输出上下文")
    p_build.add_argument("--task", required=True, help="任务描述")
    p_build.add_argument("--out", required=True, help="输出文件，例如 ctx.json")
    p_build.add_argument("--limit", type=int, default=8, help="每一类最多召回多少条")
    p_build.add_argument("--prefer", choices=("auto", "fulltext", "keyword"), default="auto")
    p_build.add_argument("--no-state", action="store_true", help="不读取 PowerContext 状态")
    p_build.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    p_status = sub.add_parser("status", help="读取并打印 PowerContext 里的任务状态")
    p_status.add_argument("--scope", default=None, help="覆盖 POWERCONTEXT_SCOPE_ID")
    p_status.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    p_save = sub.add_parser("save", help="往 PowerContext 写一条任务进展 / 验证结果")
    p_save.add_argument(
        "--kind",
        required=True,
        choices=("task", "completed", "todo", "verification"),
        help="记忆类型",
    )
    p_save.add_argument("--text", required=True, help="内容")
    p_save.add_argument("--reason", default=None, help="写入原因")
    p_save.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    p_fix = sub.add_parser("fix", help="用 MiniMax 按上下文生成并落地代码修改")
    p_fix.add_argument("--ctx", default="ctx.json", help="上下文文件，默认 ctx.json")
    p_fix.add_argument("--target", default=None, help="要修改的文件（默认取 ctx.json 里的唯一代码文件）")
    p_fix.add_argument("--out", default="logs/minimax.md", help="调用记录输出位置")
    p_fix.add_argument("--dry-run", action="store_true", help="只调用模型，不写文件")
    p_fix.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    p_verify = sub.add_parser("verify", help="运行目标测试并记录结果")
    p_verify.add_argument("--root", default="sample_project", help="业务工程根目录")
    p_verify.add_argument("--out", default=None, help="把执行记录写入该文件")
    p_verify.add_argument("--label", default="验证", help="记录标题，例如「改前」/「改后」")
    p_verify.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    p_serve = sub.add_parser("serve", help="启动 PowerContext 服务（状态落到固定的 SQLite 文件）")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_serve.add_argument("--port", type=int, default=8000, help="监听端口")
    p_serve.add_argument("--state-dir", default=".powercontext", help="状态目录（相对当前目录）")
    p_serve.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    return parser


# --------------------------------------------------------------------- index
def _cmd_index(args: argparse.Namespace) -> int:
    from . import ingest, seekdb

    settings = SeekDBSettings.from_env()
    if args.database:
        settings = SeekDBSettings(
            settings.host,
            settings.port,
            settings.user,
            settings.password,
            args.database,
        )
    root = Path(args.root)
    if not root.is_dir():
        print(f"错误：--root 指向的目录不存在：{root}", file=sys.stderr)
        return 2

    conn = seekdb.connect_database(settings)
    try:
        parser = seekdb.ensure_schema(conn)
        if args.reset:
            seekdb.truncate_documents(conn)
        documents = list(ingest.iter_documents(root))
        written = seekdb.sync_documents(conn, documents, f"{root.resolve().name}/")
        counts = seekdb.count_documents(conn)
    finally:
        conn.close()

    result = {
        "root": str(root),
        "database": settings.database,
        "dsn": settings.dsn,
        "parser": parser,
        "written": written,
        "doc_counts": counts,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"已从 {root} 导入 {written} 条文档到 seekdb {settings.dsn}")
        print(f"全文索引解析器: {parser}")
        print(f"库内统计: {counts}")
    return 0


# --------------------------------------------------------------------- build
def _cmd_build(args: argparse.Namespace) -> int:
    from .build import build_context

    context = build_context(
        args.task,
        args.out,
        limit=args.limit,
        include_state=not args.no_state,
        prefer=args.prefer,
    )
    if args.json:
        print(json.dumps(context, ensure_ascii=False, indent=2))
        return 0
    retrieval = context["retrieval"]
    print(f"已写出上下文: {args.out}")
    print(
        f"检索模式: {retrieval['mode']}（seekdb {retrieval['dsn']}，解析器 {retrieval['parser']}）"
    )
    print(
        f"规格 {len(context['specs'])} 条 / 代码 {len(context['code'])} 条 / 测试 {len(context['tests'])} 条"
        f" / 整文件 {len(context.get('files') or [])} 份"
    )
    state = context["state"]
    print(
        f"任务状态: completed={len(state['completed'])} todo={len(state['todo'])} "
        f"verification={len(state['verification'])}"
        f"（PowerContext {'可用' if retrieval['powercontext_available'] else '不可用'}）"
    )
    return 0


# -------------------------------------------------------------------- status
def _cmd_status(args: argparse.Namespace) -> int:
    from .state import PowerContextClient, PowerContextError, load_state

    settings = PowerContextSettings.from_env()
    if args.scope:
        settings = PowerContextSettings(
            settings.base_url, args.scope, settings.timeout, settings.api_token
        )
    client = PowerContextClient(settings)
    try:
        client.capabilities()
    except PowerContextError as exc:
        print(f"无法读取 PowerContext 状态：{exc}", file=sys.stderr)
        return 1

    state = load_state(client)
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    print(f"PowerContext: {settings.base_url}")
    print(f"scope 键: {settings.scope_id}")
    print(f"scope id: {state.get('scope_id') or '<未解析>'}")
    task = state.get("task")
    if task:
        print(f"当前任务: {task}")
    for kind in ("completed", "todo", "verification"):
        items = state.get(kind) or []
        print(f"{kind} ({len(items)}):")
        for item in items:
            print(f"  - {item}")
    if not any(state.get(k) for k in ("completed", "todo", "verification")):
        print("  （暂无记录）")
    return 0


# ---------------------------------------------------------------------- save
def _cmd_save(args: argparse.Namespace) -> int:
    from .state import PowerContextError, save_progress

    try:
        result = save_progress(args.kind, args.text, reason=args.reason)
    except PowerContextError as exc:
        print(f"写入 PowerContext 失败：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"已写入 {args.kind}: {args.text}")
    return 0


# ----------------------------------------------------------------------- fix
def _cmd_fix(args: argparse.Namespace) -> int:
    from .config import MissingApiKeyError
    from .fix import run_fix
    from .minimax_api import MiniMaxError

    try:
        result = run_fix(
            args.ctx,
            out_log=args.out,
            target=args.target,
            dry_run=args.dry_run,
        )
    except MissingApiKeyError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except MiniMaxError as exc:
        print(f"MiniMax 调用失败：{exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.turn.to_record(), ensure_ascii=False, indent=2))
        return 0
    print(f"模型：{result.model}")
    print(f"目标文件：{result.target}")
    print(f"是否落地：{'是' if result.applied else '否（dry-run）'}")
    print(f"是否有改动：{'是' if result.changed else '否'}")
    if result.changed:
        print("--- diff ---")
        print(result.diff, end="")
    if args.out:
        print(f"调用记录已写入 {args.out}")
    return 0


# -------------------------------------------------------------------- verify
def _cmd_verify(args: argparse.Namespace) -> int:
    import datetime
    import subprocess

    root = Path(args.root)
    if not root.is_dir():
        print(f"错误：--root 指向的目录不存在：{root}", file=sys.stderr)
        return 2
    tests = sorted(str(p) for p in root.glob("tests/test_*.py"))
    if not tests:
        tests = sorted(str(p) for p in root.rglob("test_*.py"))
    if not tests:
        print(f"错误：在 {root} 下没有找到测试文件", file=sys.stderr)
        return 2

    cmd = [sys.executable, "-m", "pytest", *tests, "-v"]
    display = f"python -m pytest {' '.join(tests)} -v"
    proc = subprocess.run(cmd, cwd=Path.cwd(), capture_output=True, text=True)
    output = (proc.stdout or "") + (proc.stderr or "")

    summary = ""
    for line in reversed(output.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            summary = line.strip()
            break

    if proc.returncode == 0:
        conclusion = "结论: 目标测试与回归测试全部通过（退出码 0）。"
    else:
        conclusion = (
            "结论: 存在失败用例（退出码非 0），失败详情见上方断言信息；"
            "失败必须是断言失败而非 import error 或环境错误。"
        )

    header = [
        f"# {args.label}目标测试执行记录",
        "",
        f"时间: {datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"工作目录: {Path.cwd()}",
        f"命令: $ {display}",
        f"实际解释器: {sys.executable} (Python {sys.version.split()[0]})",
        "",
        "--- 命令输出 ---",
        output.rstrip("\n"),
        "",
        f"--- 退出码: {proc.returncode} {'（非 0）' if proc.returncode else ''}---",
        "",
        f"{conclusion}",
        f"用例统计: {summary}",
        "",
    ]
    text = "\n".join(header)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"已写入 {out}")

    print(output, end="")
    print(f"退出码: {proc.returncode}")
    return proc.returncode


# ---------------------------------------------------------------------- serve
def _cmd_serve(args: argparse.Namespace) -> int:
    """启动 PowerContext 服务，并把状态固定落到一个确定的 SQLite 文件。

    为什么不直接用 ``powercontext server run``：那条命令的数据库路径取自
    ``POWERCONTEXT_SERVER_DATABASE_URL``，**不设置时走服务端默认路径**。
    于是「改了环境变量起服务」和「没改环境变量起服务」会指向两个不同的库，
    表现为「重启之后状态突然没了」。这里把路径固定死，重启多少次都指向同一个文件。
    """
    import os
    import shutil
    import subprocess

    state_dir = Path(args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    db_file = state_dir / "powercontext.db"

    executable = shutil.which("powercontext")
    if executable is None:
        candidate = Path(sys.executable).parent / "powercontext"
        if candidate.exists():
            executable = str(candidate)
    if executable is None:
        print(
            '错误：找不到 powercontext 可执行文件。请先 pip install "powercontext[cli,server]==1.2.0"',
            file=sys.stderr,
        )
        return 2

    env = dict(os.environ)
    env["POWERCONTEXT_SERVER_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file}"
    env["POWERCONTEXT_SERVER_HTTP_HOST"] = args.host
    env["POWERCONTEXT_SERVER_HTTP_PORT"] = str(args.port)
    env.setdefault("POWERCONTEXT_HOME", str(state_dir))

    cmd = [
        executable, "server", "run",
        "--host", args.host,
        "--port", str(args.port),
        "--no-env-file",
    ]
    if args.json:
        print(json.dumps(
            {
                "command": cmd,
                "database_url": env["POWERCONTEXT_SERVER_DATABASE_URL"],
                "base_url": f"http://{args.host}:{args.port}",
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(f"PowerContext 服务：{args.host}:{args.port}")
        print(f"状态文件：{db_file}")
        print("（Ctrl-C 停止；重启后状态仍在同一文件里）")
    try:
        return subprocess.call(cmd, env=env)
    except KeyboardInterrupt:  # pragma: no cover
        return 0


_COMMANDS = {
    "index": _cmd_index,
    "build": _cmd_build,
    "status": _cmd_status,
    "save": _cmd_save,
    "fix": _cmd_fix,
    "verify": _cmd_verify,
    "serve": _cmd_serve,
}

def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _COMMANDS[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:  # pragma: no cover
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
