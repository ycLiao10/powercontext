/**
 * 端侧推理性能与质量基准（编排脚本）。
 *
 * 它用 CDP 驱动本机 Chrome / Edge，对每一组配置（量化档位 × 后端 × batch）做：
 *   1. 全新浏览器 profile 打开基准页 → 冷启动加载（含权重下载，下载耗时由 Network 事件单独统计）
 *   2. 预热后重建会话的加载耗时
 *   3. 36 条语料的向量化耗时与吞吐（同时跑 batch = 1 / 8 / 16 做对照）
 *   4. 写入 seekdb（含索引刷新）耗时
 *   5. 25 条查询的向量���耗时 + seekdb 检索耗时（服务端 SQL 时间）
 *   6. 端到端耗时（查询向量计算 → 检索 → 结果渲染）
 *   7. 用同一组向量跑固定评测集，算 Recall@5 / nDCG@5
 *
 * 每个指标重复 reps 次，取中位数；原始数据写 evidence/bench-raw.json，
 * 汇总写 evidence/bench.json。
 *
 * 用法：
 *   node scripts/bench-browser.mjs --url http://127.0.0.1:5173/ --reps 5
 *   node scripts/bench-browser.mjs --url http://127.0.0.1:5173/ --only q8-webgpu
 */

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STEPFUN_ROOT = path.resolve(__dirname, '..');

const BROWSER_CANDIDATES = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  // 非管理员安装（按用户）时的落盘位置
  path.join(process.env.LOCALAPPDATA ?? '', 'Google\\Chrome\\Application\\chrome.exe'),
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
];

const argv = process.argv.slice(2);
const arg = (n, d = null) => {
  const i = argv.indexOf(`--${n}`);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : d;
};

const URL_ = arg('url', 'http://127.0.0.1:5173/');
const PROXY = arg('proxy', 'http://127.0.0.1:8787').replace(/\/$/, '');
const REPS = Number(arg('reps', '5'));
const ONLY = arg('only', null);
// --tag edge / chrome：不同浏览器的结果分文件落盘，再用 scripts/merge-bench.mjs 合并，
// 避免后一次运行覆盖前一次。
const TAG = arg('tag', null);
const OUT_RAW = path.join(STEPFUN_ROOT, 'evidence', `bench-raw${TAG ? `.${TAG}` : ''}.json`);
const OUT_SUMMARY = path.join(STEPFUN_ROOT, 'evidence', `bench${TAG ? `.${TAG}` : ''}.json`);
const MODEL_ID = 'Xenova/bge-small-zh-v1.5';

const MODEL_FILES = {
  fp32: 'onnx/model.onnx',
  fp16: 'onnx/model_fp16.onnx',
  q8: 'onnx/model_quantized.onnx',
};

