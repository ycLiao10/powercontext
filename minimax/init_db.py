"""seekdb 建表与灌数据。

一条命令把整个检索底座准备好：建库 → 建 ``eng_docs`` 表 → 建 ngram 全文索引
→ 导入业务工程的规格/代码/测试。

用法::

    python init_db.py                          # 用默认的 sample_project
    python init_db.py --root sample_project    # 显式指定业务工程
    python init_db.py --reset                  # 先清空 eng_docs 再导入

连接参数全部来自环境变量（``SEEKDB_HOST`` / ``SEEKDB_PORT`` / ``SEEKDB_USER`` /
``SEEKDB_PASSWORD``），默认值是本地开发用的 ``127.0.0.1:2881``、``root``、空密码。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ctx import ingest, seekdb  # noqa: E402
from ctx.config import SeekDBSettings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python init_db.py", description="seekdb 建表与灌数据"
    )
    parser.add_argument("--root", default="sample_project", help="业务工程根目录")
    parser.add_argument("--database", default=None, help="覆盖 SEEKDB_DATABASE")
    parser.add_argument("--reset", action="store_true", help="先清空 eng_docs 再导入")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    settings = SeekDBSettings.from_env()
    if args.database:
        settings = SeekDBSettings(
            settings.host, settings.port, settings.user, settings.password, args.database
        )
    root = Path(args.root)
    if not root.is_dir():
        print(f"错误：业务工程目录不存在：{root}", file=sys.stderr)
        return 2

    conn = seekdb.connect_database(settings)
    try:
        parser_used = seekdb.ensure_schema(conn)
        if args.reset:
            seekdb.truncate_documents(conn)
        documents = list(ingest.iter_documents(root))
        written = seekdb.sync_documents(conn, documents, f"{root.resolve().name}/")
        counts = seekdb.count_documents(conn)
    finally:
        conn.close()

    result = {
        "database": settings.database,
        "dsn": settings.dsn,
        "table": seekdb.TABLE_NAME,
        "parser": parser_used,
        "root": str(root),
        "written": written,
        "doc_counts": counts,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"数据库 {settings.database} 就绪（seekdb {settings.dsn}）")
        print(f"表 {seekdb.TABLE_NAME}：kind/path/symbol/content，全文解析器 {parser_used}")
        print(f"已导入 {written} 条文档，库内统计：{counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
