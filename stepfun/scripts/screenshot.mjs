/**
 * 生成界面截图（用于评审材料 / 自检 UI）。
 * 用法：node scripts/screenshot.mjs [--out docs/screenshot.png] [--model <id>] [--device auto]
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};

const APP_URL = arg("app-url", "http://127.0.0.1:8787");
const OUT = path.resolve(root, arg("out", "docs/screenshot.png"));
const MODEL = arg("model", "Xenova/bge-small-zh-v1.5");
const DEVICE = arg("device", "auto");
const EDGE =
  arg("edge", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe") ||
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function ensureServer() {
  try {
    const res = await fetch(`${APP_URL}/api/health`);
    if (res.ok) return null;
  } catch {
    /* not running */
  }
  const child = spawn("node", ["server/index.mjs"], { cwd: root, stdio: "ignore" });
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

const corpusDir = path.join(root, "data", "corpus");
const docs = fs.existsSync(corpusDir)
  ? fs
      .readdirSync(corpusDir)
      .filter((f) => f.endsWith(".md"))
      .sort()
      .slice(0, 12)
      .map((f) => {
        const text = fs.readFileSync(path.join(corpusDir, f), "utf8").trim();
        return { id: path.basename(f, ".md"), text, metadata: { title: text.split("\n")[0].replace(/^#+\s*/, "") } };
      })
  : [];

const server = await ensureServer();
const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: true,
  args: ["--enable-unsafe-webgpu", "--enable-features=Vulkan", "--no-sandbox"],
});
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1 });
await page.goto(APP_URL, { waitUntil: "domcontentloaded", timeout: 60000 });
await page.waitForFunction("window.__stepfun && window.__stepfun.ready", { timeout: 60000 });

await page.evaluate(async (m, d) => {
  await window.__stepfun.setModel(m);
  await window.__stepfun.loadModel({ modelId: m, dtype: "q8", device: d });
}, MODEL, DEVICE);

if (docs.length) {
  await page.evaluate(async (d) => await window.__stepfun.importDocuments(d, { batchSize: 8 }), docs);
}
await page.evaluate(async () => {
  await window.__stepfun.search("向量索引与全文索引如何结合做混合检索", 8);
});

// 让界面也渲染出来
await page.evaluate((d) => {
  const ta = document.getElementById("corpus-input");
  if (ta) ta.value = d.map((x) => x.text).join("\n\n");
  const qi = document.getElementById("query-input");
  if (qi) {
    qi.value = "向量索引与全文索引如何结合做混合检索";
    qi.dispatchEvent(new Event("input", { bubbles: true }));
  }
}, docs);
await page.evaluate(() => {
  document.querySelector("#search-btn")?.click();
});
await sleep(2500);
await page.evaluate(() => {
  document.querySelector("#explain-btn")?.click();
});
await sleep(1500);

fs.mkdirSync(path.dirname(OUT), { recursive: true });
await page.screenshot({ path: OUT, fullPage: false });
console.log(`[screenshot] 已保存 ${OUT}`);

await browser.close();
if (server) server.kill();
process.exit(0);