/** 要跑的配置。batch=16 那一行是「优化后最佳组合」，跑完再按实测结果调整。 */
const CONFIGS = [
  { id: 'fp32-wasm', dtype: 'fp32', device: 'wasm', batch: 8, note: 'fp32 基线（WASM）' },
  { id: 'fp32-webgpu', dtype: 'fp32', device: 'webgpu', batch: 8, note: 'fp32 + WebGPU' },
  { id: 'q8-webgpu', dtype: 'q8', device: 'webgpu', batch: 8, note: 'q8 + WebGPU' },
  { id: 'q8-wasm', dtype: 'q8', device: 'wasm', batch: 8, note: 'q8 + WASM' },
  { id: 'q8-wasm-b16', dtype: 'q8', device: 'wasm', batch: 16, note: 'q8 + WASM + batch=16' },
  { id: 'q8-webgpu-b16', dtype: 'q8', device: 'webgpu', batch: 16, note: 'q8 + WebGPU + batch=16' },
  { id: 'fp16-webgpu', dtype: 'fp16', device: 'webgpu', batch: 8, note: 'fp16 + WebGPU' },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------- 评测指标（与 scripts/search.js 完全一致的公式） ----------------
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

function median(arr) {
  const a = arr.filter((v) => Number.isFinite(v)).sort((x, y) => x - y);
  if (!a.length) return null;
  const mid = Math.floor(a.length / 2);
  return a.length % 2 ? a[mid] : (a[mid - 1] + a[mid]) / 2;
}

// ---------------- 浏览器驱动 ----------------
const browserPath = arg('browser', BROWSER_CANDIDATES.find((p) => fs.existsSync(p)));
if (!browserPath) {
  console.error('找不到 Chrome / Edge，请用 --browser 指定');
  process.exit(1);
}

async function runConfig(cfg) {
  const port = 9400 + Math.floor(Math.random() * 400);
  const profile = path.join(os.tmpdir(), `stepfun-bench-${cfg.id}-${Date.now()}`);
  const child = spawn(
    browserPath,
    [
      '--headless=new',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      '--window-size=1280,900',
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profile}`,
      'about:blank',
    ],
    { detached: true, stdio: 'ignore' },
  );
  child.unref();

  const result = { ...cfg, browser: path.basename(browserPath), ok: false, error: null };

  try {
    for (let i = 0; i < 90; i += 1) {
      try {
        const r = await fetch(`http://127.0.0.1:${port}/json/version`);
        if (r.ok) break;
      } catch {
        /* 等 */
      }
      await sleep(500);
    }

    const created = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' });
    const target = await created.json();
    const ws = new WebSocket(target.webSocketDebuggerUrl);

    let msgId = 0;
    const pending = new Map();
    /** 网络事件：用来单独统计权重下载耗时 */
    const netRequests = new Map();
    const consoleLines = [];
    ws.addEventListener('message', (ev) => {
      const d = JSON.parse(ev.data);
      if (d.id && pending.has(d.id)) {
        pending.get(d.id)(d);
        pending.delete(d.id);
      }
      if (d.method === 'Runtime.consoleAPICalled') {
        consoleLines.push(
          `[${d.params.type}] ` +
            (d.params.args || []).map((a) => a.value ?? a.description ?? a.type).join(' '),
        );
      }
      if (d.method === 'Runtime.exceptionThrown') {
        consoleLines.push(`[exception] ${d.params.exceptionDetails?.text}`);
      }
      if (d.method === 'Network.requestWillBeSent') {
        netRequests.set(d.params.requestId, {
          url: d.params.request.url,
          start: d.params.timestamp,
        });
      }
      if (d.method === 'Network.loadingFinished') {
        const r = netRequests.get(d.params.requestId);
        if (r) {
          r.end = d.params.timestamp;
          r.bytes = d.params.encodedDataLength;
        }
      }
    });
    await new Promise((res, rej) => {
      ws.addEventListener('open', res);
      ws.addEventListener('error', rej);
    });
    const send = (method, params = {}) => {
      const id = ++msgId;
      return new Promise((resolve) => {
        pending.set(id, resolve);
        ws.send(JSON.stringify({ id, method, params }));
      });
    };
    const evaluate = async (expression, awaitPromise = false) => {
      const r = await send('Runtime.evaluate', {
        expression,
        returnByValue: true,
        awaitPromise,
      });
      if (r.result?.exceptionDetails) {
        throw new Error(r.result.exceptionDetails.text || '页面脚本异常');
      }
      return r.result?.result?.value;
    };

    await send('Page.enable');
    await send('Runtime.enable');
    await send('Network.enable');
    await send('DOM.enable');

    const benchUrl = `${URL_}bench.html?dtype=${cfg.dtype}&device=${cfg.device}&batch=${cfg.batch}`;
    await send('Page.navigate', { url: benchUrl });

    // 等基准页就绪
    for (let i = 0; i < 120; i += 1) {
      const ready = await evaluate('typeof window.__benchRun === "function"');
      if (ready) break;
      await sleep(500);
    }
    result.pageInfo = await evaluate('window.__benchInfo ? window.__benchInfo() : null');

    // 跑基准
    const payload = {
      corpus: CORPUS,
      queries: QUERIES.map((q) => ({ id: q.id, query: q.query })),
      reps: REPS,
      batches: [1, 8, 16],
    };
    const raw = await evaluate(
      `window.__benchRun(${JSON.stringify(payload)})`,
      true,
    );
    result.page = raw;

    // 权重下载事件（取模型 onnx 那条）
    const onnxEvents = [...netRequests.values()].filter(
      (r) => r.url.includes('/models/') && r.url.includes('.onnx'),
    );
    result.weightDownload = onnxEvents.map((r) => ({
      url: r.url.split('/').slice(-3).join('/'),
      ms: r.end && r.start ? (r.end - r.start) * 1000 : null,
      bytes: r.bytes ?? null,
    }));

    // 模型文件体积（代理返回的 Content-Length = 磁盘上的实际字节数）
    result.modelFile = MODEL_FILES[cfg.dtype];
    const head = await fetch(
      `${PROXY}/models/${MODEL_ID}/resolve/main/${MODEL_FILES[cfg.dtype]}`,
      { method: 'HEAD' },
    );
    result.modelBytes = Number(head.headers.get('content-length')) || null;

    result.ok = !raw?.error;
    result.error = raw?.error ?? null;
    result.stack = raw?.stack ?? null;
    result.console = consoleLines.slice(0, 40);
    ws.close();
  } catch (err) {
    result.error = err?.message || String(err);
  } finally {
    try {
      process.kill(child.pid);
    } catch {
      /* 已退出 */
    }
    try {
      fs.rmSync(profile, { recursive: true, force: true });
    } catch {
      /* 忽略 */
    }
  }
  return result;
}

