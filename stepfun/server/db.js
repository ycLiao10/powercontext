/**
 * seekdb 数据访问层：建库建表、写入、混合检索。
 *
 * 所有检索（纯向量 / 纯全文 / 混合）都在 seekdb 内以**单条 SQL**完成，
 * 前端拿到的就是数据库排好的序，不做任何客户端二次拼装。
 */

import mysql from 'mysql2/promise';
import { config } from './config.js';

/**
 * 建立一个直连 seekdb 的连接（不复用连接池，便于脚本与代理共用同一套语义）。
 *
 * 注意：业务库可能尚不存在（首次运行时），所以这里**不带 database 参数**连接，
 * 连接建立后再创建并选中该库。
 */
export async function connect({ database } = {}) {
  const targetDb = database ?? config.seekdb.database;
  const conn = await mysql.createConnection({
    host: config.seekdb.host,
    port: config.seekdb.port,
    user: config.seekdb.user,
    password: config.seekdb.password,
    charset: 'utf8mb4',
    // seekdb 的 VECTOR 列用字符串字面量写入，形如 '[0.1,0.2]'
    supportBigNumbers: true,
  });
  await conn.query(`CREATE DATABASE IF NOT EXISTS \`${targetDb}\` DEFAULT CHARSET utf8mb4`);
  await conn.query(`USE \`${targetDb}\``);
  return conn;
}

/** 确保业务库存在（connect() 已经做过，这里保留给显式调用场景）。 */
export async function ensureDatabase(conn) {
  const db = config.seekdb.database;
  await conn.query(`CREATE DATABASE IF NOT EXISTS \`${db}\` DEFAULT CHARSET utf8mb4`);
  await conn.query(`USE \`${db}\``);
}

/**
 * 建表 DDL —— 这里是本题最关键的 schema。
 *
 * 两点必须保持不变，否则功能直接不成立（见 tech-notes.md「索引同步模式与增量可见性」）：
 *
 * 1. `FULLTEXT INDEX ... WITH PARSER ngram`
 *    默认分词器按空白切词，中文整段算一个 token，全文检索几乎命中不了。
 *    ngram 之后中英文都能命中。注意 ngram 下必须用 NATURAL LANGUAGE MODE，
 *    BOOLEAN MODE 恒返回 0 行。
 *
 * 2. `VECTOR INDEX ... WITH(..., sync_mode=IMMEDIATE)`
 *    HNSW 向量索引默认 SYNC_MODE=ASYNC：新插入的文档在 `APPROXIMATE` 检索里
 *    **查不到**（实测 3 条命中只返回 1 条）。改成 IMMEDIATE 后写入即可见，
 *    这是「增量导入：写入后刷新索引即可检索」的前提。
 */
export function createTableSql(dim = config.vectorDim) {
  const t = config.seekdb.table;
  return `
    CREATE TABLE IF NOT EXISTS \`${t}\` (
      \`id\`         VARCHAR(128) NOT NULL,
      \`title\`      VARCHAR(512) DEFAULT NULL,
      \`content\`    STRING       DEFAULT NULL,
      \`embedding\`  VECTOR(${dim}) DEFAULT NULL,
      \`metadata\`   JSON         DEFAULT NULL,
      \`created_at\` TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (\`id\`),
      FULLTEXT INDEX \`idx_fts\` (\`content\`) WITH PARSER ngram,
      VECTOR INDEX \`idx_vec\` (\`embedding\`) WITH(
        distance=cosine, type=hnsw, lib=vsag,
        m=16, ef_construction=200, sync_mode=IMMEDIATE
      )
    ) ORGANIZATION = HEAP`.trim();
}

/** 建库 + 建表。 */
export async function ensureSchema(conn) {
  await ensureDatabase(conn);
  await conn.query(createTableSql());
}

/** 把浮点数组序列化成 seekdb 接受的向量字面量。 */
export function toVectorLiteral(vector) {
  if (typeof vector === 'string') return vector;
  if (!Array.isArray(vector)) throw new TypeError('向量必须是数组或字符串');
  return `[${vector.map((v) => Number(v).toFixed(6)).join(',')}]`;
}

/**
 * 写入文档。
 * @param {import('mysql2/promise').Connection} conn
 * @param {Array<{id:string,title?:string,content:string,embedding:number[]|string,metadata?:object}>} docs
 */
export async function insertDocuments(conn, docs) {
  if (!docs.length) return { inserted: 0 };
  const t = config.seekdb.table;
  const values = docs.map((d) => [
    String(d.id),
    d.title ?? null,
    d.content ?? '',
    toVectorLiteral(d.embedding),
    d.metadata ? JSON.stringify(d.metadata) : null,
  ]);
  const placeholders = docs.map(() => '(?,?,?,?,?)').join(',');
  await conn.query(
    `INSERT INTO \`${t}\` (id,title,content,embedding,metadata) VALUES ${placeholders}
     ON DUPLICATE KEY UPDATE title=VALUES(title), content=VALUES(content),
                             embedding=VALUES(embedding), metadata=VALUES(metadata)`,
    values.flat(),
  );
  return { inserted: values.length };
}

/** 文档总数。 */
export async function countDocuments(conn) {
  const t = config.seekdb.table;
  const [rows] = await conn.query(`SELECT COUNT(*) AS n FROM \`${t}\``);
  return Number(rows[0].n);
}

