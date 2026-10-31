/**
 * 预下载模型权重到本地镜像目录，供浏览器端 transformers.js 直接加载。
 *
 * 目的：
 *   1. 让整套流程**完全离线**可跑 —— 评审机上没有外网也能跑通「导入→检索」闭环；
 *   2. 规避浏览器系统代理导致的跨网失败（权重只是静态文件，不涉及任何推理调用）。
 *
 * 目录结构刻意与 huggingface 的 URL 形态保持一致：
 *   public/models/<model_id>/resolve/main/<file>
 * 前端把 env.remoteHost 指向 http://<origin>/models/ 即可命中本地副本。
 *
 * 用法：
 *   npm run prepare:model                      # 默认从 https://hf-mirror.com/ 拉
 *   HF_ENDPOINT=https://huggingface.co npm run prepare:model
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');

const MODEL_ID = process.env.STEPFUN_MODEL_ID || 'Xenova/bge-small-zh-v1.5';
const ENDPOINT = (process.env.HF_ENDPOINT || 'https://hf-mirror.com/').replace(/\/?$/, '/');

/**
 * 需要下载的文件。
 * dtype=q8 对应 transformers.js 的 onnx/model_quantized.onnx。
 *
 * 默认只下 q8（应用运行时需要的那个）；
 * 跑量化对照基准时用 `--dtypes=q8,fp32,fp16` 把对照档位也拉下来。
 */
const DTYPE_FILES = {
  fp32: { path: 'onnx/model.onnx', required: true },
  fp16: { path: 'onnx/model_fp16.onnx', required: true },
  q8: { path: 'onnx/model_quantized.onnx', required: true },
  int8: { path: 'onnx/model_int8.onnx', required: false },
  uint8: { path: 'onnx/model_uint8.onnx', required: false },
};

const dtypeArg = process.argv.find((a) => a.startsWith('--dtypes='));
const DTYPES = (dtypeArg ? dtypeArg.slice('--dtypes='.length) : 'q8')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);

const FILES = [
  { path: 'config.json', required: true },
  { path: 'tokenizer.json', required: true },
  { path: 'tokenizer_config.json', required: false },
  { path: 'special_tokens_map.json', required: false },
  ...DTYPES.map((d) => {
    const spec = DTYPE_FILES[d];
    if (!spec) throw new Error(`未知 dtype：${d}`);
    return { path: spec.path, required: spec.required };
  }),
];

/**
 * 镜像目录放在 stepfun/models/（而不是 web/public/）。
 * 原因：模型文件由薄代理以静态资源方式提供，缺失文件必须返回 404；
 * 若放在前端静态目录里，Vite 的 SPA fallback 会把缺失文件回成 index.html，
 * transformers.js 会拿到 HTML 再当 JSON 解析，直接崩掉。
 */
const outRoot = path.resolve(root, '..', 'models', MODEL_ID, 'resolve', 'main');
fs.mkdirSync(outRoot, { recursive: true });

console.log(`[prepare:model] 模型：${MODEL_ID}`);
console.log(`[prepare:model] 权重源：${ENDPOINT}`);

let ok = 0;
let skipped = 0;

for (const file of FILES) {
  const url = `${ENDPOINT}${MODEL_ID}/resolve/main/${file.path}`;
  const dest = path.join(outRoot, file.path);
  fs.mkdirSync(path.dirname(dest), { recursive: true });

  if (fs.existsSync(dest) && fs.statSync(dest).size > 0) {
    console.log(`[prepare:model] 已存在，跳过：${file.path}`);
    skipped += 1;
    continue;
  }

  const res = await fetch(url, { redirect: 'follow' });
  if (!res.ok) {
    if (file.required) {
      console.error(`[prepare:model] 下载失败 ${file.path}：HTTP ${res.status}`);
      process.exit(1);
    }
    console.warn(`[prepare:model] 跳过可选文件 ${file.path}（HTTP ${res.status}）`);
    skipped += 1;
    continue;
  }

  const buf = Buffer.from(await res.arrayBuffer());
  fs.writeFileSync(dest, buf);
  console.log(`[prepare:model] 已下载 ${file.path}（${(buf.length / 1024 / 1024).toFixed(2)} MB）`);
  ok += 1;
}

console.log(`[prepare:model] 完成：新下载 ${ok} 个，跳过 ${skipped} 个 → ${outRoot}`);
