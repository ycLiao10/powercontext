/**
 * 把 onnxruntime-web 的 wasm 运行时资源复制到 stepfun/runtime/ort/。
 *
 * 为什么需要这一步：
 *   - transformers.js 默认让 onnxruntime-web 从 jsdelivr CDN 拉 wasm/胶水文件；
 *   - 本作品要求离线可跑、且不引入外部 CDN 依赖，所以在启动前把资源复制到本地，
 *     并让 browser-embedder.js 把 `env.backends.onnx.wasm.wasmPaths` 指向
 *     `<proxy>/ort/`（由薄代理以静态文件方式提供）。
 *
 * 为什么不由前端 dev server 直接提供：
 *   onnxruntime-web 运行时是用**动态 import()** 加载 .mjs 胶水文件的，
 *   而 Vite 明确禁止从 publicDir 里 import 源文件（会报 "This file is in /public
 *   ... should not be imported from source code"）。放在代理后面就没有这个限制。
 *
 * 用法：npm run prepare:ort（已挂在 predev / prebuild 上）
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const srcDir = path.join(root, 'node_modules', 'onnxruntime-web', 'dist');
const outDir = path.resolve(root, '..', 'runtime', 'ort');

if (!fs.existsSync(srcDir)) {
  console.error('[prepare:ort] 找不到 onnxruntime-web/dist，请先执行 npm install');
  process.exit(1);
}

fs.mkdirSync(outDir, { recursive: true });

// 覆盖 webgpu / wasm / jspi / asyncify 四种构建的胶水与二进制，
// 具体用到哪一套由 onnxruntime-web 在运行时决定。
const files = fs
  .readdirSync(srcDir)
  .filter((f) => f.startsWith('ort-wasm-simd-threaded') && (f.endsWith('.mjs') || f.endsWith('.wasm')));

let bytes = 0;
for (const f of files) {
  const from = path.join(srcDir, f);
  const to = path.join(outDir, f);
  fs.copyFileSync(from, to);
  bytes += fs.statSync(to).size;
}

console.log(
  `[prepare:ort] 已复制 ${files.length} 个 onnxruntime-web 运行时文件到 runtime/ort/（${(
    bytes /
    1024 /
    1024
  ).toFixed(1)} MB）`,
);