/**
 * 对瞬时的 tablet 错误做指数退避重试。
 *
 * seekdb 的 DDL（TRUNCATE / 建表）会异步重建 tablet，窗口期内紧随其后的
 * DML/DQL 可能报 `tablet does not exist`。这是数据库内部实现细节，
 * 对调用方表现为偶发失败，重试即可稳定通过。
 */
export async function withTabletRetry(fn, { retries = 5, baseDelayMs = 120 } = {}) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await fn();
    } catch (err) {
      const transient = /tablet does not exist|tablet not exist|OB_GET_TABLET/i.test(
        String(err?.message ?? ''),
      );
      if (!transient || attempt >= retries) throw err;
      await new Promise((r) => setTimeout(r, baseDelayMs * (attempt + 1)));
    }
  }
}

/**
 * 清空表。
 *
 * 注意：`TRUNCATE TABLE` 在 seekdb 里是 DDL，会**异步重建 tablet**。
 * 紧接着的 INSERT/SELECT 有概率撞在重建窗口里，报
 * `ER_GET_TABLET_ERROR: tablet does not exist`（实测每 30~40 次清表出现 1 次）。
 * 这里对这类瞬时错误做指数退避重试，把偶发失败变成稳定成功。
 */
export async function truncateDocuments(conn, opts = {}) {
  const t = config.seekdb.table;
  await withTabletRetry(() => conn.query(`TRUNCATE TABLE \`${t}\``), opts);
}

/* ------------------------------------------------------------------ *
 * 检索：三种模式，全部在 seekdb 内单条 SQL 完成
 * ------------------------------------------------------------------ */

/**
 * 纯向量检索（HNSW 近似最近邻）。
 */
export function vectorSearchSql(topK) {
  const t = config.seekdb.table;
  return `
    SELECT id, title, content, metadata,
           cosine_distance(embedding, ?) AS distance
    FROM \`${t}\`
    ORDER BY cosine_distance(embedding, ?) APPROXIMATE
    LIMIT ${Number(topK)}`.trim();
}

/**
 * 纯全文检索（ngram 分词 + NATURAL LANGUAGE MODE）。
 */
export function fulltextSearchSql(topK) {
  const t = config.seekdb.table;
  return `
    SELECT id, title, content, metadata,
           MATCH(content) AGAINST (? IN NATURAL LANGUAGE MODE) AS relevance
    FROM \`${t}\`
    WHERE MATCH(content) AGAINST (? IN NATURAL LANGUAGE MODE)
    ORDER BY relevance DESC
    LIMIT ${Number(topK)}`.trim();
}

/**
 * 混合检索：向量 + 全文，**一条 SQL** 内用 RRF（Reciprocal Rank Fusion）融合两路排名。
 *
 * 两路排名分别来自 seekdb 的两个索引：
 *   - 全文分支：MATCH(content) AGAINST(...)      → TEXT RETRIEVAL SCAN
 *   - 向量分支：ORDER BY cosine_distance(...) APPROXIMATE → VECTOR INDEX SCAN
 * 融合在数据库的 ORDER BY 里完成，前端不做任何合并。
 *
 * RRF 公式：score = Σ 1/(k + rank)，k=60 为经典取值。
 */
export function hybridSearchSql(topK, rrfK = 60, candidateLimit = 50) {
  const t = config.seekdb.table;
  const k = Number(rrfK);
  const cand = Number(candidateLimit);
  const top = Number(topK);
  return `
    SELECT d.id, d.title, d.content, d.metadata,
           COALESCE(ft.r, 0) AS fulltext_rank,
           COALESCE(vv.r, 0) AS vector_rank,
           ROUND(
             COALESCE(1.0 / (${k} + ft.r), 0) +
             COALESCE(1.0 / (${k} + vv.r), 0)
           , 6) AS hybrid_score
    FROM \`${t}\` d
    LEFT JOIN (
      SELECT id,
             ROW_NUMBER() OVER (
               ORDER BY MATCH(content) AGAINST (? IN NATURAL LANGUAGE MODE) DESC
             ) AS r
      FROM \`${t}\`
      WHERE MATCH(content) AGAINST (? IN NATURAL LANGUAGE MODE)
      LIMIT ${cand}
    ) ft ON ft.id = d.id
    LEFT JOIN (
      SELECT id, ROW_NUMBER() OVER (ORDER BY cosine_distance(embedding, ?)) AS r
      FROM (
        SELECT id, embedding FROM \`${t}\`
        ORDER BY cosine_distance(embedding, ?) APPROXIMATE
        LIMIT ${cand}
      ) z
    ) vv ON vv.id = d.id
    WHERE ft.id IS NOT NULL OR vv.id IS NOT NULL
    ORDER BY hybrid_score DESC
    LIMIT ${top}`.trim();
}

/**
 * 统一检索入口。
 * @param {'hybrid'|'vector'|'fulltext'} mode
 */
export async function search(conn, { mode = 'hybrid', query = '', embedding, topK = 5 }) {
  if (mode === 'vector') {
    const vector = toVectorLiteral(embedding);
    const [rows] = await conn.query(vectorSearchSql(topK), [vector, vector]);
    return rows.map((r) => ({ ...r, mode }));
  }
  if (mode === 'fulltext') {
    const [rows] = await conn.query(fulltextSearchSql(topK), [query, query]);
    return rows.map((r) => ({ ...r, mode }));
  }
  if (mode === 'hybrid') {
    const vector = toVectorLiteral(embedding);
    const [rows] = await conn.query(hybridSearchSql(topK), [query, query, vector, vector]);
    return rows.map((r) => ({ ...r, mode }));
  }
  throw new Error(`未知的检索模式：${mode}`);
}
