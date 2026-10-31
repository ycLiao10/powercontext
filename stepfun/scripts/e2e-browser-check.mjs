/**
 * 浏览器端到端验收脚本（无第三方依赖，只用 Node 内置能力 + CDP）。
 *
 * 它做的事，完全对应赛题「端侧推理 + seekdb 混合检索」的核心闭环：
 *   1. 打开页面，等待模型加载完成，读取**后端徽标**（WebGPU / WASM）
 *   2. 导入 N 条文档（浏览器内分批向量化 → POST 代理 → 写入 seekdb）
 *   3. 执行一次混合检索，确认返回结果（含 hybrid_score 与 ft/vec 命中来源）
 *   4. 再导入 1 条新文档，**立刻**检索，确认新增文档立即可见（增量可见性）
 *   5. 截图存档
 *
 * 用法：
 *   node scripts/e2e-browser-check.mjs --url http://127.0.0.1:5173/
 *   node scripts/e2e-browser-check.mjs --url http://127.0.0.1:5173/ --disable-gpu
 *   node scripts/e2e-browser-check.mjs --url http://127.0.0.1:5173/ --browser "C:\\path\\chrome.exe"
 *
 * 参数：
 *   --url        页面地址（默认 http://127.0.0.1:5173/）
 *   --browser    浏览器可执行文件路径（Chrome / Edge 均可，自动探测候选列表）
 *   --edge       同 --browser（保留旧名，向后兼容）
 *   --disable-gpu  模拟「没有 WebGPU」的环境（chrome://flags 等价物）
 *   --port       DevTools 端口（默认 9333）
 *   --shot       截图保存路径
 *   --keep       结束后不关闭浏览器
 */

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const BROWSER_CANDIDATES = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  // 非管理员安装（按用户）时的落盘位置
  path.join(process.env.LOCALAPPDATA ?? '', 'Google\\Chrome\\Application\\chrome.exe'),
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
  '/snap/bin/chromium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
];

const argv = process.argv.slice(2);
const arg = (name, fallback = null) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};
const has = (name) => argv.includes(`--${name}`);

const URL_ = arg('url', 'http://127.0.0.1:5173/');
const PROXY = arg('proxy', 'http://127.0.0.1:8787');
const PORT = arg('port', '9333');
const SHOT = arg('shot', null);
const DISABLE_GPU = has('disable-gpu');
const RESET = has('reset');
const BROWSER =
  arg('browser') ?? arg('edge') ?? BROWSER_CANDIDATES.find((p) => fs.existsSync(p)) ?? null;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** --reset：先清空知识库，让条数可预期。 */
if (RESET) {
  const res = await fetch(`${PROXY.replace(/\/$/, '')}/api/documents`, { method: 'DELETE' });
  console.log(`[0] 清空知识库：HTTP ${res.status}`);
}

/** 导入用的 10 条文档（每条一个空行分隔的段落）。 */
const DOCS = [
  ['缓存穿透的成因与治理', '缓存穿透是指查询一个根本不存在的键，缓存和数据库都不命中，请求每次都打到数据库上。常见成因是恶意构造大量不存在的键。最有效的治理手段是布隆过滤器，在请求到达缓存之前先做一次存在性判断。'],
  ['缓存雪崩与缓存击穿', '缓存雪崩指大量键在同一时刻集中失效，造成数据库压力瞬间飙升，通常用随机过期时间或者多级缓存来缓解。缓存击穿则是单个热点键失效导致大量请求同时打到数据库，可以用互斥锁或者逻辑过期来应对。'],
  ['向量数据库与相似度检索', '向量数据库把高维浮点向量建立近似最近邻索引，常用 HNSW 或者 IVF 索引结构。相似度度量常见有余弦相似度、内积和欧氏距离。HNSW 的召回率和查询延迟之间需要通过 m 和 ef_construction 参数做权衡。'],
  ['全文检索与倒排索引', '倒排索引把词条映射到包含它的文档列表，使得关键词查询不必扫描全表。中文分词是全文检索效果的关键，默认分词器会把整段中文当作一个词元，通常需要 ngram 或者词典分词配合自然语言模式。'],
  ['混合检索与排名融合', '混合检索同时使用向量语义匹配和全文词法匹配，两者分数量纲不同，直接加权需要归一化。倒数排名融合 RRF 只使用名次，公式为 1/(k+rank)，不需要任何归一化，是工业界的常见做法。'],
  ['索引同步模式与增量可见性', '向量索引的同步模式决定了新写入的数据何时可以被检索到。默认异步模式下，新文档写入了基表但索引尚未同步，查询会查不到它。同步模式设置为立即之后，写入完成即可被检索到，这是增量导入场景的硬性前提。'],
  ['浏览器内的本地推理', '把嵌入模型跑在浏览器里可以避免把用户文本发送到云端。Transformers.js 配合 WebGPU 后端可以在支持的显卡上获得明显加速，没有 WebGPU 时可以降级到 WASM 后端，代价是速度变慢但功能完全一致。'],
  ['ONNX Runtime 的执行后端', 'ONNX Runtime Web 提供 wasm、webgpu 和 webgl 等多种执行后端。WebGPU 后端基于 JSEP 规范，可以把算子映射到计算着色器上执行。wasm 后端依赖 WebAssembly SIMD 和多线程，在现代浏览器上依然有不错的吞吐。'],
  ['Redis 与分布式缓存', 'Redis 是最常用的分布式缓存实现，支持持久化、发布订阅和 Lua 脚本。缓存三大经典问题是穿透、击穿和雪崩，分别对应不存在的键、单热点键失效和批量键同时失效。'],
  ['混合检索的评测指标', '检索质量常用 Recall@K、nDCG@K 和 MRR 衡量。Recall@K 衡量相关文档被召回的比例，nDCG@K 考虑相关性等级和排名位置，MRR 关注首个相关结果的位置。评测集需要人工标注相关性和分级相关性。'],
];

