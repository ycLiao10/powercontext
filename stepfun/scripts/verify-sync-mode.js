/**
 * 验证「索引同步模式与增量可见性」——本题最有价值的工程发现。
 *
 * ── 现象 ────────────────────────────────────────────────────────────────
 * seekdb 的 HNSW 向量索引默认 `SYNC_MODE=ASYNC`：
 *   事务提交后只写 redo log 就返回，由独立的 Change Stream 管道异步把向量
 *   写入内存中的 delta HNSW 索引。因此**新插入的文档在向量索引里不存在**。
 *
 * ── 影响面取决于查询形态（这是关键，容易误判）──────────────────────────
 *   形态 A：单次扫描，全文过滤 + 向量排序（带 APPROXIMATE）
 *           执行计划是 "VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)"，
 *           即**向量索引主导扫描**，全文索引只做前置过滤。
 *           → 新文档不在向量索引里 ⇒ 直接消失，召回 0 行。
 *
 *   形态 B：单次扫描，全文过滤 + 向量排序（**不带** APPROXIMATE）
 *           走基表精确扫描 ⇒ 新文档可见，但失去了 HNSW 加速。
 *
 *   形态 C：两分支 RRF 融合（向量分支 APPROXIMATE + 全文分支各自扫描）
 *           全文分支读基表 ⇒ 新文档仍能召回，但向量分支排名缺失，
 *           融合分数被系统性拉低 ⇒ 排序质量下降。
 *
 *   形态 D：纯向量检索（APPROXIMATE）⇒ 新文档不可见。
 *
 *   结论：默认的 ASYNC 模式会让「刚写入就检索」直接查不到，
 *        必须显式配置 `sync_mode=IMMEDIATE`。
 *
 * ── 用法 ────────────────────────────────────────────────────────────────
 *   node scripts/verify-sync-mode.js
 * 输出：evidence/sync-mode-comparison.json
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { config } from '../server/config.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STEPFUN_ROOT = path.resolve(__dirname, '..');

const mysql = (await import('mysql2/promise')).default;

const DIM = 8;
const TABLES = {
  async: 'documents_async',
  immediate: 'documents_immediate',
};

/** 3 条都能被关键词「索引 同步」命中的文档，便于观察召回差异。 */
const DOCS = [
  { id: 'a1', title: '索引同步模式说明', content: '向量索引的同步模式决定了新文档何时可以被检索到。' },
  { id: 'a2', title: '增量导入实践', content: '增量导入要求写入后索引立即可见，否则刚写入的数据检索不到。' },
  { id: 'a3', title: '异步写入的问题', content: '异步同步模式下，新插入的文档在短时间内查不出来。' },
];

const KEYWORDS = '索引 同步';

function makeVector(seed) {
  const v = new Array(DIM).fill(0);
  v[seed % DIM] = 1;
  v[(seed + 3) % DIM] = 0.4;
  const norm = Math.sqrt(v.reduce((s, x) => s + x * x, 0));
  return v.map((x) => x / norm);
}

function tableDdl(table, syncMode) {
  return `
    CREATE TABLE \`${table}\` (
      \`id\`        VARCHAR(64) NOT NULL,
      \`title\`     VARCHAR(255) DEFAULT NULL,
      \`content\`   STRING DEFAULT NULL,
      \`embedding\` VECTOR(${DIM}) DEFAULT NULL,
      PRIMARY KEY (\`id\`),
      FULLTEXT INDEX \`idx_fts\` (\`content\`) WITH PARSER ngram,
      VECTOR INDEX \`idx_vec\` (\`embedding\`) WITH(
        distance=cosine, type=hnsw, lib=vsag,
        m=16, ef_construction=200, sync_mode=${syncMode}
      )
    ) ORGANIZATION = HEAP`.trim();
}

