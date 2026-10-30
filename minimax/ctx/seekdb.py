"""seekdb 连接与 ``eng_docs`` 表结构管理。

表结构按赛题要求固定为四列 ``(kind, path, symbol, content)``，
外加：

* 复合主键 ``(kind, path, symbol)`` —— 让「同一路径同一符号」天然唯一，
  重复导入时可以用 ``ON DUPLICATE KEY UPDATE`` 做幂等 upsert；
* ``KEY idx_path (path)`` —— 支持按文件整体失效；
* ``FULLTEXT KEY ft_content (content) WITH PARSER ngram`` —— 全文索引。

关于解析器：seekdb (OceanBase seekdb v1.4.0.0) 的默认分词器按空白切词，
中文整段会被当成一个 token，查询「优惠」这类中文词**匹配不到**；
``WITH PARSER ngram`` 会把 CJK 切成 bigram，中英文都能命中，
因此默认使用 ngram。若某个 seekdb 版本不支持 ngram，
:func:`ensure_schema` 会自动退回到默认解析器，检索侧再退化为关键词打分
（见 :mod:`ctx.retrieval`），两条路径都是真实 SQL 查询。

:func:`ensure_schema` 是**自纠正**的：它会检查实际的主键与全文索引，
发现与目标结构不一致（例如早期实验留下的 ``PRIMARY KEY (id)`` 自增主键）
就重建表 / 重建索引，保证重复导入不会产生重复行。
``eng_docs`` 是可从源文件完全重建的派生索引，重建是安全的。
"""

from __future__ import annotations

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from .config import SeekDBSettings

__all__ = [
    "SeekDBError",
    "connect",
    "connect_database",
    "ensure_database",
    "ensure_schema",
    "describe_schema",
    "detect_fulltext_parser",
    "upsert_documents",
    "delete_by_path",
    "truncate_documents",
    "count_documents",
    "fetch_documents",
    "sync_documents",
]

TABLE_NAME = "eng_docs"

_CREATE_TABLE_PLAIN = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
  kind    VARCHAR(32)  NOT NULL,
  path    VARCHAR(512) NOT NULL,
  symbol  VARCHAR(255) NOT NULL DEFAULT '',
  content TEXT,
  PRIMARY KEY (kind, path, symbol),
  KEY idx_path (path)
) DEFAULT CHARSET utf8mb4
"""

_CREATE_TABLE_NGRAM = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
  kind    VARCHAR(32)  NOT NULL,
  path    VARCHAR(512) NOT NULL,
  symbol  VARCHAR(255) NOT NULL DEFAULT '',
  content TEXT,
  PRIMARY KEY (kind, path, symbol),
  KEY idx_path (path),
  FULLTEXT KEY ft_content (content) WITH PARSER ngram
) DEFAULT CHARSET utf8mb4
"""

_ADD_FULLTEXT_NGRAM = f"ALTER TABLE {TABLE_NAME} ADD FULLTEXT KEY ft_content (content) WITH PARSER ngram"
_ADD_FULLTEXT_DEFAULT = f"ALTER TABLE {TABLE_NAME} ADD FULLTEXT KEY ft_content (content)"

_EXPECTED_PRIMARY_KEY = ["kind", "path", "symbol"]


class SeekDBError(RuntimeError):
    """seekdb 连接或执行出错。"""


def connect(settings: SeekDBSettings, *, database: str | None = None) -> Connection:
    """建立连接。``database`` 为 ``None`` 时不指定默认库。"""
    try:
        conn = pymysql.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            database=database,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=DictCursor,
            connect_timeout=10,
            read_timeout=120,
            write_timeout=120,
        )
    except Exception as exc:  # pragma: no cover - 依赖外部服务
        raise SeekDBError(
            f"无法连接 seekdb {settings.user}@{settings.host}:{settings.port}：{exc}"
        ) from exc
    with conn.cursor() as cur:
        cur.execute("SET NAMES utf8mb4")
    return conn