/** 增量导入用的第 11 条（含唯一词「雪碧鱼」，用于验证立刻可检索）。 */
const EXTRA = [
  ['雪碧鱼的可观测性实践', '雪碧鱼是一套内部中间件监控方案，采集服务调用链的延迟分位数据，并用滑动窗口做基线告警。雪碧鱼不参与业务链路，只做旁路采样，因此不会影响主链路延迟。'],
];

const toText = (list) => list.map(([t, c]) => `# ${t}\n${c}`).join('\n\n');

if (!BROWSER) {
  console.error('找不到 Chrome / Edge 可执行文件，请用 --browser（或 --edge）指定路径');
  process.exit(1);
}

/** 把一组文档写成临时文件，走「选择文件」导入路径（id 按文件名区分，不会互相覆盖）。 */
const writeTempDoc = (list, name) => {
  const file = path.join(os.tmpdir(), name);
  fs.writeFileSync(file, toText(list), 'utf8');
  return file;
};
const FILE_MAIN = writeTempDoc(DOCS, 'stepfun-e2e-main.txt');
const FILE_EXTRA = writeTempDoc(EXTRA, 'stepfun-e2e-extra.txt');

// ---------- 启动浏览器 ----------
const profile = path.join(os.tmpdir(), `stepfun-e2e-${PORT}`);
const browserArgs = [
  '--headless=new',
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-extensions',
  '--window-size=1440,900',
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  'about:blank',
];
if (DISABLE_GPU) {
  browserArgs.unshift('--disable-gpu', '--disable-gpu-compositing');
}

const child = spawn(BROWSER, browserArgs, { detached: true, stdio: 'ignore' });
child.unref();

async function waitForDevtools() {
  for (let i = 0; i < 60; i += 1) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (res.ok) return;
    } catch {
      /* 还没起来 */
    }
    await sleep(500);
  }
  throw new Error('DevTools 端点未就绪');
}
await waitForDevtools();

const created = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' });
const target = await created.json();
const ws = new WebSocket(target.webSocketDebuggerUrl);

let msgId = 0;
const pending = new Map();
const consoleLines = [];
ws.addEventListener('message', (ev) => {
  const data = JSON.parse(ev.data);
  if (data.id && pending.has(data.id)) {
    pending.get(data.id)(data);
    pending.delete(data.id);
  }
  if (data.method === 'Runtime.consoleAPICalled') {
    consoleLines.push(
      `[${data.params.type}] ` +
        (data.params.args || []).map((a) => a.value ?? a.description ?? a.type).join(' '),
    );
  }
  if (data.method === 'Runtime.exceptionThrown') {
    consoleLines.push(`[exception] ${data.params.exceptionDetails?.text}`);
  }
});
await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve);
  ws.addEventListener('error', reject);
});

function send(method, params = {}) {
  const id = ++msgId;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const r = await send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (r.result?.exceptionDetails) {
    throw new Error(`页面脚本异常：${r.result.exceptionDetails.text}`);
  }
  return r.result?.result?.value;
}

await send('Page.enable');
await send('Runtime.enable');
await send('DOM.enable');

/** 往 React 受控组件里塞值：必须走原生 setter + input 事件。 */
const setField = (selector, value, tag) => `
(() => {
  const el = document.querySelector(${JSON.stringify(selector)});
  if (!el) return 'no-element';
  const proto = ${JSON.stringify(tag)} === 'input' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, ${JSON.stringify(value)});
  el.dispatchEvent(new Event('input', { bubbles: true }));
  return el.value;
})()`;

/** 走「选择文件」导入路径。 */
async function attachFile(filePath) {
  const doc = await send('DOM.getDocument', { depth: -1, pierce: true });
  const root = doc.result.root;
  const q = await send('DOM.querySelector', {
    nodeId: root.nodeId,
    selector: '#file',
  });
  const nodeId = q.result.nodeId;
  if (!nodeId) throw new Error('找不到文件输入框');
  await send('DOM.setFileInputFiles', { nodeId, files: [filePath] });
  await sleep(600);
  return evaluate("(document.querySelector('.tag-count')||{}).textContent");
}

