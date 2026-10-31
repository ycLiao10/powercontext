/**
 * 执行混合检索 SQL 并打印结果。
 *
 * 用法：
 *   node scripts/search.js "缓存雪崩怎么解决"          # 混合检索
 *   node scripts/search.js "缓存雪崩" --mode=fulltext    # 纯全文
 *   node scripts/search.js "缓存雪崩" --mode=vector       # 纯向量
 *   node scripts/search.js --eval                         # 跑固定评测集（Recall@5 / nDCG@5）
 *
 * 检索始终由 seekdb 执行，这里只负责准备查询向量并展示返回的原始行。
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { connect, search } from '../server/db.js';
import { embedTexts, modelInfo } from './offline-eval-embedder.js';
import { loggedQuery } from '../server/sql-log.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STEPFUN_ROOT = path.resolve(__dirname, '..');

const args = process.argv.slice(2);
const evalMode = args.includes('--eval');
const modeArg = args.find((a) => a.startsWith('--mode='));
const mode = modeArg ? modeArg.split('=')[1] : 'hybrid';
const query = args.filter((a) => !a.startsWith('--')).join(' ').trim();

/* ---------------- 评测指标 ---------------- */

function recallAtK(ranked, relevant, k) {
  const top = ranked.slice(0, k);
  const hit = relevant.filter((id) => top.includes(id)).length;
  return relevant.length ? hit / relevant.length : 0;
}

function ndcgAtK(ranked, graded, k) {
  const top = ranked.slice(0, k);
  let dcg = 0;
  top.forEach((id, i) => {
    const gain = graded[id] ?? 0;
    if (gain > 0) dcg += (2 ** gain - 1) / Math.log2(i + 2);
  });
  const ideal = Object.values(graded)
    .sort((a, b) => b - a)
    .slice(0, k);
  let idcg = 0;
  ideal.forEach((gain, i) => {
    if (gain > 0) idcg += (2 ** gain - 1) / Math.log2(i + 2);
  });
  return idcg > 0 ? dcg / idcg : 0;
}

async function runEval(conn) {
  const spec = JSON.parse(fs.readFileSync(path.join(STEPFUN_ROOT, 'data', 'queries.json'), 'utf8'));
  const embeddings = await embedTexts(spec.queries.map((q) => q.query));

  let recallSum = 0;
  let ndcgSum = 0;
  const details = [];

  for (let i = 0; i < spec.queries.length; i += 1) {
    const q = spec.queries[i];
    const rows = await search(conn, {
      mode,
      query: q.query,
      embedding: embeddings[i],
      topK: 5,
    });
    const ranked = rows.map((r) => r.id);
    const recall = recallAtK(ranked, q.relevant, 5);
    const ndcg = ndcgAtK(ranked, q.graded, 5);
    recallSum += recall;
    ndcgSum += ndcg;
    details.push({
      id: q.id,
      query: q.query,
      lang: q.lang,
      ranked,
      relevant: q.relevant,
      recallAt5: Number(recall.toFixed(4)),
      ndcgAt5: Number(ndcg.toFixed(4)),
    });
    console.log(
      `${q.id} [${q.lang}] ${q.query}\n` +
        `   top5=${ranked.join(', ')}\n` +
        `   relevant=${q.relevant.join(', ')}  Recall@5=${recall.toFixed(4)}  nDCG@5=${ndcg.toFixed(4)}`,
    );
  }

  const n = spec.queries.length;
  const summary = {
    mode,
    model: modelInfo(),
    queries: n,
    recallAt5: Number((recallSum / n).toFixed(4)),
    ndcgAt5: Number((ndcgSum / n).toFixed(4)),
    details,
  };
  const outPath = path.join(STEPFUN_ROOT, 'evidence', `quality-${mode}.json`);
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(summary, null, 2), 'utf8');
  console.log(`\nRecall@5 = ${summary.recallAt5}   nDCG@5 = ${summary.ndcgAt5}   (${n} 条查询)`);
  console.log(`✓ 结果已写入 ${path.relative(STEPFUN_ROOT, outPath)}`);
  return summary;
}

async function main() {
  const conn = await connect();
  try {
    if (evalMode) {
      await runEval(conn);
      return;
    }
    if (!query) {
      console.error('用法：node scripts/search.js "<查询词>" [--mode=hybrid|vector|fulltext]');
      console.error('   或：node scripts/search.js --eval');
      process.exitCode = 2;
      return;
    }
    let embedding;
    if (mode !== 'fulltext') {
      embedding = (await embedTexts([query]))[0];
    }
    const rows = await search(conn, { mode, query, embedding, topK: 5 });
    console.log(`查询：${query}   模式：${mode}   命中 ${rows.length} 条\n`);
    rows.forEach((r, i) => {
      const score = r.hybrid_score ?? r.distance ?? r.relevance;
      console.log(`${i + 1}. [${r.id}] ${r.title}`);
      console.log(`   score=${score}`);
      if (r.fulltext_rank !== undefined) {
        console.log(`   fulltext_rank=${r.fulltext_rank}  vector_rank=${r.vector_rank}`);
      }
      console.log(`   ${String(r.content).slice(0, 60)}...`);
    });
  } finally {
    await conn.end().catch(() => {});
  }
}

main().catch((err) => {
  console.error(`❌ 失败：${err.message}`);
  process.exitCode = 1;
});