// ---------------- 主流程 ----------------
const corpusRaw = fs
  .readFileSync(path.join(STEPFUN_ROOT, 'data', 'corpus.jsonl'), 'utf8')
  .split('\n')
  .filter((l) => l.trim());
const CORPUS = corpusRaw.map((l) => JSON.parse(l));
const QUERIES = JSON.parse(
  fs.readFileSync(path.join(STEPFUN_ROOT, 'data', 'queries.json'), 'utf8'),
).queries;

const configs = ONLY ? CONFIGS.filter((c) => c.id === ONLY) : CONFIGS;
const rawRuns = [];

for (const cfg of configs) {
  process.stdout.write(`\n=== ${cfg.id} (${cfg.note}) ===\n`);
  const t0 = Date.now();
  const r = await runConfig(cfg);
  rawRuns.push(r);
  if (r.ok) {
    console.log(
      `  冷加载 ${r.page.coldLoadMs?.toFixed(0)}ms  预热加载 ${r.page.warmLoadMs?.toFixed(0)}ms` +
        `  权重下载 ${r.weightDownload?.[0]?.ms?.toFixed(0) ?? 'N/A'}ms` +
        `  模型 ${((r.modelBytes ?? 0) / 1048576).toFixed(1)}MB` +
        `  耗时 ${((Date.now() - t0) / 1000).toFixed(0)}s`,
    );
    const last = r.page.reps[r.page.reps.length - 1];
    console.log(
      `  向量化(36条) ${last.batchRuns[cfg.batch]?.ms?.toFixed(0)}ms` +
        `  吞吐 ${last.batchRuns[cfg.batch]?.docsPerSec?.toFixed(2)} 条/秒` +
        `  写入 ${last.write?.serverInsertMs?.toFixed(0)}ms` +
        `  检索 ${median(last.queries.map((q) => q.serverSearchMs))?.toFixed(1)}ms` +
        `  端到端 ${last.endToEnd?.totalMs?.toFixed(0)}ms`,
    );
  } else {
    console.log(`  ✗ 失败：${r.error}`);
    if (r.stack) console.log(`  stack: ${String(r.stack).split('\n').slice(0, 6).join('\n         ')}`);
    if (r.console?.length) {
      console.log('  控制台：');
      for (const l of r.console.slice(0, 15)) console.log(`    ${l}`);
    }
  }
}

