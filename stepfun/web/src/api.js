/** 与本地桥接服务通信的薄封装 */
const json = async (res) => {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body?.error ?? `HTTP ${res.status}`);
  return body;
};

const post = (url, payload) =>
  fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  }).then(json);

export const health = () => fetch("/api/health").then(json);
export const listModels = () => fetch("/api/models").then(json);
export const listCollections = () => fetch("/api/collections").then(json);
export const collectionStats = (name) =>
  fetch(`/api/collections/${encodeURIComponent(name)}/stats`).then(json);
export const createCollection = (payload) => post("/api/collections", payload);
export const deleteCollection = (name) =>
  fetch(`/api/collections/${encodeURIComponent(name)}`, { method: "DELETE" }).then(json);
export const addDocuments = (name, documents) =>
  post(`/api/collections/${encodeURIComponent(name)}/documents`, { documents });
export const refresh = (name) => post(`/api/collections/${encodeURIComponent(name)}/refresh`, {});
export const search = (name, payload) =>
  post(`/api/collections/${encodeURIComponent(name)}/search`, payload);
export const explain = (name, payload) =>
  post(`/api/collections/${encodeURIComponent(name)}/explain`, payload);
