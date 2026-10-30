from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
from pathlib import Path

SCHEMA = """
create table if not exists documents (
    id integer primary key autoincrement,
    title text not null,
    content_hash text not null unique,
    added_at real not null
);
create table if not exists chunks (
    id integer primary key autoincrement,
    document_id integer not null references documents(id) on delete cascade,
    ordinal integer not null,
    start_offset integer not null,
    end_offset integer not null,
    text text not null
);
create virtual table if not exists chunks_fts using fts5(
    text, content='chunks', content_rowid='id', tokenize='trigram'
);
create trigger if not exists chunks_ai after insert on chunks begin
    insert into chunks_fts(rowid, text) values (new.id, new.text);
end;
create trigger if not exists chunks_ad after delete on chunks begin
    insert into chunks_fts(chunks_fts, rowid, text) values ('delete', old.id, old.text);
end;
create table if not exists chunk_vectors (
    chunk_id integer primary key references chunks(id) on delete cascade,
    model text not null,
    dim integer not null,
    vector blob not null
);
"""


def chunk_text(text: str, size: int = 600, overlap: int = 80) -> list[tuple[str, int, int]]:
    chunks: list[tuple[str, int, int]] = []
    length = len(text)
    start = 0
    while start < length:
        end = min(start + size, length)
        if end < length:
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("。"), window.rfind("\n"))
            if cut > size // 2:
                end = start + cut + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append((chunk, start, end))
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return chunks


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def add_document(self, title: str, text: str, added_at: float) -> tuple[int | None, list[tuple[int, str, int, int]]]:
        """写入文档与其分块。返回 (document_id, 分块列表)；重复内容返回 (None, [])。"""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        try:
            cursor = self._conn.execute(
                "insert into documents(title, content_hash, added_at) values (?, ?, ?)",
                (title, digest, added_at),
            )
        except sqlite3.IntegrityError:
            return None, []
        document_id = int(cursor.lastrowid)
        rows = []
        for ordinal, (chunk, start, end) in enumerate(chunk_text(text), start=1):
            chunk_cursor = self._conn.execute(
                "insert into chunks(document_id, ordinal, start_offset, end_offset, text) values (?, ?, ?, ?, ?)",
                (document_id, ordinal, start, end, chunk),
            )
            rows.append((int(chunk_cursor.lastrowid), chunk, ordinal, start))
        self._conn.commit()
        return document_id, rows

    def documents(self) -> list[dict]:
        rows = self._conn.execute(
            """
            select d.id, d.title, d.added_at, count(c.id) as chunk_count
            from documents d left join chunks c on c.document_id = d.id
            group by d.id order by d.added_at desc
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def chunk_count(self) -> int:
        return int(self._conn.execute("select count(*) from chunks").fetchone()[0])

    def search(self, query: str, limit: int = 5) -> list[dict]:
        query = query.strip()
        if not query:
            return []

        hits = {hit["id"]: hit for hit in self._search_fts(f'"{query}"', limit)}
        if len(hits) < limit:
            terms = _query_terms(query)[:32]
            if terms:
                for hit in self._search_fts(" OR ".join(f'"{term}"' for term in terms), limit):
                    if hit["id"] not in hits or hit["score"] > hits[hit["id"]]["score"]:
                        hits[hit["id"]] = hit
        if not hits and len(query) < 3:
            hits = {hit["id"]: hit for hit in self._search_like(query, limit)}
        return sorted(hits.values(), key=lambda hit: hit["score"], reverse=True)[:limit]

    def _search_fts(self, match: str, limit: int) -> list[dict]:
        try:
            rows = self._conn.execute(
                """
                select c.id, c.ordinal, c.start_offset, c.end_offset, c.text,
                       d.id as document_id, d.title as document_title,
                       -bm25(chunks_fts) as score
                from chunks_fts
                join chunks c on c.id = chunks_fts.rowid
                join documents d on d.id = c.document_id
                where chunks_fts match ?
                order by score desc
                limit ?
                """,
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]

    def _search_like(self, query: str, limit: int) -> list[dict]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._conn.execute(
            """
            select c.id, c.ordinal, c.start_offset, c.end_offset, c.text,
                   d.id as document_id, d.title as document_title,
                   0.0 as score
            from chunks c join documents d on d.id = c.document_id
            where c.text like ? escape '\\'
            limit ?
            """,
            (f"%{escaped}%", limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_vectors(self, model: str, vectors: list[tuple[int, list[float]]]) -> None:
        for chunk_id, vector in vectors:
            blob = struct.pack(f"<{len(vector)}f", *vector)
            self._conn.execute(
                "insert or replace into chunk_vectors(chunk_id, model, dim, vector) values (?, ?, ?, ?)",
                (chunk_id, model, len(vector), blob),
            )
        self._conn.commit()

    def vector_search(self, query_vector: list[float], limit: int) -> list[dict]:
        rows = self._conn.execute(
            """
            select v.chunk_id, v.dim, v.vector,
                   c.ordinal, c.start_offset, c.end_offset, c.text,
                   d.id as document_id, d.title as document_title
            from chunk_vectors v
            join chunks c on c.id = v.chunk_id
            join documents d on d.id = c.document_id
            """
        ).fetchall()
        if not rows:
            return []
        scored = []
        for row in rows:
            vector = struct.unpack(f"<{row['dim']}f", row["vector"])
            scored.append((_cosine(query_vector, vector), row))
        scored.sort(key=lambda item: item[0], reverse=True)
        results = []
        for score, row in scored[:limit]:
            if score <= 0:
                continue
            results.append(
                {
                    "id": row["chunk_id"],
                    "ordinal": row["ordinal"],
                    "start_offset": row["start_offset"],
                    "end_offset": row["end_offset"],
                    "text": row["text"],
                    "document_id": row["document_id"],
                    "document_title": row["document_title"],
                    "score": score,
                }
            )
        return results

    def chunks_without_vectors(self, model: str) -> list[tuple[int, str]]:
        rows = self._conn.execute(
            """
            select c.id, c.text from chunks c
            left join chunk_vectors v on v.chunk_id = c.id and v.model = ?
            where v.chunk_id is null
            """,
            (model,),
        ).fetchall()
        return [(int(row["id"]), row["text"]) for row in rows]


def _query_terms(query: str) -> list[str]:
    """把查询拆成 FTS trigram 可命中的词元：ASCII 词整词保留，CJK 连续段切成三元组。"""
    terms: list[str] = []
    run: list[str] = []
    run_is_ascii = False

    def flush() -> None:
        if not run:
            return
        text = "".join(run)
        if run_is_ascii:
            if len(text) >= 2:
                terms.append(text)
        elif len(text) <= 3:
            terms.append(text)
        else:
            terms.extend(text[index:index + 3] for index in range(len(text) - 2))
        run.clear()

    for char in query:
        if not char.isalnum():
            flush()
            continue
        char_is_ascii = char.isascii()
        if run and char_is_ascii != run_is_ascii:
            flush()
        run_is_ascii = char_is_ascii
        run.append(char)
    flush()
    return list(dict.fromkeys(terms))


def _cosine(left, right) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