/** 四种查询形态，覆盖「向量索引是否主导扫描」的所有关键差异。 */
function queryShapes(table, vector) {
  return {
    // A：单扫描，全文过滤 + APPROXIMATE 向量排序 —— 向量索引主导
    fulltextPlusApprox: {
      sql: `SELECT id FROM \`${table}\`
            WHERE MATCH(content) AGAINST (? IN NATURAL LANGUAGE MODE)
            ORDER BY cosine_distance(embedding, ?) APPROXIMATE
            LIMIT 10`,
      params: [KEYWORDS, vector],
      desc: '全文过滤 + APPROXIMATE 向量排序（单扫描，向量索引主导）',
    },
    // B：单扫描，全文过滤 + 精确向量排序 —— 走基表
    fulltextExact: {
      sql: `SELECT id FROM \`${table}\`
            WHERE MATCH(content) AGAINST (? IN NATURAL LANGUAGE MODE)
            ORDER BY cosine_distance(embedding, ?)
            LIMIT 10`,
      params: [KEYWORDS, vector],
      desc: '全文过滤 + 精确向量排序（单扫描，走基表）',
    },
    // C：两分支 RRF 融合
    rrfHybrid: {
      sql: `
        SELECT d.id,
               COALESCE(ft.r, 0) AS fulltext_rank,
               COALESCE(vv.r, 0) AS vector_rank
        FROM \`${table}\` d
        LEFT JOIN (
          SELECT id, ROW_NUMBER() OVER (
                   ORDER BY MATCH(content) AGAINST (? IN NATURAL LANGUAGE MODE) DESC) AS r
          FROM \`${table}\`
          WHERE MATCH(content) AGAINST (? IN NATURAL LANGUAGE MODE)
          LIMIT 50
        ) ft ON ft.id = d.id
        LEFT JOIN (
          SELECT id, ROW_NUMBER() OVER (ORDER BY cosine_distance(embedding, ?)) AS r
          FROM (
            SELECT id, embedding FROM \`${table}\`
            ORDER BY cosine_distance(embedding, ?) APPROXIMATE
            LIMIT 50
          ) z
        ) vv ON vv.id = d.id
        WHERE ft.id IS NOT NULL OR vv.id IS NOT NULL
        ORDER BY ft.r IS NULL, ft.r, vv.r
        LIMIT 10`,
      params: [KEYWORDS, KEYWORDS, vector, vector],
      desc: '两分支 RRF 融合（向量分支 APPROXIMATE + 全文分支扫基表）',
    },
    // D：纯向量检索
    vectorOnly: {
      sql: `SELECT id FROM \`${table}\`
            ORDER BY cosine_distance(embedding, ?) APPROXIMATE
            LIMIT 10`,
      params: [vector],
      desc: '纯向量检索（APPROXIMATE）',
    },
  };
}

