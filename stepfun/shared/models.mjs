/**
 * 端侧 embedding 模型注册表（服务端与前端共用的唯一数据源）。
 *
 * 所有模型均为 HuggingFace 上公开可获取的开源模型，浏览器端通过
 * Transformers.js (@huggingface/transformers) 加载 ONNX 权重，在用户设备上计算向量。
 *
 * 量化变体与 Transformers.js 的 dtype -> 文件后缀映射一一对应：
 *   fp32 -> onnx/model.onnx
 *   fp16 -> onnx/model_fp16.onnx
 *   q8   -> onnx/model_quantized.onnx   (ORT 动态量化 uint8/INT8)
 *   int8 -> onnx/model_int8.onnx
 */

export const MODELS = {
  "Xenova/bge-small-zh-v1.5": {
    id: "Xenova/bge-small-zh-v1.5",
    label: "bge-small-zh-v1.5（中文，512 维）",
    dimension: 512,
    /** bge 系列检索时需要在查询前拼接指令前缀 */
    queryPrefix: "为这个句子生成表示以用于检索相关文章：",
    source: "https://huggingface.co/Xenova/bge-small-zh-v1.5",
    license: "MIT",
  },
  "Xenova/bge-small-en-v1.5": {
    id: "Xenova/bge-small-en-v1.5",
    label: "bge-small-en-v1.5（英文，384 维）",
    dimension: 384,
    queryPrefix: "Represent this sentence for searching relevant passages: ",
    source: "https://huggingface.co/Xenova/bge-small-en-v1.5",
    license: "MIT",
  },
  "Xenova/all-MiniLM-L6-v2": {
    id: "Xenova/all-MiniLM-L6-v2",
    label: "all-MiniLM-L6-v2（轻量，384 维）",
    dimension: 384,
    queryPrefix: "",
    source: "https://huggingface.co/Xenova/all-MiniLM-L6-v2",
    license: "Apache-2.0",
  },
};

export const DEFAULT_MODEL = "Xenova/bge-small-zh-v1.5";

/** 各推理后端可用的精度方案 */
export const DTYPE_BY_DEVICE = {
  webgpu: ["fp16", "fp32", "q8"],
  wasm: ["q8", "fp32"],
};

/** 默认精度：WebGPU 优先 FP16（GPU 上更快、体积更小），WASM 优先 INT8/q8 */
export const DEFAULT_DTYPE_BY_DEVICE = {
  webgpu: "fp16",
  wasm: "q8",
};

export function listModels() {
  return Object.values(MODELS);
}

export function getModel(id) {
  return MODELS[id] ?? null;
}

/**
 * 集合名规则：同一模型共用一个知识库集合，切换模型即切换集合。
 * seekdb-js 的集合名只允许字母、数字与下划线，因此需要归一化。
 */
export function collectionNameFor(modelId) {
  const short = modelId.split("/").pop();
  const normalized = short.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return `kb_${normalized}`;
}
