/**
 * 性能基准：用真实浏览器（Edge）驱动本应用，采集各配置下的实测指标。
 *
 * 覆盖题目要求分阶段计时的全部环节：
 *   模型下载与加载 / 文档向量化 / 写入与索引刷新 / 查询向量化 / seekdb 检索 / 端到端查询
 * 首次（冷启动）与预热后（热路径）分开报告，重复采样取中位数（另给 p90 / min）。
 *
 * 用法：
 *   node scripts/bench.mjs [--model Xenova/bge-small-zh-v1.5] [--devices webgpu,wasm]
 *                          [--dtypes fp32,fp16,q8] [--iterations 20] [--batch-size 8]
 *                          [--corpus data/corpus] [--out docs/bench-results.json]
 *                          [--app-url http://127.0.0.1:8787] [--edge <path>]
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
const MODELS = argList("model", "Xenova/bge-small-zh-v1.5");
const DEVICES = argList("devices", "webgpu,wasm");
const DTYPES = argList("dtypes", "fp32,fp16,q8");
const ITERATIONS = Number(arg("iterations", "20"));
const BATCH_SIZE = Number(arg("batch-size", "8"));
const CORPUS_DIR = path.resolve(root, arg("corpus", "data/corpus"));
const OUT_JSON = path.resolve(root, arg("out", "docs/bench-results.json"));
const OUT_MD = path.resolve(root, arg("md", "docs/BENCHMARK.md"));
const EDGE =
  arg("edge", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe") ||
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const median = (a) => {
  if (!a.length) return null;
  const s = [...a].sort((x, y) => x - y);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const p90 = (a) => {
  if (!a.length) return null;
  const s = [...a].sort((x, y) => x - y);
  return s[Math.min(s.length - 1, Math.floor(s.length * 0.9))];
};
const r1 = (x) => (x === null || x === undefined ? null : +Number(x).toFixed(2));

function readCorpus() {
  if (!fs.existsSync(CORPUS_DIR)) {
    throw new Error(`语料目录不存在：${CORPUS_DIR}`);
  }
  const files = fs
    .readdirSync(CORPUS_DIR)
    .filter((f) => f.endsWith(".md"))
    .sort();
  if (!files.length) throw new Error(`语料目录为空：${CORPUS_DIR}`);
  return files.map((f) => {
    const text = fs.readFileSync(path.join(CORPUS_DIR, f), "utf8").trim();
    const id = path.basename(f, ".md");
    return { id, text, metadata: { title: text.split("\n")[0].replace(/^#+\s*/, "") } };
  });
}

function readQueries() {
  const p = path.join(root, "data", "eval", "queries.json");
  if (!fs.existsSync(p)) return [];
  try {
    return JSON.parse(fs.readFileSync(p, "utf8")).queries ?? [];
  } catch {
    return [];
  }
}

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
  console.log("[bench] 启动本地服务…");
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

const sanitize = (s) => s.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

