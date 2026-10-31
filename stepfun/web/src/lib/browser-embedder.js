/**
 * 浏览器内本地向量化（生产路径）。
 *
 * 这是赛题「文档和查询的 embedding 全部在浏览器内计算」的**唯一实现**。
 * 整个过程在浏览器里跑，不访问任何云端推理 API。
 *
 * 后端策略（三层优先级，见 backend-pref.js）：
 *   1. URL 参数 `?backend=webgpu|wasm` —— 显式覆盖（测试脚本用）
 *   2. localStorage —— 用户在右上角下拉框手动选的选择，刷新后保持
 *   3. 默认「自动」：先**主动探测** WebGPU 能力（navigator.gpu.requestAdapter()），
 *      有适配器就用 `device: 'webgpu'`，没有就用 `device: 'wasm'`
 *
 * 另外：若选了 webgpu 但建会话仍失败，自动切到 WASM 重新加载一次。
 *
 * 为什么不写成「先 webgpu，catch 到异常再试 wasm」：
 *   transformers.js 内部用 `webInitChain = webInitChain.then(load)` 串行化建会话，
 *   一旦首次建会话失败，这条链就变成 rejected 状态，后续任何一次
 *   `pipeline(..., {device:'wasm'})` 都会**复用同一个错误**，根本不会真的降级。
 *   所以降级必须在**调用 pipeline 之前**决策。
 *   （实测：Edge `--disable-gpu` 下，`?backend=wasm` 直接加载成功 1.5s，
 *     而「先 webgpu 再降级」会一直报 webgpu 的原始错误。）
 *
 * 另外 Transformers.js **不会自动降级**，device 必须显式指定。
 */

import { pipeline } from '@huggingface/transformers';
import {
  MODEL_ID as MODEL_IDENT,
  PROXY_BASE,
  currentRemoteHost,
  resolveRemoteHost as resolveHost,
} from './runtime-env.js';
import {
  BACKEND_PREF,
  clearBlockedReason,
  clearUrlPref,
  readBlockedReason,
  readStoredPref,
  resolvePref,
  setBlockedReason,
  writeStoredPref,
} from './backend-pref.js';

/** 模型标识与量化档位。 */
export const MODEL_ID = MODEL_IDENT;
export const MODEL_DTYPE = 'q8';
export const MODEL_DIM = 512;

export const BACKEND = {
  WEBGPU: 'webgpu',
  WASM: 'wasm',
};

/** 当前后端状态（供界面徽标使用）。 */
export const backendState = {
  /** 实际生效的后端 */
  device: null,
  /** 用户选择的偏好：auto / webgpu / wasm */
  pref: BACKEND_PREF.AUTO,
  /** 偏好来源：url / storage / default */
  prefSource: 'default',
  /** 尝试过 WebGPU 但失败的原因 */
  webgpuError: null,
  /** 需要展示给用户的一次性提示（如「手动选 WebGPU 不可用，已回落到自动」） */
  notice: null,
  /** WebGPU 能力探测结果 {available, reason} */
  webgpuProbe: null,
  /** 模型加载耗时 */
  loadMs: 0,
  /** 模型标识 */
  model: MODEL_ID,
  dtype: MODEL_DTYPE,
  dimension: MODEL_DIM,
  remoteHost: currentRemoteHost(),
  /** 权重实际来源 */
  weightSource: '未加载',
};

/**
 * 解析权重来源：本地离线镜像优先，探测失败再退回 hf-mirror。
 * 具体逻辑在 runtime-env.js（生产与基准测试共用同一份配置）。
 */
export async function resolveRemoteHost() {
  const host = await resolveHost();
  backendState.remoteHost = host;
  backendState.weightSource = host.startsWith(PROXY_BASE) ? '本地离线镜像' : host;
  return host;
}

/** 模型与后端的静态信息，供界面展示。 */
export function modelInfo() {
  return {
    model: MODEL_ID,
    dtype: MODEL_DTYPE,
    dimension: MODEL_DIM,
    remoteHost: backendState.remoteHost,
    weightSource: backendState.weightSource,
  };
}

/** 探测浏览器是否具备 WebGPU 能力（不保证模型能在上面跑通）。 */
export function detectWebGPU() {
  try {
    return typeof navigator !== 'undefined' && 'gpu' in navigator && !!navigator.gpu;
  } catch {
    return false;
  }
}