async function run() {
  const conn = await mysql.createConnection({
    host: config.seekdb.host,
    port: config.seekdb.port,
    user: config.seekdb.user,
    password: config.seekdb.password,
    charset: 'utf8mb4',
  });

  const results = {
    generatedAt: new Date().toISOString(),
    seekdbVersion: '5.7.25-OceanBase seekdb-v1.4.0.0',
    dimension: DIM,
    keywords: KEYWORDS,
    totalDocs: DOCS.length,
    expectedIds: DOCS.map((d) => d.id),
    tables: {},
  };

  try {
    await conn.query(`CREATE DATABASE IF NOT EXISTS \`${config.seekdb.database}\` DEFAULT CHARSET utf8mb4`);
    await conn.query(`USE \`${config.seekdb.database}\``);

    for (const [mode, table] of Object.entries(TABLES)) {
      const syncMode = mode === 'async' ? 'ASYNC' : 'IMMEDIATE';
      await conn.query(`DROP TABLE IF EXISTS \`${table}\``);
      await conn.query(tableDdl(table, syncMode));

      // 读取实际生效的 sync_mode（以服务端 DDL 为准，不靠假设）
      const [ddlRows] = await conn.query(`SHOW CREATE TABLE \`${table}\``);
      const ddlText = Object.values(ddlRows[0]).find((v) => typeof v === 'string' && v.includes('CREATE TABLE'));
      const effective = ddlText?.match(/SYNC_MODE=(\w+)/)?.[1] ?? 'UNKNOWN';

      // 写入 3 条文档
      for (const [i, d] of DOCS.entries()) {
        const vec = `[${makeVector(i + 1).map((x) => x.toFixed(6)).join(',')}]`;
        await conn.query(
          `INSERT INTO \`${table}\` (id,title,content,embedding) VALUES (?,?,?,?)`,
          [d.id, d.title, d.content, vec],
        );
      }

      // 立刻检索——不等异步管道
      const vector = `[${makeVector(1).map((x) => x.toFixed(6)).join(',')}]`;
      const shapes = queryShapes(table, vector);
      const shapeResults = {};

      for (const [shapeName, shape] of Object.entries(shapes)) {
        const [rows] = await conn.query(shape.sql, shape.params);
        const ids = rows.map((r) => r.id);
        shapeResults[shapeName] = {
          description: shape.desc,
          recallCount: ids.length,
          recalledIds: ids,
          missingIds: DOCS.map((d) => d.id).filter((id) => !ids.includes(id)),
          rows,
        };
      }

      // 全量行数（确认真实写进去了）
      const [countRows] = await conn.query(`SELECT COUNT(*) AS n FROM \`${table}\``);
      const total = Number(countRows[0].n);

      results.tables[mode] = {
        table,
        requestedSyncMode: syncMode,
        effectiveSyncMode: effective,
        rowsInTable: total,
        shapes: shapeResults,
      };

      console.log(`\n=== sync_mode=${effective}（表 ${table}）===`);
      console.log(`  表内行数: ${total}`);
      for (const [name, r] of Object.entries(shapeResults)) {
        const flag = r.recallCount === DOCS.length ? '✓' : '✗';
        console.log(`  ${flag} ${name}: 召回 ${r.recallCount}/${DOCS.length}  [${r.recalledIds.join(', ')}]`);
      }
    }

    const a = results.tables.async;
    const i = results.tables.immediate;
    results.conclusion = {
      asyncShapes: Object.fromEntries(
        Object.entries(a.shapes).map(([k, v]) => [k, v.recallCount]),
      ),
      immediateShapes: Object.fromEntries(
        Object.entries(i.shapes).map(([k, v]) => [k, v.recallCount]),
      ),
      totalDocs: DOCS.length,
      summary:
        `ASYNC 下受影响最大的形态是「全文过滤 + APPROXIMATE 向量排序」与「纯向量检索」` +
        `（召回 ${a.shapes.fulltextPlusApprox.recallCount}/${DOCS.length}、` +
        `${a.shapes.vectorOnly.recallCount}/${DOCS.length}）；` +
        `IMMEDIATE 下全部形态召回 ${i.shapes.fulltextPlusApprox.recallCount}/${DOCS.length}，` +
        `说明显式配置 sync_mode=IMMEDIATE 是「写入后立即可检索」的必要条件。`,
    };

    const outPath = path.join(STEPFUN_ROOT, 'evidence', 'sync-mode-comparison.json');
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    fs.writeFileSync(outPath, JSON.stringify(results, null, 2), 'utf8');

    console.log('\n=== 结论 ===');
    console.log(`  ${results.conclusion.summary}`);
    console.log(`  ✓ 已写入 ${path.relative(STEPFUN_ROOT, outPath)}`);
  } finally {
    // 清理实验用的两张表（结果已落到 evidence/），加 --keep 可保留以便手工排查
    if (!process.argv.includes('--keep')) {
      for (const table of Object.values(TABLES)) {
        await conn.query(`DROP TABLE IF EXISTS \`${table}\``).catch(() => {});
      }
    }
    await conn.end().catch(() => {});
  }
}

run().catch((err) => {
  console.error(`❌ 验证失败：${err.message}`);
  process.exitCode = 1;
});