async function run() {
  const corpus = readCorpus();
  const queries = readQueries();
  const queryTexts = queries.length
    ? queries.map((q) => q.text)
    : ["向量检索与全文检索的混合排序", "HNSW 索引参数怎么调", "INT8 量化对召回的影响"];
  console.log(
    `[bench] 语料 ${corpus.length} 篇，查询集 ${queries.length || queryTexts.length} 条，迭代 ${ITERATIONS} 次`
  );

  const server = await ensureServer();
  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: true,
    args: [
      "--enable-unsafe-webgpu",
      "--enable-features=Vulkan,UseSkiaRenderer",
      "--no-sandbox",
      "--disable-dev-shm-usage",
    ],
  });
  const page = await browser.newPage();
  page.on("pageerror", (e) => console.error(`[pageerror] ${e.message}`));
  await page.goto(APP_URL, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForFunction("window.__stepfun && window.__stepfun.ready", { timeout: 60000 });

  const browserVersion = await browser.version();
  const results = [];

  for (const modelId of MODELS) {
    for (const device of DEVICES) {
      for (const dtype of DTYPES) {
        const label = `${modelId} / ${device} / ${dtype}`;
        console.log(`\n[bench] ===== ${label} =====`);
        const collection = `bench_${sanitize(modelId.split("/").pop())}_${device}_${dtype}`;

        const loaded = await page.evaluate(
          async (m, d, t) => {
            await window.__stepfun.setModel(m);
            return await window.__stepfun.loadModel({ modelId: m, dtype: t, device: d });
          },
          modelId,
          device,
          dtype
        );
        console.log(
          `[bench]   实际后端=${loaded.backend} 实际精度=${loaded.dtype} 加载=${loaded.loadMs.toFixed(1)}ms` +
            (loaded.backend !== device ? ` (请求 ${device}，已自动降级)` : "")
        );

        const imported = await page.evaluate(
          async (docs, coll, bs) => await window.__stepfun.importDocuments(docs, { batchSize: bs, collection: coll }),
          corpus,
          collection,
          BATCH_SIZE
        );
        console.log(
          `[bench]   导入 ${imported.count} 篇：向量化 ${imported.embedMs.toFixed(1)}ms，写入 ${imported.writeMs.toFixed(
            1
          )}ms，索引刷新 ${imported.refreshMs.toFixed(1)}ms`
        );

        // 预热一次，随后重复采样
        const env = await page.evaluate(() => window.__stepfun.state());
        await page.evaluate(async (q) => await window.__stepfun.search(q, 10), queryTexts[0]);
        const samples = [];
        for (let i = 0; i < ITERATIONS; i++) {
          const q = queryTexts[i % queryTexts.length];
          const r = await page.evaluate(
            async (qq) => await window.__stepfun.search(qq, 10),
            q
          );
          samples.push(r);
        }
        const queryEmbed = samples.map((s) => s.queryEmbedMs);
        const db = samples.map((s) => s.dbMs);
        const e2e = samples.map((s) => s.endToEndMs);

        const record = {
          model: modelId,
          requested: { device, dtype },
          actual: { backend: loaded.backend, dtype: loaded.dtype },
          collection,
          documentCount: imported.count,
          batchSize: BATCH_SIZE,
          cold: {
            modelLoadMs: r1(loaded.loadMs),
            docEmbedMs: r1(imported.embedMs),
            docEmbedThroughput: r1(imported.count / (imported.embedMs / 1000)),
            writeMs: r1(imported.writeMs),
            indexRefreshMs: r1(imported.refreshMs),
          },
          warm: {
            queryEmbedMs: { median: r1(median(queryEmbed)), p90: r1(p90(queryEmbed)), min: r1(Math.min(...queryEmbed)) },
            seekdbSearchMs: { median: r1(median(db)), p90: r1(p90(db)), min: r1(Math.min(...db)) },
            endToEndMs: { median: r1(median(e2e)), p90: r1(p90(e2e)), min: r1(Math.min(...e2e)) },
          },
          gpu: loaded.gpu,
          crossOriginIsolated: env.crossOriginIsolated,
          hardwareConcurrency: env.hardwareConcurrency,
          iterations: ITERATIONS,
        };
        results.push(record);
        console.log(
          `[bench]   查询向量化中位数 ${record.warm.queryEmbedMs.median}ms | seekdb 检索 ${record.warm.seekdbSearchMs.median}ms | 端到端 ${record.warm.endToEndMs.median}ms`
        );
      }
    }
  }

  const environment = {
    browser: browserVersion,
    platform: `${os.platform()} ${os.release()} ${os.arch()}`,
    cpu: os.cpus()[0]?.model ?? "unknown",
    cpuCount: os.cpus().length,
    node: process.version,
    appUrl: APP_URL,
    corpusSize: corpus.length,
    querySetSize: queries.length,
    iterations: ITERATIONS,
    batchSize: BATCH_SIZE,
    measuredAt: new Date().toISOString(),
  };

  const payload = { environment, results };
  fs.mkdirSync(path.dirname(OUT_JSON), { recursive: true });
  fs.writeFileSync(OUT_JSON, JSON.stringify(payload, null, 2), "utf8");
  fs.writeFileSync(OUT_MD, renderMarkdown(payload), "utf8");
  console.log(`\n[bench] 结果已写入 ${OUT_JSON} 与 ${OUT_MD}`);

  await browser.close();
  if (server) server.kill();
  process.exit(0);
}

