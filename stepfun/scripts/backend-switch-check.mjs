/**
 * 后端手动切换的端到端验证脚本。
 *
 * 验证三件事：
 *   1. 徽标可点击，展开下拉，三个选项都在
 *   2. 选 WASM → 刷新后仍是 WASM（localStorage 持久化）
 *   3. 选 WebGPU 但设备不支持 → 回落到自动（WASM）并显示原因
 *
 * 用法：
 *   node scripts/backend-switch-check.mjs --url http://127.0.0.1:5173/ \
 *        --browser "C:\path\chrome.exe"
 */

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const BROWSER_CANDIDATES = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  path.join(process.env.LOCALAPPDATA ?? '', 'Google\\Chrome\\Application\\chrome.exe'),
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
];

const argv = process.argv.slice(2);
const arg = (name, fallback = null) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};
const has = (name) => argv.includes(`--${name}`);

const URL_ = arg('url', 'http://127.0.0.1:5173/');
const PORT = arg('port', '9355');
const BROWSER = arg('browser', BROWSER_CANDIDATES.find((p) => fs.existsSync(p)));
/** --disable-gpu 模拟「没有 WebGPU」的环境，用来验证回落到自动 */
const DISABLE_GPU = has('disable-gpu');

if (!BROWSER) {
  console.error('找不到 Chrome / Edge，请用 --browser 指定');
  process.exit(1);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// 用带时间戳的目录，避免上一轮残留的浏览器进程锁住 profile
const profile = path.join(os.tmpdir(), `stepfun-bs-${PORT}-${Date.now()}`);

/** 清理临时 profile：浏览器可能还没完全退出，失败不影响验收结果。 */
function cleanupProfile() {
  try {
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
  } catch {
    /* 忽略：临时目录残留无害 */
  }
}

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
if (DISABLE_GPU) browserArgs.unshift('--disable-gpu', '--disable-gpu-compositing');

const child = spawn(BROWSER, browserArgs, { detached: true, stdio: 'ignore' });
child.unref();

async function waitForDevtools() {
  for (let i = 0; i < 60; i += 1) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (res.ok) return;
    } catch {
      /* 未就绪 */
    }
    await sleep(300);
  }
  throw new Error('DevTools 未就绪');
}

await waitForDevtools();
const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
const page = targets.find((t) => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.onopen = res;
  ws.onerror = rej;
});

let msgId = 0;
const pending = new Map();
/** 主框架导航计数：用来确认「切换后端确实触发了页面重载」。 */
let navCount = 0;
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.method === 'Page.frameNavigated' && m.params?.frame?.parentId === undefined) {
    navCount += 1;
  }
  if (m.id && pending.has(m.id)) {
    pending.get(m.id)(m);
    pending.delete(m.id);
  }
};
function send(method, params = {}) {
  const id = ++msgId;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expr) {
  const r = await send('Runtime.evaluate', {
    expression: expr,
    returnByValue: true,
    awaitPromise: true,
  });
  return r.result?.result?.value;
}
async function waitUntil(fn, timeoutMs, label) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const v = await fn();
    if (v) return v;
    await sleep(500);
  }
  throw new Error(`等待超时：${label}`);
}

await send('Page.enable');
await send('Runtime.enable');

// --mock-no-webgpu：注入一个让 requestAdapter() 返回 null 的补丁，
// 用来确定性地验证「手动选 WebGPU 但设备不支持 → 回落到自动」这条路径。
// （Chrome 的 --disable-gpu 并不会禁用 WebGPU，它会回退到软件后端，
//   所以拿不到适配器的情况只能用这种方式模拟。）
if (has('mock-no-webgpu')) {
  await send('Page.addScriptToEvaluateOnNewDocument', {
    source: `
      (() => {
        try {
          Object.defineProperty(navigator, 'gpu', {
            configurable: true,
            get() {
              return {
                requestAdapter: async () => null,
                getPreferredCanvasFormat: () => 'bgra8unorm',
              };
            },
          });
        } catch (e) { /* 忽略 */ }
      })();
    `,
  });
}

// ---------- 1) 打开页面，等模型加载 ----------
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
console.log(`[1] 初始后端徽标 = ${badge}`);

// ---------- 2) 徽标可点击，展开下拉 ----------
const menuInfo = await evaluate(
  `(() => {
    const btn = document.querySelector('.badge-button');
    if (!btn) return { ok: false, reason: '找不到 .badge-button' };
    btn.click();
    return { ok: true };
  })()`,
);
if (!menuInfo?.ok) throw new Error(`徽标不可点击：${menuInfo?.reason}`);
await sleep(400);
const options = await evaluate(
  `(() => [...document.querySelectorAll('.badge-option-label')].map(e => e.textContent.trim()))()`,
);
console.log(`[2] 下拉选项 = ${JSON.stringify(options)}`);
if (!Array.isArray(options) || options.length !== 3) {
  throw new Error(`下拉选项数量异常：${JSON.stringify(options)}`);
}
for (const want of ['自动', 'WebGPU', 'WASM']) {
  if (!options.some((o) => o.includes(want))) throw new Error(`下拉缺少选项：${want}`);
}

