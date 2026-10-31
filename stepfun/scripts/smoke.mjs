/**
 * 端到端冒烟测试：用真实浏览器（Edge）跑通
 * 「加载端侧模型 → 本地向量化 → 写入 seekdb → 混合检索 → 展示结果」全流程。
 *
 * 用法：node scripts/smoke.mjs [--model <id>] [--device auto|webgpu|wasm] [--dtype fp32|fp16|q8] [--keep-server]
 */
import { spawn } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};

const APP_URL = arg("app-url", "http://127.0.0.1:8787");
const MODEL = arg("model", "Xenova/bge-small-zh-v1.5");
const DEVICE = arg("device", "auto");
const DTYPE = arg("dtype", "q8");
const EDGE =
  arg("edge", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe") ||
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function ensureServer() {
  try {
    const res = await fetch(`${APP_URL}/api/health`);
    if (res.ok) return null;
  } catch {
    /* 未启动 */
  }
  if (!fs.existsSync(path.join(root, "web", "dist", "index.html"))) {
    throw new Error("web/dist 不存在，请先执行 npm run build");
  }
  console.log("[smoke] 启动本地服务…");
  const child = spawn("node", ["server/index.mjs"], {
    cwd: root,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (d) => process.stdout.write(`[api] ${d}`));
  child.stderr.on("data", (d) => process.stderr.write(`[api] ${d}`));
  for (let i = 0; i < 60; i++) {
    await sleep(500);
    try {
      const res = await fetch(`${APP_URL}/api/health`);
      if (res.ok) return child;
    } catch {
      /* keep waiting */
    }
  }
  throw new Error("本地服务启动超时");
}

const SAMPLE = [
  {
    id: "smoke-1",
    text: "seekdb 支持向量索引 HNSW 与全文索引 IK 分词，可以在一张表上同时做语义检索与关键词检索。",
    metadata: { title: "混合检索能力" },
  },
  {
    id: "smoke-2",
    text: "在浏览器中使用 Transformers.js 运行 embedding 模型时，优先使用 WebGPU 后端，不可用时降级到 WASM。",
    metadata: { title: "端侧推理后端" },
  },
  {
    id: "smoke-3",
    text: "INT8 动态量化可以把 embedding 模型体积压缩到 FP32 的四分之一左右，检索质量通常只有极小幅度下降。",
    metadata: { title: "模型量化" },
  },
  {
    id: "smoke-4",
    text: "增量写入数据后需要刷新向量索引，新导入的文档才能立即被检索到。",
    metadata: { title: "索引刷新" },
  },
];

async function main() {
  const server = await ensureServer();
  let browser;
  try {
    browser = await puppeteer.launch({
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
    page.on("console", (m) => {
      const t = m.text();
      if (t.includes("[stepfun]") || m.type() === "error") console.log(`[page] ${t}`);
    });
    page.on("pageerror", (e) => console.error(`[pageerror] ${e.message}`));

    console.log(`[smoke] 打开 ${APP_URL}`);
    await page.goto(APP_URL, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForFunction("window.__stepfun && window.__stepfun.ready", { timeout: 60000 });

    const tLoad = Date.now();
    const loaded = await page.evaluate(
      async (modelId, dtype, device) => {
        await window.__stepfun.setModel(modelId);
        return await window.__stepfun.loadModel({ modelId, dtype, device });
      },
      MODEL,
      DTYPE,
      DEVICE
    );
    console.log(`[smoke] 模型加载完成（${Date.now() - tLoad} ms）:`, loaded);
    if (loaded.backend !== DEVICE && DEVICE !== "auto") {
      console.warn(
        `[smoke] 注意：请求 ${DEVICE}，实际使用 ${loaded.backend}（自动降级）`
      );
    }

    const imported = await page.evaluate(
      async (docs) => await window.__stepfun.importDocuments(docs, { batchSize: 4 }),
      SAMPLE
    );
    console.log("[smoke] 导入完成:", imported);

    const searched = await page.evaluate(
      async () => await window.__stepfun.search("向量索引和全文检索怎么结合", 4)
    );
    console.log("[smoke] 检索结果:");
    for (const r of searched.results) {
      console.log(
        `   - ${r.id} score=${r.score.toFixed(4)} :: ${String(r.document).slice(0, 48)}…`
      );
    }
    console.log(
      `[smoke] 查询向量化 ${searched.queryEmbedMs.toFixed(1)} ms | seekdb 检索 ${searched.dbMs.toFixed(
        1
      )} ms | 端到端 ${searched.endToEndMs.toFixed(1)} ms`
    );

    const ok = searched.results.length > 0;
    console.log(ok ? "[smoke] PASS" : "[smoke] FAIL：检索没有返回结果");
    if (!ok) process.exitCode = 1;
  } finally {
    await browser?.close();
    if (server && !argv.includes("--keep-server")) server.kill();
  }
  // 显式退出：避免被 kill 掉的子进程句柄拖住事件循环
  process.exit(process.exitCode ?? 0);
}

main().catch((err) => {
  console.error("[smoke] 失败:", err);
  process.exit(1);
});
