/**
 * 推理后端偏好（手动切换）。
 *
 * 为什么需要手动切换：
 *   实测（Intel Iris Xe 核显，bge-small-zh-v1.5，q8 量化）显示
 *   **WebGPU 并不总是更快**：q8 + WebGPU 端到端 123.3 ms，而 q8 + WASM 只有 60.9 ms。
 *   原因见 tech-notes.md「端侧运行」。但赛题 2.2 要求「优先使用 WebGPU」，
 *   所以默认仍是自动探测（等价于「优先 WebGPU，不可用才降级 WASM」），
 *   同时把选择权交给用户：右上角徽标可手动切到 WebGPU / WASM。
 *
 * 三层优先级（从高到低）：
 *   1. URL 参数 `?backend=webgpu|wasm` —— 显式的一次性覆盖，测试脚本用
 *   2. localStorage `stepfun:backend-pref` —— 用户在界面上选的选择，刷新后保持
 *   3. 默认 `auto` —— 探测 WebGPU，可用就用 WebGPU，否则 WASM
 *
 * 另外有一个**会话级**的 WebGPU 禁用标记（sessionStorage）：
 *   当「探测通过但建会话失败」时，transformers.js 的 webInitChain 已被污染，
 *   同一页面里再试 WASM 会复用同一个错误（见 browser-embedder.js 注释）。
 *   所以此时记录原因、禁用本次会话的 WebGPU 并刷新页面。
 *   它是会话级的（不是永久的），用户随时可以用下拉框切到 WASM 或 WebGPU 重新尝试。
 */

/** 后端偏好取值。 */
export const BACKEND_PREF = {
  AUTO: 'auto',
  WEBGPU: 'webgpu',
  WASM: 'wasm',
};

/** localStorage 键：用户手动选择的后端。 */
export const PREF_STORAGE_KEY = 'stepfun:backend-pref';

/** sessionStorage 键：本次会话内 WebGPU 建会话失败的标记。 */
export const WEBGPU_BLOCKED_KEY = 'stepfun:webgpu-blocked';

/** sessionStorage 键：WebGPU 不可用的原因（给界面提示用）。 */
export const WEBGPU_ERROR_KEY = 'stepfun:webgpu-error';

const isPref = (v) =>
  v === BACKEND_PREF.AUTO || v === BACKEND_PREF.WEBGPU || v === BACKEND_PREF.WASM;

/** 读取 localStorage 里的偏好（无值或非法值都当作 auto）。 */
export function readStoredPref() {
  try {
    const v = window.localStorage.getItem(PREF_STORAGE_KEY);
    return isPref(v) ? v : BACKEND_PREF.AUTO;
  } catch {
    return BACKEND_PREF.AUTO;
  }
}

/** 写入 localStorage 偏好。 */
export function writeStoredPref(pref) {
  try {
    if (isPref(pref)) window.localStorage.setItem(PREF_STORAGE_KEY, pref);
    else window.localStorage.removeItem(PREF_STORAGE_KEY);
  } catch {
    /* 隐私模式下 localStorage 可能不可用，忽略 */
  }
}

/** 读取 URL 参数里的显式覆盖（?backend=webgpu|wasm）。 */
export function readUrlPref() {
  try {
    const v = new URLSearchParams(window.location.search).get('backend');
    return isPref(v) ? v : null;
  } catch {
    return null;
  }
}

/** 去掉 URL 上的 ?backend 参数（用户在界面上改过选择后调用，避免 URL 覆盖用户选择）。 */
export function clearUrlPref() {
  try {
    const url = new URL(window.location.href);
    if (!url.searchParams.has('backend')) return;
    url.searchParams.delete('backend');
    window.history.replaceState({}, '', url.toString());
  } catch {
    /* 忽略 */
  }
}

/** 读取本次会话 WebGPU 被禁用的原因（null = 未禁用）。 */
export function readBlockedReason() {
  try {
    return window.sessionStorage.getItem(WEBGPU_BLOCKED_KEY);
  } catch {
    return null;
  }
}

/** 标记本次会话禁用 WebGPU，并记录原因。 */
export function setBlockedReason(reason) {
  try {
    window.sessionStorage.setItem(WEBGPU_BLOCKED_KEY, String(reason ?? '未知原因'));
    window.sessionStorage.setItem(WEBGPU_ERROR_KEY, String(reason ?? '未知原因'));
  } catch {
    /* 忽略 */
  }
}

/** 清除会话级禁用标记（用户手动重新选择 WebGPU 时调用）。 */
export function clearBlockedReason() {
  try {
    window.sessionStorage.removeItem(WEBGPU_BLOCKED_KEY);
    window.sessionStorage.removeItem(WEBGPU_ERROR_KEY);
  } catch {
    /* 忽略 */
  }
}

/**
 * 解析最终生效的偏好。
 * @returns {{pref: string, source: 'url'|'storage'|'default'}}
 */
export function resolvePref() {
  const url = readUrlPref();
  if (url) return { pref: url, source: 'url' };
  const stored = readStoredPref();
  if (stored !== BACKEND_PREF.AUTO) return { pref: stored, source: 'storage' };
  return { pref: BACKEND_PREF.AUTO, source: 'default' };
}