// ---------- 3) 选 WASM，刷新后仍在 WASM ----------
// 切换后端会触发页面重载（transformers.js 的会话链无法原地换 device），
// 用导航计数确认重载真的发生了，避免读到 reload 前的旧徽标值。
const navBefore = navCount;
await evaluate(
  `(() => {
    const b = document.querySelector('.badge-option[data-value="wasm"]');
    if (!b) return 'no option';
    b.click();
    return 'clicked';
  })()`,
);
await waitUntil(async () => navCount > navBefore, 30000, '页面完成重新加载');
const afterPick = await waitUntil(
  async () => {
    const t = await evaluate(
      "(() => { const el = document.querySelector('.badge-value'); return el ? el.textContent : ''; })()",
    );
    return t && t !== '加载中' ? t : null;
  },
  180000,
  '切到 WASM 后模型加载完成',
);
console.log(`[3] 选择 WASM 后徽标 = ${afterPick}`);
if (afterPick !== 'WASM') throw new Error(`选择 WASM 后实际是 ${afterPick}`);

const stored = await evaluate("(() => window.localStorage.getItem('stepfun:backend-pref'))()");
console.log(`[4] localStorage['stepfun:backend-pref'] = ${stored}`);
if (stored !== 'wasm') throw new Error(`localStorage 未写入 wasm，实际 ${stored}`);

// 刷新页面，验证持久化
const navBeforeReload = navCount;
await send('Page.navigate', { url: URL_ });
await waitUntil(async () => navCount > navBeforeReload, 30000, '刷新完成');
const afterReload = await waitUntil(
  async () => {
    const t = await evaluate(
      "(() => { const el = document.querySelector('.badge-value'); return el ? el.textContent : ''; })()",
    );
    return t && t !== '加载中' ? t : null;
  },
  180000,
  '刷新后模型加载完成',
);
console.log(`[5] 刷新后徽标 = ${afterReload}`);
if (afterReload !== 'WASM') throw new Error(`刷新后偏好丢失，实际 ${afterReload}`);

// ---------- 4) 选 WebGPU：可用则生效，不可用则回落到自动 ----------
await evaluate(
  `(() => {
    const btn = document.querySelector('.badge-button');
    btn.click();
    return true;
  })()`,
);
await sleep(400);
const navBeforeWebGPU = navCount;
await evaluate(
  `(() => {
    const b = document.querySelector('.badge-option[data-value="webgpu"]');
    if (!b) return 'no option';
    b.click();
    return 'clicked';
  })()`,
);
await waitUntil(async () => navCount > navBeforeWebGPU, 30000, '选 WebGPU 后页面完成重新加载');
const afterWebGPU = await waitUntil(
  async () => {
    const t = await evaluate(
      "(() => { const el = document.querySelector('.badge-value'); return el ? el.textContent : ''; })()",
    );
    return t && t !== '加载中' ? t : null;
  },
  180000,
  '选 WebGPU 后模型加载完成',
);
const prefNow = await evaluate("(() => window.localStorage.getItem('stepfun:backend-pref'))()");
const hint = await evaluate(
  "(() => { const el = document.querySelector('.hint'); return el ? el.textContent : ''; })()",
);
console.log(`[6] 选择 WebGPU 后徽标 = ${afterWebGPU}，偏好 = ${prefNow}`);
console.log(`    提示 = ${hint || '(无)'}`);

if (DISABLE_GPU || has('mock-no-webgpu')) {
  // 设备不支持 WebGPU：应回落到自动（偏好退回 auto），徽标为 WASM，并显示原因
  if (prefNow !== 'auto') {
    throw new Error(`WebGPU 不可用时偏好应回落到 auto，实际 ${prefNow}`);
  }
  if (afterWebGPU !== 'WASM') {
    throw new Error(`WebGPU 不可用时应降级 WASM，实际 ${afterWebGPU}`);
  }
  if (!hint || !hint.includes('WebGPU')) {
    throw new Error(`未显示回落原因，提示为：${hint}`);
  }
  console.log('    ✓ WebGPU 不可用 → 回落到自动，显示原因');
} else {
  if (afterWebGPU !== 'WebGPU') {
    throw new Error(`WebGPU 可用时应使用 WebGPU，实际 ${afterWebGPU}`);
  }
  console.log('    ✓ WebGPU 可用 → 生效');
}

console.log('\n=== 后端手动切换验收通过 ===');
try {
  ws.close();
} catch {
  /* 忽略 */
}
try {
  process.kill(child.pid);
} catch {
  /* 忽略 */
}
cleanupProfile();