/**
 * 真正探测 WebGPU 是否**可用**：必须能拿到 GPU 适配器。
 *
 * `navigator.gpu` 存在 ≠ WebGPU 可用：
 *   - Chrome/Edge 启动参数 `--disable-gpu` 时 API 还在，但 requestAdapter() 返回 null；
 *   - 老显卡 / 虚拟机 / 远程桌面同样可能拿不到适配器。
 * 只有 requestAdapter() 成功，才值得用 webgpu 后端。
 *
 * @returns {Promise<{available: boolean, reason: string}>}
 */
export async function probeWebGPU() {
  if (!detectWebGPU()) {
    return { available: false, reason: '当前浏览器未提供 navigator.gpu' };
  }
  try {
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) {
      return {
        available: false,
        reason: 'navigator.gpu.requestAdapter() 返回空：没有可用的 GPU 适配器',
      };
    }
    return { available: true, reason: '' };
  } catch (err) {
    return { available: false, reason: `GPU 适配器申请失败：${err?.message || err}` };
  }
}

let extractorPromise = null;

/**
 * 决定本次加载用哪个后端。
 *
 * 优先级（从高到低）：
 *   1. URL 参数 `?backend=webgpu|wasm` —— 显式的一次性覆盖（测试脚本用）
 *   2. localStorage —— 用户在界面上手动选的选择，刷新后保持
 *   3. 默认「自动」—— 探测 WebGPU，可用就用 WebGPU，否则 WASM
 *
 * 特殊处理：
 *   - 用户选了 WebGPU 但探测不到适配器 → **回落到自动**并提示原因
 *     （此时自动也会得到 WASM，因为自动同样探测不到；但偏好会退回 auto，
 *      用户下次刷新不会被一个用不了的选择卡住）
 *   - 上一次 WebGPU 建会话失败 → 本次会话禁用 WebGPU，直接用 WASM
 *
 * @returns {Promise<{device: string, notice: string|null}>}
 */
async function decideDevice() {
  const { pref, source } = resolvePref();
  backendState.pref = pref;
  backendState.prefSource = source;

  // 1) 本次会话已被标记为 WebGPU 不可用（上次建会话失败）
  const blocked = readBlockedReason();
  if (blocked) {
    backendState.webgpuError = `WebGPU 建会话失败，本次会话已禁用：${blocked}`;
    console.warn('[embedder] 本次会话禁用 WebGPU，使用 WASM：', blocked);
    return { device: BACKEND.WASM, notice: backendState.webgpuError };
  }

  // 2) 用户显式选了 WASM —— 不探测，直接用
  if (pref === BACKEND_PREF.WASM) {
    console.info('[embedder] 用户选择 WASM 后端（localStorage）');
    return { device: BACKEND.WASM, notice: null };
  }

  // 3) 用户显式选了 WebGPU —— 先探测，可用就用
  if (pref === BACKEND_PREF.WEBGPU) {
    const probe = await probeWebGPU();
    backendState.webgpuProbe = probe;
    if (probe.available) {
      console.info('[embedder] 用户选择 WebGPU 后端（localStorage）');
      return { device: BACKEND.WEBGPU, notice: null };
    }
    // 选了 WebGPU 但不可用 → 回落到自动（并把偏好退回 auto）
    writeStoredPref(BACKEND_PREF.AUTO);
    backendState.pref = BACKEND_PREF.AUTO;
    backendState.prefSource = 'fallback';
    const notice = `手动选择的 WebGPU 不可用，已回落到「自动」：${probe.reason}`;
    backendState.webgpuError = probe.reason;
    console.warn('[embedder]', notice);
    return { device: BACKEND.WASM, notice };
  }

  // 4) 自动：探测 WebGPU，可用就用 WebGPU，否则 WASM
  const probe = await probeWebGPU();
  backendState.webgpuProbe = probe;
  if (probe.available) {
    return { device: BACKEND.WEBGPU, notice: null };
  }
  backendState.webgpuError = probe.reason;
  console.warn('[embedder] WebGPU 不可用，直接使用 WASM 后端：', probe.reason);
  return { device: BACKEND.WASM, notice: null };
}

