/**
 * 生成 SQL 日志样例，供评审直接确认「混合检索是在 seekdb 内一条 SQL 完成的」。
 *
 * 用法：node scripts/gen-sql-log-samples.js
 *
 * 会在 evidence/sql-log.txt 里留下三份样例：
 *   1. 纯向量检索（label=vector）
 *   2. 纯全文检索（label=fulltext）
 *   3. 混合检索（label=hybrid）—— 一条 SQL 里同时包含
 *      MATCH(content) AGAINST(...) 与 cosine_distance(...) APPROXIMATE，
 *      并在数据库内用 RRF 融合两路排名
 * 外加一份写入样例（label=insert），证明向量确实存进了 seekdb。
 *
 * 日志开关：SQL_LOG_ENABLED=0 可关闭，SQL_LOG_FILE 可改路径。
 */

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { connect, ensureSchema, insertDocuments, countDocuments } from '../server/db.js';
import { loggedQuery, resetSqlLog, sqlLogPath } from '../server/sql-log.js';
import { hybridSearchSql, vectorSearchSql, fulltextSearchSql } from '../server/db.js';
import { embedTexts } from './offline-eval-embedder.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STEPFUN_ROOT = path.resolve(__dirname, '..');

const SAMPLE_DOCS = [
  {
    id: 'sample-01',
    title: '向量索引同步模式与增量可见性',
    content: 'HNSW 向量索引默认使用异步同步模式，新插入的文档在短时间内检索不到；改为立即同步模式后写入即可见。',
    metadata: { domain: '数据库', tags: ['向量索引', 'HNSW', '同步'] },
  },
  {
    id: 'sample-02',
    title: '全文检索与向量检索的选型',
    content: '全文检索擅长精确词匹配，向量检索能捕捉语义相似度，混合检索结合两者，用 RRF 融合两路排名。',
    metadata: { domain: '数据库', tags: ['混合检索', '全文索引'] },
  },
  {
    id: 'sample-03',
    title: '缓存穿透击穿雪崩治理',
    content: '缓存穿透用布隆过滤器拦截，击穿用互斥重建，雪崩靠随机过期与多级缓存缓解。',
    metadata: { domain: '缓存', tags: ['缓存'] },
  },
];

async function main() {
  const conn = await connect();
  try {
    await ensureSchema(conn);

    // 清空日志，只留本次样例
    resetSqlLog();
    console.log(`✓ 已重置 SQL 日志：${sqlLogPath}`);

    // 先清空表，保证样例结果干净
    await conn.query(`TRUNCATE TABLE \`${process.env.SEEKDB_TABLE ?? 'documents'}\``);

    // ---- 样例 0：写入（证明向量确实存进 seekdb）----
    const embeddings = await embedTexts(SAMPLE_DOCS.map((d) => `${d.title}。${d.content}`));
    const t = process.env.SEEKDB_TABLE ?? 'documents';
    const placeholders = SAMPLE_DOCS.map(() => '(?,?,?,?,?)').join(',');
    const values = SAMPLE_DOCS.flatMap((d, i) => [
      d.id,
      d.title,
      d.content,
      `[${embeddings[i].map((v) => Number(v).toFixed(6)).join(',')}]`,
      JSON.stringify(d.metadata),
    ]);
    const insertSql = `INSERT INTO \`${t}\` (id,title,content,embedding,metadata) VALUES ${placeholders}
      ON DUPLICATE KEY UPDATE title=VALUES(title), content=VALUES(content),
                              embedding=VALUES(embedding), metadata=VALUES(metadata)`;
    await loggedQuery(conn, 'insert', insertSql, values, '写入样例文档：向量由浏览器/本地模型算出后存入 seekdb');
    console.log(`✓ 写入 ${SAMPLE_DOCS.length} 条样例文档，当前总数 = ${await countDocuments(conn)}`);

    const query = '混合检索 同步模式';
    const vector = `[${embeddings[0].map((v) => Number(v).toFixed(6)).join(',')}]`;

    // ---- 样例 1：纯向量 ----
    const vecRows = await loggedQuery(
      conn,
      'vector',
      vectorSearchSql(5),
      [vector, vector],
      '纯向量检索：只走 HNSW 向量索引',
    );
    console.log(`✓ 纯向量检索 → ${vecRows.length} 条`);

    // ---- 样例 2：纯全文 ----
    const ftRows = await loggedQuery(
      conn,
      'fulltext',
      fulltextSearchSql(5),
      [query, query],
      '纯全文检索：只走 ngram 全文索引',
    );
    console.log(`✓ 纯全文检索 → ${ftRows.length} 条`);

    // ---- 样例 3：混合（一条 SQL，RRF 融合）----
    const hybRows = await loggedQuery(
      conn,
      'hybrid',
      hybridSearchSql(5),
      [query, query, vector, vector],
      '混合检索：向量 + 全文，两路排名在**同一条 SQL** 内用 RRF 融合，由 seekdb 完成',
    );
    console.log(`✓ 混合检索 → ${hybRows.length} 条`);
    hybRows.forEach((r, i) => {
      console.log(`   ${i + 1}. [${r.id}] ${r.title}  score=${r.hybrid_score} (ft=${r.fulltext_rank}, vec=${r.vector_rank})`);
    });

    console.log(`\n✓ 三份样例已写入：${path.relative(STEPFUN_ROOT, sqlLogPath)}`);
    console.log('  评审可用 grep 查看各模式实际执行的 SQL：');
    console.log(`    grep -A 40 "label=hybrid" ${path.relative(STEPFUN_ROOT, sqlLogPath)}`);
  } finally {
    await conn.end().catch(() => {});
  }
}

main().catch((err) => {
  console.error(`❌ 生成样例失败：${err.message}`);
  process.exitCode = 1;
});
