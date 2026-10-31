/**
 * 检索质量评测：对比量化前（FP32 基线）与量化后（INT8/FP16）在同一评测集上的
 * Recall@K / nDCG@K / MRR@10 变化。
 *
 * 评测方法：
 *   Recall@K = |{前 K 条命中中属于人工标注相关文档}| / |该查询标注的相关文档总数|
 *   DCG@K    = Σ_{i=1..K} (2^rel_i − 1) / log2(i+1)，rel_i 取自 qrels（未标注记 0）
 *   nDCG@K   = DCG@K / IDCG@K（IDCG 由该查询相关文档按标注等级降序的理想排序计算）
 *   MRR@K    = 1 / (第一篇相关文档在结果中的位置)，未命中记 0
 *
 * 用法：
 *   node scripts/eval-quality.mjs [--model Xenova/bge-small-zh-v1.5] [--dtypes fp32,q8]
 *                                 [--device auto] [--topk 10]
 *                                 [--queries data/eval/queries.json] [--qrels data/eval/qrels.json]
 *                                 [--out docs/eval-results.json] [--md docs/EVALUATION.md]
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};
const argList = (name, fallback) =>
  arg(name, fallback)
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

const APP_URL = arg("app-url", "http://127.0.0.1:8787");
const MODEL = arg("model", "Xenova/bge-small-zh-v1.5");
const DTYPES = argList("dtypes", "fp32,q8");
const DEVICE = arg("device", "auto");
const TOPK = Number(arg("topk", "10"));
const QUERIES_FILE = path.resolve(root, arg("queries", "data/eval/queries.json"));
const QRELS_FILE = path.resolve(root, arg("qrels", "data/eval/qrels.json"));
const CORPUS_DIR = path.resolve(root, arg("corpus", "data/corpus"));
const OUT_JSON = path.resolve(root, arg("out", "docs/eval-results.json"));
const OUT_MD = path.resolve(root, arg("md", "docs/EVALUATION.md"));
const EDGE =
  arg("edge", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe") ||
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const sanitize = (s) => s.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

function readCorpus() {
  if (!fs.existsSync(CORPUS_DIR)) throw new Error(`语料目录不存在：${CORPUS_DIR}`);
  return fs
    .readdirSync(CORPUS_DIR)
    .filter((f) => f.endsWith(".md"))
    .sort()
    .map((f) => {
      const text = fs.readFileSync(path.join(CORPUS_DIR, f), "utf8").trim();
      return {
        id: path.basename(f, ".md"),
        text,
        metadata: { title: text.split("\n")[0].replace(/^#+\s*/, "") },
      };
    });
}

function loadEvalSet() {
  if (!fs.existsSync(QUERIES_FILE) || !fs.existsSync(QRELS_FILE)) {
    throw new Error(`评测集缺失：${QUERIES_FILE} / ${QRELS_FILE}`);
  }
  const queries = JSON.parse(fs.readFileSync(QUERIES_FILE, "utf8")).queries ?? [];
  const qrels = JSON.parse(fs.readFileSync(QRELS_FILE, "utf8"));
  if (!queries.length) throw new Error("评测集为空");
  return { queries, qrels };
}

/* ---------------- 指标计算 ---------------- */
function recallAtK(ranked, relevant, k) {
  if (!relevant.size) return null;
  let hit = 0;
  for (let i = 0; i < Math.min(k, ranked.length); i++) if (relevant.has(ranked[i])) hit++;
  return hit / relevant.size;
}

function dcgAtK(ranked, relMap, k) {
  let dcg = 0;
  for (let i = 0; i < Math.min(k, ranked.length); i++) {
    const rel = relMap[ranked[i]] ?? 0;
    if (rel > 0) dcg += (Math.pow(2, rel) - 1) / Math.log2(i + 2);
  }
  return dcg;
}

function idcgAtK(relMap, k) {
  const rels = Object.values(relMap)
    .filter((r) => r > 0)
    .sort((a, b) => b - a)
    .slice(0, k);
  let idcg = 0;
  rels.forEach((rel, i) => {
    idcg += (Math.pow(2, rel) - 1) / Math.log2(i + 2);
  });
  return idcg;
}

function ndcgAtK(ranked, relMap, k) {
  const idcg = idcgAtK(relMap, k);
  if (!idcg) return null;
  return dcgAtK(ranked, relMap, k) / idcg;
}

function mrrAtK(ranked, relevant, k) {
  for (let i = 0; i < Math.min(k, ranked.length); i++) {
    if (relevant.has(ranked[i])) return 1 / (i + 1);
  }
  return 0;
}

const mean = (arr) => {
  const v = arr.filter((x) => x !== null && x !== undefined);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
};
const fmt = (x) => (x === null || x === undefined ? "-" : Number(x).toFixed(4));