def ensure_database(conn: Connection, database: str) -> None:
    """确保目标库存在。"""
    safe = database.replace("`", "")
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{safe}` DEFAULT CHARSET utf8mb4")


def connect_database(settings: SeekDBSettings) -> Connection:
    """连接 seekdb，确保目标库存在并已选中。

    这是业务侧应当使用的入口：库不存在会自动建好，连接一定落在目标库上。
    """
    conn = connect(settings)
    try:
        ensure_database(conn, settings.database)
        conn.select_db(settings.database)
    except Exception:
        conn.close()
        raise
    return conn


def _table_exists(conn: Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (TABLE_NAME,),
        )
        row = cur.fetchone()
    return bool(row and row["n"])


def _primary_key_columns(conn: Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = 'PRIMARY' "
            "ORDER BY SEQ_IN_INDEX",
            (TABLE_NAME,),
        )
        return [row["COLUMN_NAME"] for row in cur.fetchall()]


def _fulltext_indexes(conn: Connection) -> dict[str, str]:
    """返回 ``{索引名: 解析器}``，解析器取值 ``ngram`` / ``default``。"""
    with conn.cursor() as cur:
        cur.execute(f"SHOW INDEX FROM {TABLE_NAME}")
        rows = cur.fetchall()
    indexes: dict[str, str] = {}
    for row in rows:
        if str(row.get("Index_type") or "").upper() != "FULLTEXT":
            continue
        name = str(row.get("Key_name"))
        comment = str(row.get("Comment") or "").lower()
        indexes.setdefault(name, "ngram" if "ngram" in comment else "default")
    return indexes


def detect_fulltext_parser(conn: Connection) -> str:
    """返回当前表的全文解析器：``ngram`` / ``default`` / ``none``。"""
    indexes = _fulltext_indexes(conn)
    if not indexes:
        return "none"
    if "ngram" in indexes.values():
        return "ngram"
    return "default"


def describe_schema(conn: Connection) -> dict:
    """返回当前表结构摘要，便于打印与留证。"""
    return {
        "table": TABLE_NAME,
        "primary_key": _primary_key_columns(conn),
        "fulltext_indexes": _fulltext_indexes(conn),
        "parser": detect_fulltext_parser(conn),
    }


def _create_table(conn: Connection) -> None:
    """建表；优先带 ngram 全文索引，不支持则退化为普通表 + 后续单独加索引。"""
    try:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_NGRAM)
        return
    except pymysql.err.Error as exc:
        ngram_error = exc
    with conn.cursor() as cur:
        try:
            cur.execute(_CREATE_TABLE_PLAIN)
        except pymysql.err.Error:
            # 普通建表也失败，说明不是解析器的问题，把原始错误抛出去
            raise ngram_error


def _ensure_fulltext(conn: Connection) -> str:
    """保证表上恰好有一个全文索引，并优先使用 ngram 解析器。"""
    indexes = _fulltext_indexes(conn)
    # 多个全文索引会让 MATCH() 选索引产生歧义，只保留一个；且优先 ngram
    for name, parser in list(indexes.items()):
        if parser == "ngram":
            continue
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX `{name}` ON {TABLE_NAME}")
        indexes.pop(name, None)

    if indexes:
        return detect_fulltext_parser(conn)

    with conn.cursor() as cur:
        try:
            cur.execute(_ADD_FULLTEXT_NGRAM)
            return "ngram"
        except pymysql.err.Error:
            cur.execute(_ADD_FULLTEXT_DEFAULT)
            return "default"


def ensure_schema(conn: Connection) -> str:
    """确保表结构与全文索引就绪，返回实际使用的解析器。

    自纠正逻辑：主键不是 ``(kind, path, symbol)`` 时重建表（派生索引，可重建）。
    """
    if _table_exists(conn):
        if _primary_key_columns(conn) != _EXPECTED_PRIMARY_KEY:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {TABLE_NAME}")
            _create_table(conn)
    else:
        _create_table(conn)

    return _ensure_fulltext(conn)


def sync_documents(conn: Connection, documents, path_prefix: str) -> int:
    """幂等写入文档，并把 ``path_prefix`` 下已不存在的记录裁掉。

    只做 upsert 是不够的：某个文件被删除、或者某个符号被改名之后，
    旧记录会留在库里，检索时就会被召回（拿到过期内容）。
    因此这里同时做一次「按当前文档集合裁剪」，让 ``ctx index`` 成为真正的全量刷新。

    ``path_prefix`` 形如 ``sample_project/``，只裁剪这个前缀下的记录，
    不会动库里其它项目的数据。
    """
    rows = [(doc.kind, doc.path, doc.symbol, doc.content) for doc in documents]
    if rows:
        sql = (
            f"INSERT INTO {TABLE_NAME} (kind, path, symbol, content) VALUES (%s, %s, %s, %s) "
            f"ON DUPLICATE KEY UPDATE content = VALUES(content)"
        )
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
    _prune(conn, rows, path_prefix)
    return len(rows)


def _prune(conn: Connection, rows: list[tuple[str, str, str, str]], path_prefix: str) -> None:
    """删掉 ``path_prefix`` 下不在当前文档集合里的记录。"""
    if not rows:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {TABLE_NAME} WHERE path LIKE %s", (f"{path_prefix}%",)
            )
        return
    keep = ", ".join(["(%s, %s, %s)"] * len(rows))
    params: list = [f"{path_prefix}%"]
    for kind, path, symbol, _content in rows:
        params.extend([kind, path, symbol])
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {TABLE_NAME} WHERE path LIKE %s "
            f"AND (kind, path, symbol) NOT IN ({keep})",
            params,
        )


def upsert_documents(conn: Connection, documents) -> int:
    """幂等写入文档。

    ``(kind, path, symbol)`` 相同即视为同一条记录，内容被整体替换，
    因此「改了规格重新导入」后库里不会再残留旧内容。
    """
    rows = [(doc.kind, doc.path, doc.symbol, doc.content) for doc in documents]
    if not rows:
        return 0
    sql = (
        f"INSERT INTO {TABLE_NAME} (kind, path, symbol, content) VALUES (%s, %s, %s, %s) "
        f"ON DUPLICATE KEY UPDATE content = VALUES(content)"
    )
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def delete_by_path(conn: Connection, path: str) -> int:
    """删除某个文件路径下的全部记录（文件被删除/重命名时使用）。"""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {TABLE_NAME} WHERE path = %s", (path,))
        return cur.rowcount


def truncate_documents(conn: Connection) -> None:
    """清空表。"""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {TABLE_NAME}")


def count_documents(conn: Connection) -> dict:
    """按 kind 统计行数。"""
    with conn.cursor() as cur:
        cur.execute(f"SELECT kind, COUNT(*) AS n FROM {TABLE_NAME} GROUP BY kind")
        return {row["kind"]: row["n"] for row in cur.fetchall()}


def fetch_documents(
    conn: Connection, kind: str, symbol: str, paths: list[str]
) -> list[dict]:
    """按 ``kind`` / ``symbol`` / ``path`` 精确取回文档。

    不走全文检索——用于取「整份文件」这类**必须原样拿到**的内容，
    不能让它被相关度排序淘汰掉。
    """
    if not paths:
        return []
    placeholders = ", ".join(["%s"] * len(paths))
    sql = (
        f"SELECT kind, path, symbol, content FROM {TABLE_NAME} "
        f"WHERE kind = %s AND symbol = %s AND path IN ({placeholders}) ORDER BY path"
    )
    with conn.cursor() as cur:
        cur.execute(sql, [kind, symbol, *paths])
        return list(cur.fetchall())
