/**
 * 抓一张「导入进行中」的界面截图（宽屏两栏 + 进度条可见），作为 UI 证据图。
 *
 * 用法：
 *   node scripts/capture-ui-shot.mjs --url http://127.0.0.1:5173/ \
 *        --file <待导入的文件> --out <输出 png> [--port 9360]
 *
 * 做的事：启动本机 Chrome/Edge（1440x900）→ 打开页面 → 等模型加载完 →
 * 通过文件输入框导入指定文件 → 点击「向量化并导入」→
 * 在进度条出现的那一刻截图。
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const EDGE_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
];

const argv = process.argv.slice(2);
const arg = (n, d = null) => {
  const i = argv.indexOf(`--${n}`);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : d;
};
const URL_ = arg('url', 'http://127.0.0.1:5173/');
const FILE = arg('file');
const OUT = arg('out', 'progress.png');
const PORT = arg('port', '9360');
const EDGE = EDGE_CANDIDATES.find((p) => fs.existsSync(p));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

if (!EDGE) {
  console.error('找不到 Chrome / Edge，可执行文件请自行确认');
  process.exit(1);
}

const profile = path.join(os.tmpdir(), `stepfun-progress-${PORT}`);
const child = spawn(
  EDGE,
  [
    '--headless=new',
    '--no-first-run',
    '--no-default-browser-check',
    '--window-size=1440,900',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    'about:blank',
  ],
  { detached: true, stdio: 'ignore' },
);
child.unref();

for (let i = 0; i < 60; i += 1) {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
    if (r.ok) break;
  } catch {
    /* 等 DevTools 就绪 */
  }
  await sleep(500);
}

const created = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' });
const target = await created.json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
let msgId = 0;
const pending = new Map();
ws.addEventListener('message', (ev) => {
  const d = JSON.parse(ev.data);
  if (d.id && pending.has(d.id)) {
    pending.get(d.id)(d);
    pending.delete(d.id);
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
const evaluate = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  return r.result?.result?.value;
};

await send('Page.enable');
await send('Runtime.enable');
await send('DOM.enable');
await send('Page.navigate', { url: URL_ });

for (let i = 0; i < 120; i += 1) {
  const b = await evaluate("(document.querySelector('.badge-value')||{}).textContent");
  if (b && b !== '加载中') break;
  await sleep(1000);
}
console.log('后端:', await evaluate("(document.querySelector('.badge-value')||{}).textContent"));

const doc = await send('DOM.getDocument', { depth: -1, pierce: true });
const q = await send('DOM.querySelector', { nodeId: doc.result.root.nodeId, selector: '#file' });
await send('DOM.setFileInputFiles', { nodeId: q.result.nodeId, files: [FILE] });
await sleep(800);
console.log('解析:', await evaluate("(document.querySelector('.tag-count')||{}).textContent"));

await evaluate(
  "(() => { const b=[...document.querySelectorAll('button')].find(x=>x.textContent.includes('向量化并导入')); b.click(); return 'ok'; })()",
);

let shotTaken = false;
for (let i = 0; i < 200; i += 1) {
  const p = await evaluate(
    "(() => { const el=document.querySelector('.progress'); return el ? el.innerText : ''; })()",
  );
  if (p) {
    const r = await send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(OUT, Buffer.from(r.result.data, 'base64'));
    console.log('进度条内容:\n' + p);
    console.log('截图已保存:', OUT);
    shotTaken = true;
    break;
  }
  await sleep(60);
}
if (!shotTaken) console.log('没抓到进度条（可能导入太快）');

ws.close();
try {
  process.kill(child.pid);
} catch {
  /* 已退出 */
}
process.exit(0);