async function ensureServer() {
  try {
    const res = await fetch(`${APP_URL}/api/health`);
    if (res.ok) return null;
  } catch {
    /* not running */
  }
  if (!fs.existsSync(path.join(root, "web", "dist", "index.html"))) {
    throw new Error("web/dist 不存在，请先执行 npm run build");
  }
  const child = spawn("node", ["server/index.mjs"], { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
  child.stdout.on("data", (d) => process.stdout.write(`[api] ${d}`));
  child.stderr.on("data", (d) => process.stderr.write(`[api] ${d}`));
  for (let i = 0; i < 60; i++) {
    await sleep(500);
    try {
      const res = await fetch(`${APP_URL}/api/health`);
      if (res.ok) return child;
    } catch {
      /* wait */
    }
  }
  throw new Error("本地服务启动超时");
}

async function run() {
  const corpus = readCorpus();
  const { queries, qrels } = loadEvalSet();
  console.log(`[eval] 语料 ${corpus.length} 篇，查询 ${queries.length} 条，K=${TOPK}`);

  const server = await ensureServer();
  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: true,
    args: ["--enable-unsafe-webgpu", "--enable-features=Vulkan", "--no-sandbox"],
  });
  const page = await browser.newPage();
  page.on("pageerror", (e) => console.error(`[pageerror] ${e.message}`));
  await page.goto(APP_URL, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForFunction("window.__stepfun && window.__stepfun.ready", { timeout: 60000 });

  const configs = [];
  for (const dtype of DTYPES) {
    const collection = `eval_${sanitize(MODEL.split("/").pop())}_${dtype}`;
    console.log(`\n[eval] ===== ${MODEL} / ${DEVICE} / ${dtype} =====`);
    const loaded = await page.evaluate(
      async (m, d, t) => {
        await window.__stepfun.setModel(m);
        return await window.__stepfun.loadModel({ modelId: m, dtype: t, device: d });
      },
      MODEL,
      DEVICE,
      dtype
    );
    const imported = await page.evaluate(
      async (docs, coll) => await window.__stepfun.importDocuments(docs, { batchSize: 8, collection: coll }),
      corpus,
      collection
    );
    console.log(`[eval]   实际后端=${loaded.backend} 实际精度=${loaded.dtype}，导入 ${imported.count} 篇`);

    const perQuery = [];
    for (const q of queries) {
      const r = await page.evaluate(
        async (text, k) => await window.__stepfun.search(text, k),
        q.text,
        TOPK
      );
      const ranked = r.results.map((x) => x.id);
      const relMap = qrels[q.id] ?? {};
      const relevant = new Set(Object.keys(relMap));
      const recall1 = recallAtK(ranked, relevant, 1);
      const recall5 = recallAtK(ranked, relevant, 5);
      const recall10 = recallAtK(ranked, relevant, 10);
      const ndcg10 = ndcgAtK(ranked, relMap, 10);
      const mrr = mrrAtK(ranked, relevant, 10);
      perQuery.push({
        id: q.id,
        text: q.text,
        ranked,
        recallAt1: recall1,
        recallAt5: recall5,
        recallAt10: recall10,
        ndcgAt10: ndcg10,
        mrrAt10: mrr,
        queryEmbedMs: r.queryEmbedMs,
        dbMs: r.dbMs,
      });
    }
    const summary = {
      model: MODEL,
      requested: { device: DEVICE, dtype },
      actual: { backend: loaded.backend, dtype: loaded.dtype },
      collection,
      documentCount: imported.count,
      queries: perQuery.length,
      metrics: {
        recallAt1: mean(perQuery.map((x) => x.recallAt1)),
        recallAt5: mean(perQuery.map((x) => x.recallAt5)),
        recallAt10: mean(perQuery.map((x) => x.recallAt10)),
        ndcgAt10: mean(perQuery.map((x) => x.ndcgAt10)),
        mrrAt10: mean(perQuery.map((x) => x.mrrAt10)),
      },
      latency: {
        queryEmbedMsMedian: mean(perQuery.map((x) => x.queryEmbedMs)),
        dbMsMedian: mean(perQuery.map((x) => x.dbMs)),
      },
    };
    configs.push({ summary, perQuery });
    console.log(
      `[eval]   Recall@1=${fmt(summary.metrics.recallAt1)} Recall@5=${fmt(
        summary.metrics.recallAt5
      )} Recall@10=${fmt(summary.metrics.recallAt10)} nDCG@10=${fmt(summary.metrics.ndcgAt10)} MRR@10=${fmt(
        summary.metrics.mrrAt10
      )}`
    );
  }

  // 基线与量化配置的对比
  const baseline = configs[0];
  const comparisons = configs.slice(1).map((c) => {
    const delta = {};
    for (const k of Object.keys(c.summary.metrics)) {
      const b = baseline.summary.metrics[k];
      const v = c.summary.metrics[k];
      delta[k] = {
        baseline: b,
        quantized: v,
        abs: v !== null && b !== null ? v - b : null,
        rel: v !== null && b ? (v - b) / b : null,
      };
    }
    let changed = 0;
    for (let i = 0; i < baseline.perQuery.length; i++) {
      const a = baseline.perQuery[i].ranked.join("|");
      const b = c.perQuery[i].ranked.join("|");
      if (a !== b) changed++;
    }
    return { comparedWith: baseline.summary, target: c.summary, delta, rankingChangedQueries: changed };
  });

  const payload = {
    environment: {
      browser: await browser.version(),
      platform: `${os.platform()} ${os.release()} ${os.arch()}`,
      node: process.version,
      measuredAt: new Date().toISOString(),
      corpusSize: corpus.length,
      queryCount: queries.length,
      topK: TOPK,
      method:
        "同语料库内检索评测：每种配置独立导入同一批语料到独立集合，再用全部查询在 seekdb 内做向量+全文混合检索，按人工标注相关性计算 Recall@K / nDCG@K / MRR@K",
    },
    configs: configs.map((c) => c.summary),
    comparisons,
    details: configs.map((c) => ({ summary: c.summary, perQuery: c.perQuery })),
  };
  fs.mkdirSync(path.dirname(OUT_JSON), { recursive: true });
  fs.writeFileSync(OUT_JSON, JSON.stringify(payload, null, 2), "utf8");
  fs.writeFileSync(OUT_MD, renderMarkdown(payload), "utf8");
  console.log(`\n[eval] 结果已写入 ${OUT_JSON} 与 ${OUT_MD}`);

  await browser.close();
  if (server) server.kill();
  process.exit(0);
}

function renderMarkdown(payload) {
  const { environment, configs, comparisons } = payload;
  const rows = configs.map(
    (c) =>
      `| ${c.requested.dtype} → **${c.actual.dtype}** | ${c.actual.backend} | ${c.documentCount} | ${fmt(
        c.metrics.recallAt1
      )} | ${fmt(c.metrics.recallAt5)} | ${fmt(c.metrics.recallAt10)} | ${fmt(c.metrics.ndcgAt10)} | ${fmt(
        c.metrics.mrrAt10
      )} |`
  );
  const cmpRows = comparisons.length
    ? comparisons
        .map((c) => {
          const d = c.delta;
          return `| ${c.target.requested.dtype} → ${c.target.actual.dtype} | ${fmt(d.recallAt10.baseline)} → ${fmt(
            d.recallAt10.quantized
          )} | ${d.recallAt10.abs >= 0 ? "+" : ""}${fmt(d.recallAt10.abs)} | ${fmt(d.ndcgAt10.baseline)} → ${fmt(
            d.ndcgAt10.quantized
          )} | ${d.ndcgAt10.abs >= 0 ? "+" : ""}${fmt(d.ndcgAt10.abs)} | ${c.rankingChangedQueries}/${c.target.queries} |`;
        })
        .join("\n")
    : "（仅一种配置）";

  return `# 检索质量评测结果

> 本文件由 \`npm run eval\` 自动生成，所有数字来自提交应用的真实运行。

## 评测方法

- **数据集**：${environment.corpusSize} 篇中文技术文档构成语料库；${environment.queryCount} 条人工标注查询，
  每条查询标注 2-4 篇相关文档（相关性等级 2 = 高度相关，1 = 相关）。
- **检索方式**：${environment.method}。
- **指标**：
  - \`Recall@K = |Top-K 命中的相关文档数| / |标注的相关文档总数|\`
  - \`DCG@K = Σ (2^rel_i − 1) / log2(i+1)\`；\`nDCG@K = DCG@K / IDCG@K\`（IDCG 取相关文档按标注等级降序的理想排序）
  - \`MRR@10 = 1 / 第一篇相关文档的排名位置\`（未命中记 0）
- **对比口径**：同一台设备、同一浏览器、同一语料、同一查询集，仅改变 embedding 精度；
  每种配置导入到独立集合，避免互相污染。

| 项 | 值 |
| --- | --- |
| 浏览器 | ${environment.browser} |
| 平台 | ${environment.platform} |
| Node.js | ${environment.node} |
| 语料规模 | ${environment.corpusSize} 篇 |
| 查询数 | ${environment.queryCount} |
| TopK | ${environment.topK} |
| 采集时间 | ${environment.measuredAt} |

## 各配置指标

| 精度 | 推理后端 | 文档数 | Recall@1 | Recall@5 | Recall@10 | nDCG@10 | MRR@10 |
| --- | --- | --- | --- | --- | --- | --- | --- |
${rows.join("\n")}

## 量化前后对比

| 对比 | Recall@10 | Δ Recall@10 | nDCG@10 | Δ nDCG@10 | 排序发生变化的查询数 |
| --- | --- | --- | --- | --- | --- |
${cmpRows}
`;
}

run().catch((err) => {
  console.error("[eval] 失败:", err);
  process.exit(1);
});