/**
 * 填查询框并提交检索。
 * 注意：必须等**新的**结果出现，不能只看「有结果卡片」——
 * 上一次的卡片还在页面上，会造成假通过。
 * @param {string} query 查询词
 * @param {string} expect 首条卡片里必须出现的关键词
 */
async function search(query, expect) {
  const echoed = await evaluate(setField('.search-input', query, 'input'));
  if (echoed !== query) throw new Error(`查询框没有正确写入：${echoed}`);
  await sleep(300);
  await evaluate(
    "(() => { const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='检索'); b.click(); return 'ok'; })()",
  );
  return waitUntil(
    async () => {
      const t = await evaluate(
        "(() => { const c=document.querySelector('.result-card'); return c ? c.innerText : ''; })()",
      );
      return t && t.includes(expect) ? t : null;
    },
    120000,
    `检索「${query}」返回含「${expect}」的首条结果`,
  );
}

async function waitUntil(fn, timeoutMs, label) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const v = await fn();
    if (v) return v;
    await sleep(700);
  }
  throw new Error(`等待超时：${label}`);
}

// ---------- 1) 打开页面，等模型 ----------
await send('Page.navigate', { url: URL_ });
const badge = await waitUntil(
  async () => {
    const t = await evaluate(
      "(() => { const el = document.querySelector('.badge-value'); return el ? el.textContent : ''; })()",
    );
    return t && t !== '加载中' ? t : null;
  },
  180000,
  '模型加载完成',
);

const kb = await evaluate("(document.querySelector('.kb-value')||{}).textContent");
console.log(`[1] 后端徽标 = ${badge}；知识库原有 ${kb} 条`);

// ---------- 2) 导入 10 条 ----------
const parsed = await attachFile(FILE_MAIN);
console.log(`[2] 解析结果：${parsed}`);
if (!parsed || !parsed.includes('10')) throw new Error(`应解析 10 篇，实际：${parsed}`);

await evaluate(
  "(() => { const b=[...document.querySelectorAll('button')].find(x=>x.textContent.includes('向量化并导入')); b.click(); return b.textContent; })()",
);
const imported = await waitUntil(
  async () => {
    const t = await evaluate("(document.querySelector('.ok-msg')||{}).textContent");
    return t && t.includes('已导入') ? t : null;
  },
  180000,
  '导入完成',
);
console.log(`[3] ${imported}`);

// ---------- 3) 混合检索 ----------
const first = await search('缓存雪崩和穿透怎么处理', '缓存雪崩与缓存击穿');
console.log(`[4] 检索首条卡片：`);
console.log(
  first
    .split('\n')
    .slice(0, 6)
    .map((l) => '    ' + l)
    .join('\n'),
);
if (!first.includes('缓存')) throw new Error('检索结果与查询不符');
if (!/hybrid_score|全文命中|向量命中/.test(first) && !first.includes('命中')) {
  throw new Error('结果卡片缺少命中来源信息');
}

// ---------- 4) 增量导入 1 条，立刻检索 ----------
const parsed2 = await attachFile(FILE_EXTRA);
console.log(`[5] 增量文件解析结果：${parsed2}`);
await evaluate(
  "(() => { const b=[...document.querySelectorAll('button')].find(x=>x.textContent.includes('向量化并导入')); b.click(); return 'ok'; })()",
);
const imported2 = await waitUntil(
  async () => {
    const t = await evaluate("(document.querySelector('.ok-msg')||{}).textContent");
    return t && t.includes('已导入 1 条') ? t : null;
  },
  180000,
  '增量导入完成',
);
console.log(`[6] ${imported2}`);

const first2 = await search('雪碧鱼的可观测性实践', '雪碧鱼');
console.log(`[7] 增量可见性 —— 检索「雪碧鱼」首条卡片：`);
console.log(
  first2
    .split('\n')
    .slice(0, 6)
    .map((l) => '    ' + l)
    .join('\n'),
);
if (!first2.includes('雪碧鱼')) {
  throw new Error('增量可见性失败：新增文档没有排在首位');
}
console.log('    ✓ 新增文档写入后立即可被检索到');

if (SHOT) {
  const r = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  fs.writeFileSync(SHOT, Buffer.from(r.result.data, 'base64'));
  console.log(`[8] 截图已保存：${SHOT}`);
}

const errors = consoleLines.filter((l) => l.startsWith('[error]') || l.startsWith('[exception]'));
console.log('--- 页面错误（应为空）---');
console.log(errors.join('\n') || '(无)');

ws.close();
if (!has('keep')) {
  try {
    process.kill(child.pid);
  } catch {
    /* 已退出 */
  }
}
console.log('=== 端到端验收通过 ===');
process.exit(0);
