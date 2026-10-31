/**
 * 浏览器端运行时环境配置（生产路径与基准测试共用）。
 *
 * 抽出来是为了让 `browser-embedder.js`（生产）与 `bench.js`（基准）用**完全一致**的
 * 配置 —— 否则基准数据测出来的不是实际跑的那套参数。
 */

import { env } from '@huggingface/transformers';

/** 模型标识。 */
export const MODEL_ID = 'Xenova/bge-small-zh-v1.5';

/** 各量化档位对应的模型文件（transformers.js 的 DEFAULT_DTYPE_SUFFIX_MAPPING）。 */
export const DTYPE_SUFFIX = {
  fp32: '',
  fp16: '_fp16',
  q8: '_quantized',
  int8: '_int8',
  uint8: '_uint8',
  q4: '_q4',
  q4f16: '_q4f16',
};

/** 某个 dtype 对应的权重文件名。 */
export function modelFileName(dtype) {
  const suffix = DTYPE_SUFFIX[dtype];
  if (suffix === undefined) throw new Error(`未知 dtype：${dtype}`);
  return `onnx/model${suffix}.onnx`;
}

/** 薄代理地址。 */
export const PROXY_BASE = (import.meta.env.VITE_PROXY_URL || 'http://127.0.0.1:8787').replace(
  /\/$/,
  '',
);

const EXPLICIT_HOST = import.meta.env.VITE_HF_ENDPOINT || '';
const LOCAL_MIRROR = `${PROXY_BASE}/models/`;
const FALLBACK_HOST = 'https://hf-mirror.com/';

/**
 * onnxruntime-web 的 wasm 运行时地址。
 * 必须是**对象形式** {mjs, wasm}：transformers.js 只在对象形式下才会自己去下载
 * wasm 二进制、并把 .mjs 胶水转成 blob URL（见其 ensureWasmLoaded）。
 */
const ORT_BASE = (import.meta.env.VITE_ORT_WASM_PATH || `${PROXY_BASE}/ort/`).replace(/\/?$/, '/');
export const ORT_FILES = {
  mjs: `${ORT_BASE}ort-wasm-simd-threaded.asyncify.mjs`,
  wasm: `${ORT_BASE}ort-wasm-simd-threaded.asyncify.wasm`,
};

env.allowLocalModels = false;
env.backends.onnx.wasm.wasmPaths = { ...ORT_FILES };

let resolvedHost = null;

/**
 * 解析权重来源：本地离线镜像优先，探测失败再退回 hf-mirror。
 * 探测只请求一个很小的 config.json，并校验 Content-Type ——
 * 不能只看 HTTP 状态码，否则拿到 HTML 回退页会误判成「本地镜像可用」。
 */
export async function resolveRemoteHost() {
  if (resolvedHost) return resolvedHost;

  if (EXPLICIT_HOST) {
    resolvedHost = EXPLICIT_HOST;
  } else {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 4000);
      const res = await fetch(`${LOCAL_MIRROR}${MODEL_ID}/resolve/main/config.json`, {
        signal: controller.signal,
        cache: 'no-store',
      });
      clearTimeout(timer);
      const type = res.headers.get('content-type') || '';
      resolvedHost = res.ok && type.includes('json') ? LOCAL_MIRROR : FALLBACK_HOST;
    } catch {
      resolvedHost = FALLBACK_HOST;
    }
  }

  env.remoteHost = resolvedHost;
  return resolvedHost;
}

/** 当前生效的权重源。 */
export function currentRemoteHost() {
  return resolvedHost || LOCAL_MIRROR;
}
