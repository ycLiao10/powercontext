/**
 * 把 onnxruntime-web 的 wasm / mjs 运行时文件复制到 web/public/ort/，
 * 使浏览器端推理完全走本机资源，不依赖任何 CDN。
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const target = path.join(root, "web", "public", "ort");

const candidates = [];
try {
  candidates.push(path.dirname(require.resolve("onnxruntime-web/package.json")));
} catch {
  /* 未安装时回退到 node_modules 目录探测 */
}
candidates.push(path.join(root, "node_modules", "onnxruntime-web", "dist"));

let src = null;
for (const c of candidates) {
  if (fs.existsSync(c) && fs.readdirSync(c).some((f) => f.endsWith(".wasm"))) {
    src = c;
    break;
  }
}

if (!src) {
  console.error("[copy-ort] 未找到 onnxruntime-web 的 wasm 文件，请先 npm install");
  process.exit(1);
}

fs.mkdirSync(target, { recursive: true });
const files = fs.readdirSync(src).filter((f) => f.endsWith(".wasm") || f.endsWith(".mjs"));
for (const f of files) {
  fs.copyFileSync(path.join(src, f), path.join(target, f));
}
console.log(`[copy-ort] 已复制 ${files.length} 个运行时文件 -> ${target}`);
for (const f of files) console.log(`  - ${f}`);
