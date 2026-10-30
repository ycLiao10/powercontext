"""在 seekdb 里检索工程资料。

检索**全部走真实 SQL**，不读本地文件：

1. **全文检索（首选）** —— ``MATCH(content) AGAINST (... IN NATURAL LANGUAGE MODE)``，
   配合 ``FULLTEXT KEY ft_content (content) WITH PARSER ngram``。
   seekdb v1.4.0.0 实测：中文 bigram 与英文词都能命中，不存在的词返回 0 行。
2. **关键词打分（退化路径）** —— 当该 seekdb 版本不支持可用的全文解析器时，
   退化为在 SQL 里用 ``LIKE`` + ``CASE WHEN`` 对关键词计数打分，
   结果仍然是数据库返回的行，不是内存里拼出来的。

:func:`search` 会自动在两条路径之间选择，并在返回值里如实标注实际使用的模式。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymysql
from pymysql.connections import Connection

__all__ = ["Hit", "search", "fulltext_search", "keyword_search", "extract_keywords"]

_ASCII = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_MAX_QUERY_CHARS = 400
_MAX_KEYWORDS = 16


@dataclass(frozen=True)
class Hit:
    """一条检索命中。"""

    kind: str
    path: str
    symbol: str
    content: str
    score: float


def extract_keywords(query: str) -> list[str]:
    """把任务描述拆成关键词：英文词 + 中文 bigram。"""
    keywords: list[str] = []
    for word in _ASCII.findall(query):
        lowered = word.lower()
        if lowered not in keywords:
            keywords.append(lowered)
    for run in _CJK.findall(query):
        if len(run) == 1:
            candidates = [run]
        else:
            candidates = [run[i : i + 2] for i in range(len(run) - 1)]
            if len(run) <= 4:
                candidates.append(run)
        for candidate in candidates:
            if candidate not in keywords:
                keywords.append(candidate)
    return keywords[:_MAX_KEYWORDS]


def fulltext_search(
    conn: Connection, query: str, kinds: list[str], limit: int
) -> list[Hit]:
    """全文检索。数据库若不支持会抛异常，由调用方决定是否退化。"""
    text = query.strip()[:_MAX_QUERY_CHARS]
    if not text:
        return []
    placeholders = ", ".join(["%s"] * len(kinds))
    sql = (
        "SELECT kind, path, symbol, content, "
        "       MATCH(content) AGAINST (%s IN NATURAL LANGUAGE MODE) AS score "
        "FROM eng_docs "
        f"WHERE MATCH(content) AGAINST (%s IN NATURAL LANGUAGE MODE) "
        f"  AND kind IN ({placeholders}) "
        "ORDER BY score DESC, path ASC, symbol ASC "
        "LIMIT %s"
    )
    params: list = [text, text, *kinds, limit]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        Hit(
            kind=row["kind"],
            path=row["path"],
            symbol=row["symbol"],
            content=row["content"] or "",
            score=float(row["score"] or 0.0),
        )
        for row in rows
    ]


def keyword_search(
    conn: Connection, query: str, kinds: list[str], limit: int
) -> list[Hit]:
    """关键词打分（退化路径）：SQL 里用 LIKE 计数。"""
    keywords = extract_keywords(query)
    if not keywords:
        return []
    kind_placeholders = ", ".join(["%s"] * len(kinds))
    score_terms = " + ".join(
        ["CASE WHEN content LIKE CONCAT('%%', %s, '%%') THEN 1 ELSE 0 END"] * len(keywords)
    )
    where_terms = " OR ".join(["content LIKE CONCAT('%%', %s, '%%')"] * len(keywords))
    sql = (
        "SELECT kind, path, symbol, content, "
        f"       ({score_terms}) AS score "
        "FROM eng_docs "
        f"WHERE kind IN ({kind_placeholders}) AND ({where_terms}) "
        "ORDER BY score DESC, path ASC, symbol ASC "
        "LIMIT %s"
    )
    params: list = [*kinds, *keywords, *keywords, limit]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        Hit(
            kind=row["kind"],
            path=row["path"],
            symbol=row["symbol"],
            content=row["content"] or "",
            score=float(row["score"] or 0.0),
        )
        for row in rows
    ]


def search(
    conn: Connection,
    query: str,
    kinds: list[str],
    limit: int,
    *,
    prefer: str = "auto",
) -> tuple[list[Hit], str]:
    """检索指定 ``kinds``，返回 ``(命中列表, 实际使用的检索模式)``。

    ``prefer``：

    * ``auto``（默认）—— 先全文；全文不可用或无命中时退化为关键词打分；
    * ``fulltext`` —— 只用全文；
    * ``keyword`` —— 只用关键词打分。
    """
    mode = "none"
    hits: list[Hit] = []

    if prefer in ("auto", "fulltext"):
        try:
            hits = fulltext_search(conn, query, kinds, limit)
            mode = "fulltext"
        except pymysql.err.Error:
            if prefer == "fulltext":
                raise
            hits = []

    if not hits and prefer in ("auto", "keyword"):
        hits = keyword_search(conn, query, kinds, limit)
        mode = "keyword" if hits else mode

    return hits, mode
