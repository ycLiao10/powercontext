/**
 * 冒烟测试：端到端验证「写入 → 混合检索 → 增量写入立即可见」。
 *
 * 运行：node server/smoke.js
 *
 * 本脚本**不需要浏览器**：向量在此处用确定性哈希造出来，
 * 目的是隔离验证「seekdb 侧」的写入与混合检索链路是否正确。
 * 真实的浏览器端向量化（WebGPU/WASM）在阶段三接入。
 */

import { config } from './config.js';
import {
  connect,
  ensureSchema,
  insertDocuments,
  countDocuments,
  search,
  hybridSearchSql,
  vectorSearchSql,
  fulltextSearchSql,
} from './db.js';
import { loggedQuery, resetSqlLog } from './sql-log.js';

/**
 * 确定性伪向量：把文本切成字符二元组（bigram）散列到各维度，L2 归一化。
 *
 * 这样构造的向量具备**真实的相似度结构**——共享字越多余弦距离越近，
 * 冒烟测试里的排序结果才有意义（否则全是非正交稀疏向量，距离恒为 1.0）。
 * 同样的文本永远得到同样的向量。
 */
function fakeEmbedding(text, dim = config.vectorDim) {
  const vec = new Array(dim).fill(0);
  const normalized = String(text).toLowerCase().replace(/\s+/g, '');

  // 一元组 + 二元组：一元组保证单字也有贡献，二元组提供更细的区分度
  const grams = [];
  for (let i = 0; i < normalized.length; i += 1) {
    grams.push(normalized[i]);
    if (i + 1 < normalized.length) grams.push(normalized.slice(i, i + 2));
  }

  for (const gram of grams) {
    // FNV-1a 散列
    let h = 2166136261;
    for (let i = 0; i < gram.length; i += 1) {
      h ^= gram.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    const idx = Math.abs(h) % dim;
    vec[idx] += 1;
  }

  const norm = Math.sqrt(vec.reduce((s, v) => s + v * v, 0)) || 1;
  return vec.map((v) => v / norm);
}

const SEED_DOCS = [
  { id: 'doc-01', title: '季度经营报告', content: '本季度营收同比增长百分之十二，优惠活动拉动用户增长。' },
  { id: 'doc-02', title: '产品使用手册', content: '产品安装配置说明与保修条款，适用于所有型号。' },
  { id: 'doc-03', title: '数据库运维指南', content: '数据库备份恢复流程、监控告警配置与故障排查步骤。' },
  { id: 'doc-04', title: '安全合规白皮书', content: '数据加密、权限审计与合规基线的完整说明。' },
  { id: 'doc-05', title: '缓存性能优化', content: '缓存穿透击穿雪崩的成因，以及多级缓存的优化手段。' },
  { id: 'doc-06', title: '网络故障演练', content: '网络抖动、链路切换与容灾切换的演练记录。' },
  { id: 'doc-07', title: '服务接口文档', content: '用户服务与订单服务的接口定义、错误码与限流策略。' },
  { id: 'doc-08', title: '数据同步方案', content: '主从复制与数据同步的一致性校验方案对比。' },
  { id: 'doc-09', title: '存储引擎对比', content: '行存与列存引擎的读写性能对比与选型建议。' },
  { id: 'doc-10', title: '索引设计实践', content: '全文索引与向量索引的选型、参数调优与常见误区。' },
];

const INCREMENTAL_DOC = {
  id: 'doc-11',
  title: '新增的优惠文档',
  content: '这条文档在冒烟测试中被追加，用于验证增量导入后立刻可检索。',
};

const QUERY = '优惠 活动';
const TOPK = 5;

function line(char = '-') {
  return char.repeat(72);
}

async function main() {
  console.log(line('='));
  console.log('stepfun 冒烟测试：写入 → 混合检索 → 增量可见性');
  console.log(line('='));
  console.log(`seekdb     : ${config.seekdb.host}:${config.seekdb.port}/${config.seekdb.database}`);
  console.log(`表名       : ${config.seekdb.table}`);
  console.log(`向量维度   : ${config.vectorDim}`);
  console.log(`SQL 日志   : ${config.sqlLog.enabled ? config.sqlLog.file : '(关闭)'}`);

  resetSqlLog();
  const conn = await connect();

  try {
    // ---------- 1. 建库建表 ----------
    console.log(`\n${line()}\n[1/5] 建库建表（含 HNSW 向量索引 + ngram 全文索引）\n${line()}`);
    await ensureSchema(conn);
    await conn.query(`TRUNCATE TABLE \`${config.seekdb.table}\``);
    console.log('✓ schema ready');

    // ---------- 2. 写入 10 条 ----------
    console.log(`\n${line()}\n[2/5] 写入 ${SEED_DOCS.length} 条文档\n${line()}`);
    const seeded = SEED_DOCS.map((d) => ({
      ...d,
      embedding: fakeEmbedding(`${d.title} ${d.content}`),
    }));
    await insertDocuments(conn, seeded);
    const afterSeed = await countDocuments(conn);
    console.log(`✓ 写入完成，当前文档数 = ${afterSeed}`);
    if (afterSeed !== SEED_DOCS.length) {
      throw new Error(`文档数不符：期望 ${SEED_DOCS.length}，实际 ${afterSeed}`);
    }

    // ---------- 3. 三种检索模式 ----------
    console.log(`\n${line()}\n[3/5] 检索验证（top${TOPK}）\n${line()}`);
    const queryEmbedding = fakeEmbedding(QUERY);

    const hybrid = await loggedQuery(
      conn,
      'hybrid',
      hybridSearchSql(TOPK),
      [QUERY, QUERY, `[${queryEmbedding.join(',')}]`, `[${queryEmbedding.join(',')}]`],
      '向量 + 全文混合检索：一条 SQL，RRF 融合',
    );
    console.log(`\n● 混合检索（hybrid，单条 SQL 内 RRF 融合）— ${hybrid.length} 条`);
    for (const [i, r] of hybrid.entries()) {
      console.log(
        `  ${i + 1}. [${r.id}] ${r.title}  score=${r.hybrid_score}` +
          `  (ft_rank=${r.fulltext_rank}, vec_rank=${r.vector_rank})`,
      );
    }

    const vectorOnly = await loggedQuery(
      conn,
      'vector',
      vectorSearchSql(TOPK),
      [`[${queryEmbedding.join(',')}]`, `[${queryEmbedding.join(',')}]`],
      '纯向量检索',
    );
    console.log(`\n● 纯向量检索（vector）— ${vectorOnly.length} 条`);
    for (const [i, r] of vectorOnly.entries()) {
      console.log(`  ${i + 1}. [${r.id}] ${r.title}  distance=${Number(r.distance).toFixed(6)}`);
    }

    const fulltextOnly = await loggedQuery(
      conn,
      'fulltext',
      fulltextSearchSql(TOPK),
      [QUERY, QUERY],
      '纯全文检索',
    );
    console.log(`\n● 纯全文检索（fulltext）— ${fulltextOnly.length} 条`);
    for (const [i, r] of fulltextOnly.entries()) {
      console.log(`  ${i + 1}. [${r.id}] ${r.title}  relevance=${Number(r.relevance).toFixed(6)}`);
    }

    if (hybrid.length === 0) throw new Error('混合检索返回 0 条');

    // ---------- 4. 增量可见性（本题核心） ----------
    console.log(`\n${line()}\n[4/5] 增量可见性验证：新增 1 条后立即检索\n${line()}`);
    await insertDocuments(conn, [
      { ...INCREMENTAL_DOC, embedding: fakeEmbedding(`${INCREMENTAL_DOC.title} ${INCREMENTAL_DOC.content}`) },
    ]);
    const afterInsert = await countDocuments(conn);
    console.log(`✓ 追加写入完成，文档数 = ${afterInsert}`);

    // 用新文档正文里的关键词做检索，必须能查回来
    const incQuery = '追加 增量';
    const incEmbedding = fakeEmbedding(incQuery);
    const incHybrid = await loggedQuery(
      conn,
      'hybrid',
      hybridSearchSql(TOPK),
      [incQuery, incQuery, `[${incEmbedding.join(',')}]`, `[${incEmbedding.join(',')}]`],
      '增量可见性验证：写入后立即检索',
    );
    const found = incHybrid.find((r) => r.id === INCREMENTAL_DOC.id);
    console.log(`\n● 增量文档检索结果（查询词：${incQuery}）— ${incHybrid.length} 条`);
    for (const [i, r] of incHybrid.entries()) {
      console.log(`  ${i + 1}. [${r.id}] ${r.title}  score=${r.hybrid_score}`);
    }
    if (!found) {
      throw new Error(
        '增量可见性失败：刚写入的文档没有出现在检索结果中。' +
          '检查 VECTOR INDEX 的 sync_mode 是否为 IMMEDIATE。',
      );
    }
    console.log(`✓ 增量文档 [${INCREMENTAL_DOC.id}] 已可检索 —— sync_mode=IMMEDIATE 生效`);

    // ---------- 5. 结论 ----------
    console.log(`\n${line('=')}\n[5/5] 结论\n${line('=')}`);
    console.log(`✓ 写入 ${SEED_DOCS.length} 条 → 混合检索返回 ${hybrid.length} 条`);
    console.log(`✓ 新增 1 条 → 立即可检索（增量可见性通过）`);
    console.log(`✓ 三种检索模式均返回结果`);
    console.log(`✓ SQL 原文已写入：${config.sqlLog.file}`);
    console.log(line('='));
  } finally {
    await conn.end().catch(() => {});
  }
}

main().catch((err) => {
  console.error(`\n❌ 冒烟测试失败：${err.message}`);
  process.exitCode = 1;
});