// ---------------- 汇总 ----------------
function summarize(run) {
  if (!run.ok || !run.page) {
    return {
      id: run.id,
      dtype: run.dtype,
      device: run.device,
      batch: run.batch,
      note: run.note,
      status: 'N/A',
      reason: run.error || '未知原因',
      modelBytes: run.modelBytes ?? null,
    };
  }
  const reps = run.page.reps;
  const batchRuns = {};
  for (const b of [1, 8, 16]) {
    const ms = reps.map((r) => r.batchRuns[b]?.ms).filter(Number.isFinite);
    const dps = reps.map((r) => r.batchRuns[b]?.docsPerSec).filter(Number.isFinite);
    batchRuns[b] = {
      ms: ms.length ? Number(median(ms).toFixed(1)) : null,
      docsPerSec: dps.length ? Number(median(dps).toFixed(3)) : null,
    };
  }

  // 质量：取第 1 次 rep 的排序结果算 Recall@5 / nDCG@5（同配置下各次排序应完全一致）
  const rep0 = reps[0];
  let recall = null;
  let ndcg = null;
  if (rep0) {
    let rs = 0;
    let ns = 0;
    for (const q of rep0.queries) {
      const spec = QUERIES.find((x) => x.id === q.id);
      if (!spec) continue;
      rs += recallAtK(q.ids, spec.relevant, 5);
      ns += ndcgAtK(q.ids, spec.graded, 5);
    }
    recall = Number((rs / rep0.queries.length).toFixed(4));
    ndcg = Number((ns / rep0.queries.length).toFixed(4));
  }

  // 各次 rep 的排序是否一致（保证质量指标稳定可复现）
  const rankingsStable =
    reps.length < 2 ||
    reps.every(
      (r) => JSON.stringify(r.queries.map((q) => q.ids)) === JSON.stringify(rep0.queries.map((q) => q.ids)),
    );

  const e2e = reps.map((r) => r.endToEnd?.totalMs).filter(Number.isFinite);
  const e2eEmbed = reps.map((r) => r.endToEnd?.embedMs).filter(Number.isFinite);
  const qv = reps.map((r) => r.queryVectorizeTotalMs).filter(Number.isFinite);
  const qvPer = reps.map((r) => r.queryVectorizePerQueryMs).filter(Number.isFinite);
  const srvSearch = reps.flatMap((r) => r.queries.map((q) => q.serverSearchMs)).filter(Number.isFinite);
  const writeSrv = reps.map((r) => r.write?.serverInsertMs).filter(Number.isFinite);
  const writeCli = reps.map((r) => r.write?.clientMs).filter(Number.isFinite);

  return {
    id: run.id,
    note: run.note,
    dtype: run.dtype,
    device: run.device,
    batch: run.batch,
    status: 'ok',
    browser: run.browser,
    modelFile: run.modelFile,
    modelBytes: run.modelBytes,
    modelMB: run.modelBytes ? Number((run.modelBytes / 1048576).toFixed(2)) : null,
    coldLoadMs: Number(run.page.coldLoadMs.toFixed(1)),
    warmLoadMs: Number(run.page.warmLoadMs.toFixed(1)),
    weightDownloadMs: run.weightDownload?.[0]?.ms
      ? Number(run.weightDownload[0].ms.toFixed(1))
      : null,
    weightBytes: run.weightDownload?.[0]?.bytes ?? null,
    vectorize36: batchRuns[run.batch],
    batchRuns,
    writeAndIndex: {
      serverInsertMs: writeSrv.length ? Number(median(writeSrv).toFixed(1)) : null,
      clientRoundTripMs: writeCli.length ? Number(median(writeCli).toFixed(1)) : null,
    },
    queryVectorize: {
      totalMs25: qv.length ? Number(median(qv).toFixed(1)) : null,
      perQueryMs: qvPer.length ? Number(median(qvPer).toFixed(2)) : null,
    },
    seekdbSearchMs: srvSearch.length ? Number(median(srvSearch).toFixed(2)) : null,
    endToEndQueryMs: e2e.length ? Number(median(e2e).toFixed(1)) : null,
    endToEndEmbedMs: e2eEmbed.length ? Number(median(e2eEmbed).toFixed(1)) : null,
    recallAt5: recall,
    ndcgAt5: ndcg,
    rankingsStable,
    reps: reps.length,
  };
}