/**
 * 加载模型：先决定后端，再**只调用一次** pipeline()。
 *
 * @returns {Promise<object>} transformers.js 的 feature-extraction pipeline
 */
async function tryLoadPipeline() {
  // 先确定权重来源（本地离线镜像优先），再建会话
  await resolveRemoteHost();

  const { device, notice } = await decideDevice();
  if (notice) backendState.notice = notice;

  const t0 = performance.now();
  try {
    const extractor = await pipeline('feature-extraction', MODEL_ID, {
      dtype: MODEL_DTYPE,
      device,
    });
    backendState.device = device;
    backendState.loadMs = Math.round(performance.now() - t0);
    return extractor;
  } catch (err) {
    const message = err?.message || String(err);
    if (device === BACKEND.WEBGPU) {
      // transformers.js 的建会话链在首次失败后会被「污染」，
      // 同一个页面里再试 wasm 会复用同一个错误。
      // 所以这里改成：记录原因 → 标记本次会话禁用 WebGPU → 重新加载页面。
      backendState.webgpuError = message;
      console.warn('[embedder] WebGPU 建会话失败，切换到 WASM 后端重新加载：', message);
      try {
        setBlockedReason(message);
        location.reload();
      } catch {
        /* 忽略：直接抛错让界面显示 */
      }
      // 页面即将刷新，这里挂起即可
      return new Promise(() => {});
    }
    throw err;
  }
}

/**
 * 加载模型（幂等，重复调用复用同一个实例）。
 * @param {(stage: string, detail?: object) => void} onProgress
 */
export async function loadModel(onProgress = () => {}) {
  if (!extractorPromise) {
    onProgress('正在加载模型', { model: MODEL_ID, dtype: MODEL_DTYPE, device: 'webgpu → wasm' });
    extractorPromise = tryLoadPipeline().catch((err) => {
      // 失败后清空缓存，允许用户重试
      extractorPromise = null;
      throw err;
    });
  }
  return extractorPromise;
}

/**
 * 把一批文本转成归一化向量。
 * @param {string[]} texts
 * @param {(done:number,total:number) => void} [onProgress]
 * @returns {Promise<number[][]>}
 */
export async function embedTexts(texts, onProgress = () => {}) {
  if (!texts.length) return [];
  const extractor = await loadModel();
  onProgress(0, texts.length);
  const out = await extractor(texts, { pooling: 'mean', normalize: true });
  const vectors = out.tolist();
  onProgress(texts.length, texts.length);
  return vectors;
}

/** 把单条文本转成归一化向量。 */
export async function embedText(text) {
  const [vec] = await embedTexts([text]);
  return vec;
}

/**
 * 便捷函数：把向量数组转成 seekdb 接受的字面量。 */
export function toVectorLiteral(vector) {
  if (typeof vector === 'string') return vector;
  return `[${vector.map((v) => Number(v).toFixed(6)).join(',')}]`;
}

/* ------------------------------------------------------------------ *
 * 手动切换后端（界面右上角下拉框）
 * ------------------------------------------------------------------ */

/** 当前用户选择的后端偏好（auto / webgpu / wasm）。 */
export function currentPref() {
  return readStoredPref();
}

/**
 * 切换后端偏好。
 *
 * 之所以要**重新加载页面**而不是原地重建会话：
 *   transformers.js 把建会话串成 `webInitChain = webInitChain.then(load)`，
 *   首次失败后这条链变成 rejected，原地再试任何 device 都会复用同一个错误。
 *   换 device 等于换一条全新的会话链，最可靠的做法就是刷新页面。
 *
 * @param {'auto'|'webgpu'|'wasm'} pref
 */
export function setBackendPref(pref) {
  writeStoredPref(pref);
  // 用户在界面上做的选择，优先级不应该再被 URL 上的 ?backend= 覆盖
  clearUrlPref();
  // 手动选 WebGPU 时，清掉「本次会话禁用 WebGPU」的标记，让它能真正重试一次
  if (pref === BACKEND_PREF.WEBGPU) clearBlockedReason();
  location.reload();
}

// 开发模式下把切换函数暴露到 window，便于自动化脚本直接驱动（生产构建不暴露）
if (import.meta.env?.DEV && typeof window !== 'undefined') {
  window.__stepfunSetBackendPref = setBackendPref;
  window.__stepfunBackendPref = currentPref;
}
