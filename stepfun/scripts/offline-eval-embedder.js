/**
 * 【离线评测专用】本地向量化工具 —— 只服务于 `scripts/` 下的离线数据准备与质量评测，
 * **不属于生产检索链路**，生产链路的向量全部在浏览器内计算。
 *
 * ── 用途边界（重要）─────────────────────────────────────────────────────
 *   本模块唯一的调用方是 scripts/ 下的开发期工具：
 *     - import-corpus.js      往固定评测集灌入语料
 *     - search.js --eval      离线算 Recall@5 / nDCG@5
 *     - gen-sql-log-samples.js 生成 SQL 日志样例
 *
 *   赛题要求「文档和查询的 embedding 全部在浏览器内计算，禁止任何云端推理 API」
 *   以及「禁止服务端预计算向量或检索结果再回放」。本模块的定位是
 *   **离线基线与评测工具**，用于在开发机上复现同一模型的量化前后质量差异：
 *
 *     ✓ 调用的是同一个公开开源模型（BAAI/bge-small-zh-v1.5）的**本地 ONNX 权重**，
 *       完全离线，**不访问任何云端推理 API**；
 *     ✓ 不缓存、不回放检索结果——检索每次都由 seekdb 现场执行 SQL 计算；
 *     ✓ 不参与生产请求路径：server/proxy.js 里没有任何推理代码，
 *       代理只转发浏览器提交过来的向量。
 *
 *   若不想在 Node 侧跑模型，可以：
 *     - 用 `node scripts/import-corpus.js --vectors <file>` 导入浏览器导出的向量；
 *     - 或直接在浏览器里导入（前端算好向量再提交给代理）。
 *
 * ── 生产路径（对照说明）─────────────────────────────────────────────────
 *   web/ 前端里的 src/lib/browser-embedder.js 才是生产用的浏览器内推理实现，
 *   它与本模块的差别只有执行位置（浏览器 vs Node）与后端（WebGPU/WASM vs CPU EP）。
 */

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { STEPFUN_ROOT, MODEL_ID } from '../server/config.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * 国内网络环境下 huggingface.co 不可达时，可通过 HF_ENDPOINT / STEPFUN_HF_ENDPOINT
 * 指向 hf-mirror.com。默认跟随环境变量，不写死任何镜像。
 */
function resolveRemoteHost() {
  const explicit = process.env.STEPFUN_HF_ENDPOINT || process.env.HF_ENDPOINT;
  if (explicit) return explicit.endsWith('/') ? explicit : `${explicit}/`;
  return undefined;
}

let extractorPromise = null;

/**
 * 加载本地 ONNX 向量模型（离线评测专用）。
 * @param {{dtype?: string, device?: string}} options
 */
export async function getExtractor({ dtype = 'q8', device = 'cpu' } = {}) {
  if (!extractorPromise) {
    extractorPromise = (async () => {
      const { pipeline, env } = await import('@huggingface/transformers');
      const remoteHost = resolveRemoteHost();
      if (remoteHost) env.remoteHost = remoteHost;
      // 只用本地权重做推理，不连任何云端推理 API
      env.allowLocalModels = false;
      return pipeline('feature-extraction', MODEL_ID, { dtype, device });
    })();
  }
  return extractorPromise;
}

/**
 * 计算一批文本的向量（已 mean pooling + L2 归一化）。
 * @param {string[]} texts
 * @param {{dtype?: string, device?: string}} options
 * @returns {Promise<number[][]>}
 */
export async function embedTexts(texts, options = {}) {
  const extractor = await getExtractor(options);
  const output = await extractor(texts, { pooling: 'mean', normalize: true });
  return output.tolist();
}

/** 计算单条文本的向量。 */
export async function embedText(text, options = {}) {
  const [vec] = await embedTexts([text], options);
  return vec;
}

/** 模型元信息（写入 evidence 用）。 */
export function modelInfo(dtype = 'q8') {
  return {
    id: MODEL_ID,
    baseModel: 'BAAI/bge-small-zh-v1.5',
    license: 'MIT',
    dimension: 512,
    dtype,
    pooling: 'mean',
    normalize: true,
    localWeightsOnly: true,
    role: 'offline-eval-only',
  };
}

/** evidence 输出目录。 */
export const EVIDENCE_DIR = path.join(STEPFUN_ROOT, 'evidence');
