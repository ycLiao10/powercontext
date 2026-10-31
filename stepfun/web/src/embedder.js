/**
 * 端侧推理封装（Transformers.js / ONNX Runtime Web）。
 *
 * 关键约束：
 *  1. 只允许本地权重：env.allowRemoteModels = false，权重由本地 /models 目录提供；
 *  2. 显式指定推理后端：优先 WebGPU，不可用时自动降级到 WASM，并对外暴露实际使用的后端；
 *  3. 文档向量与查询向量都在浏览器里算，服务端不参与 embedding。
 */
import { pipeline, env } from "@huggingface/transformers";

// ---- 完全本地化：禁止任何远程模型/远程推理 ----
env.allowRemoteModels = false;
env.allowLocalModels = true;
// 注意：这里必须用「相对路径」而不是绝对 URL。
// Transformers.js v4 在探测本地文件是否存在时，会跳过绝对 URL 的本地路径
// （见 get_file_metadata：isURL 时直接走远端分支），
// 一旦探测失败，tokenizer 文件列表就会为空，pipeline 会缺少 tokenizer。
// 相对路径会被浏览器按当前站点 origin 解析，请求仍然只落到本地服务。
env.localModelPath = "/models/";
if (env.backends?.onnx?.wasm) {
  env.backends.onnx.wasm.wasmPaths = "/ort/";
  env.backends.onnx.wasm.proxy = false;
  // 只有开启跨源隔离（COOP/COEP）后浏览器才允许 SharedArrayBuffer，
  // 多线程才有意义；否则保持单线程，避免 ORT 反复告警。
  const isolated = typeof crossOriginIsolated !== "undefined" && crossOriginIsolated;
  env.backends.onnx.wasm.numThreads = isolated
    ? Math.max(1, Math.min(4, navigator.hardwareConcurrency || 2))
    : 1;
}

/** 探测 WebGPU 是否真的可用（存在 adapter 才算可用） */
export async function detectWebGPU() {
  if (typeof navigator === "undefined" || !("gpu" in navigator)) {
    return { available: false, reason: "浏览器未提供 navigator.gpu" };
  }
  try {
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) {
      return { available: false, reason: "WebGPU adapter 为空（通常是无受支持 GPU 或被禁用）" };
    }
    let info = null;
    try {
      info = adapter.info ?? (await adapter.requestAdapterInfo?.()) ?? null;
    } catch {
      info = null;
    }
    return {
      available: true,
      adapter: info
        ? {
            vendor: info.vendor ?? "",
            architecture: info.architecture ?? "",
            device: info.device ?? "",
            description: info.description ?? "",
          }
        : null,
    };
  } catch (err) {
    return { available: false, reason: String(err?.message ?? err) };
  }
}

/** 加载模型管线；device 显式传入，失败由上层决定是否降级 */
export async function loadPipeline({ modelId, dtype, device, onProgress }) {
  return await pipeline("feature-extraction", modelId, {
    device,
    dtype,
    progress_callback: onProgress,
  });
}

/**
 * 批量向量化。
 * @param {Function} extractor pipeline 实例
 * @param {string[]} texts 待编码文本
 * @param {object} opts batchSize 批大小；onProgress 进度回调
 * @returns {Promise<number[][]>} L2 归一化后的向量
 */
export async function embedTexts(extractor, texts, { batchSize = 8, onProgress } = {}) {
  const out = [];
  for (let i = 0; i < texts.length; i += batchSize) {
    const chunk = texts.slice(i, i + batchSize);
    const tensor = await extractor(chunk, { pooling: "mean", normalize: true });
    const data = tensor.data;
    const dim = tensor.dims[tensor.dims.length - 1];
    for (let j = 0; j < chunk.length; j++) {
      const vec = new Array(dim);
      for (let k = 0; k < dim; k++) vec[k] = data[j * dim + k];
      out.push(vec);
    }
    onProgress?.(Math.min(i + batchSize, texts.length), texts.length);
    // 让出主线程，保证长任务进行中 UI 仍可响应
    await new Promise((r) => setTimeout(r, 0));
  }
  return out;
}

/** 单条文本向量化（查询用） */
export async function embedOne(extractor, text) {
  const [vec] = await embedTexts(extractor, [text], { batchSize: 1 });
  return vec;
}

/** 把大整数向量转成可安全 JSON 序列化的普通数组 */
export const toPlainVector = (vec) => Array.from(vec, (v) => Number(v));