function renderMarkdown({ environment, results }) {
  const rows = results.map((r) => {
    const c = r.cold;
    const w = r.warm;
    return `| ${r.model} | ${r.requested.device} → **${r.actual.backend}** | ${r.requested.dtype} → **${r.actual.dtype}** | ${c.modelLoadMs} ms | ${c.docEmbedMs} ms (${c.docEmbedThroughput} 篇/秒) | ${c.writeMs} ms | ${c.indexRefreshMs} ms | ${w.queryEmbedMs.median} ms | ${w.seekdbSearchMs.median} ms | ${w.endToEndMs.median} ms |`;
  });
  const head = `| 模型 | 推理后端 | 精度 | 模型加载(冷) | 文档向量化 | 写入 seekdb | 索引刷新 | 查询向量化(热,中位) | seekdb 检索(热,中位) | 端到端(热,中位) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |`;

  const detail = results
    .map(
      (r) => `### ${r.model} · 请求 ${r.requested.device}/${r.requested.dtype} → 实际 ${r.actual.backend}/${r.actual.dtype}

- 集合：\`${r.collection}\`（${r.documentCount} 篇），批大小 ${r.batchSize}，采样 ${r.iterations} 次
- 冷启动：模型加载 ${r.cold.modelLoadMs} ms；文档向量化 ${r.cold.docEmbedMs} ms（${r.cold.docEmbedThroughput} 篇/秒）；写入 ${r.cold.writeMs} ms；索引刷新 ${r.cold.indexRefreshMs} ms
- 热路径中位数：查询向量化 ${r.warm.queryEmbedMs.median} ms（p90 ${r.warm.queryEmbedMs.p90} / min ${r.warm.queryEmbedMs.min}）；seekdb 检索 ${r.warm.seekdbSearchMs.median} ms（p90 ${r.warm.seekdbSearchMs.p90} / min ${r.warm.seekdbSearchMs.min}）；端到端 ${r.warm.endToEndMs.median} ms（p90 ${r.warm.endToEndMs.p90} / min ${r.warm.endToEndMs.min}）
- WebGPU：${r.gpu?.available ? "可用" : "不可用"}${r.gpu?.adapter ? `（${r.gpu.adapter.vendor} ${r.gpu.adapter.architecture}）` : ""}${r.gpu?.reason ? `（${r.gpu.reason}）` : ""}`
    )
    .join("\n\n");

  return `# 性能实测结果

> 本文件由 \`npm run bench\` 自动生成，所有数字来自提交应用的真实运行。

## 测试条件

| 项 | 值 |
| --- | --- |
| 浏览器 | ${environment.browser} |
| 平台 | ${environment.platform} |
| CPU | ${environment.cpu}（${environment.cpuCount} 逻辑核） |
| Node.js | ${environment.node} |
| 语料规模 | ${environment.corpusSize} 篇中文文档 |
| 查询集 | ${environment.querySetSize} 条 |
| 重复次数 | 每种配置 ${environment.iterations} 次，报告中位数（另附 p90 / min） |
| 批大小 | ${environment.batchSize} |
| 采集时间 | ${environment.measuredAt} |

说明：模型加载耗时属于**冷启动**（含权重下载/解析与 ORT 会话建立），与预热后的推理延迟分开统计；
seekdb 检索耗时为浏览器发起请求到拿到排序结果的往返时间，包含网络与库内检索；
端到端 = 查询向量化 + seekdb 检索。

## 结果总表

${head}
${rows.join("\n")}

## 分配置明细

${detail}
`;
}

run().catch((err) => {
  console.error("[bench] 失败:", err);
  process.exit(1);
});
