/**
 * 与本地薄 HTTP 代理通信。
 *
 * 代理地址默认 http://127.0.0.1:8787，可用 VITE_PROXY_URL 覆盖。
 * 浏览器不能直连 MySQL 协议端口，所以必须走这一层。
 */

const PROXY_BASE = (import.meta.env.VITE_PROXY_URL || 'http://127.0.0.1:8787').replace(/\/$/, '');

async function request(path, options = {}) {
  const res = await fetch(`${PROXY_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await res.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`代理返回了非 JSON 内容（HTTP ${res.status}）：${text.slice(0, 200)}`);
  }
  if (!res.ok || payload.ok === false) {
    throw new Error(payload.error || `请求失败（HTTP ${res.status}）`);
  }
  return payload;
}

/** 探活 + 当前知识库状态。 */
export function health() {
  return request('/api/health');
}

/** 建库建表（幂等）。 */
export function ensureSchema() {
  return request('/api/schema', { method: 'POST' });
}

/**
 * 写入文档。向量必须由浏览器算好后随请求提交。
 * @param {Array<{id:string,title:string,content:string,embedding:number[],metadata?:object}>} documents
 */
export function writeDocuments(documents) {
  return request('/api/documents', {
    method: 'POST',
    body: JSON.stringify({ documents }),
  });
}

/**
 * 检索。mode = 'hybrid' | 'vector' | 'fulltext'。
 * @param {{query:string, embedding?:number[], mode?:string, topK?:number}} params
 */
export function search({ query, embedding, mode = 'hybrid', topK = 5 }) {
  const body = { query, mode, topK };
  if (embedding) body.embedding = embedding;
  return request('/api/search', { method: 'POST', body: JSON.stringify(body) });
}

/** 清空知识库。 */
export function clearDocuments() {
  return request('/api/documents', { method: 'DELETE' });
}