const summaries = rawRuns.map(summarize);

const fp32 = summaries.find((s) => s.dtype === 'fp32' && s.status === 'ok');
for (const s of summaries) {
  if (s.status !== 'ok') continue;
  s.compressionVsFp32 = fp32?.modelBytes ? Number((fp32.modelBytes / s.modelBytes).toFixed(2)) : null;
}

const rawDoc = {
  generatedAt: new Date().toISOString(),
  host: {
    os: `${os.type()} ${os.release()}`,
    cpus: os.cpus().length,
    cpuModel: os.cpus()[0]?.model ?? null,
    totalMemGB: Number((os.totalmem() / 1073741824).toFixed(1)),
  },
  browser: path.basename(browserPath),
  browserExe: browserPath,
  reps: REPS,
  note: '每个指标的原始 5 次测量都在 runs[].page.reps 里；汇总（取中位数）见 bench.json',
  runs: rawRuns,
};

const summaryDoc = {
  generatedAt: new Date().toISOString(),
  model: MODEL_ID,
  browser: path.basename(browserPath),
  reps: REPS,
  methodology: {
    corpus: `${CORPUS.length} 条语料（data/corpus.jsonl）`,
    queries: `${QUERIES.length} 条固定查询（data/queries.json，人工标注 relevant / graded）`,
    median: '每个指标重复 5 次取中位数',
    coldLoad: '全新浏览器 profile 下的首次 pipeline() 创建，含权重下载',
    warmLoad: '同一页面内第二次创建 pipeline（权重已在浏览器 Cache）',
    vectorize: '36 条语料一次批量向量化（标题+正文，与生产路径一致）',
    writeAndIndex: 'POST /api/documents，代理侧计时含 INSERT 与索引刷新（sync_mode=IMMEDIATE）',
    seekdbSearch: '代理返回的 tookMs：服务端执行混合检索 SQL 的耗时',
    endToEndQuery: '查询向量计算 → 混合检索 → 结果渲染到 DOM（双 rAF）',
    quality: '用同一组向量跑固定评测集，Recall@5 / nDCG@5 公式与 scripts/search.js 一致',
  },
  configs: summaries,
};

fs.mkdirSync(path.dirname(OUT_RAW), { recursive: true });
fs.writeFileSync(OUT_RAW, JSON.stringify(rawDoc, null, 2), 'utf8');
fs.writeFileSync(OUT_SUMMARY, JSON.stringify(summaryDoc, null, 2), 'utf8');

console.log('\n=== 汇总 ===');
console.log(
  ['配置', 'MB', '冷加载ms', '预热ms', '向量化ms', '条/秒', '写入ms', '检索ms', '端到端ms', 'R@5', 'nDCG@5']
    .map((s, i) => (i === 0 ? s : s.padStart(10)))
    .join(' '),
);
for (const s of summaries) {
  if (s.status !== 'ok') {
    console.log(`${s.id.padEnd(10)} N/A — ${s.reason}`);
    continue;
  }
  console.log(
    [
      s.id,
      String(s.modelMB),
      String(s.coldLoadMs),
      String(s.warmLoadMs),
      String(s.vectorize36.ms),
      String(s.vectorize36.docsPerSec),
      String(s.writeAndIndex.serverInsertMs),
      String(s.seekdbSearchMs),
      String(s.endToEndQueryMs),
      String(s.recallAt5),
      String(s.ndcgAt5),
    ]
      .map((x, i) => (i === 0 ? x.padEnd(10) : x.padStart(10)))
      .join(' '),
  );
}
console.log(`\n✓ ${path.relative(STEPFUN_ROOT, OUT_RAW)}`);
console.log(`✓ ${path.relative(STEPFUN_ROOT, OUT_SUMMARY)}`);
