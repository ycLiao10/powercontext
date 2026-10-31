/**
 * 端侧推理基准（页内测量）。
 *
 * 由 `scripts/bench-browser.mjs` 通过 CDP 驱动，页面暴露 `window.__benchRun()`：
 *   - 冷启动加载（含权重下载）、预热后加载
 *   - 36 条语料的向量化耗时与吞吐（可指定多个 batch size 对比）
 *   - 写入 seekdb（含索引刷新）耗时
 *   - 查询向量化耗时
 *   - seekdb 检索耗时（服务端 SQL 时间）
 *   - 端到端耗时（查询向量计算 → 检索 → 结果渲染）
 *
 * 注意：这里**不复用** browser-embedder.js 的模型缓存，
 * 因为基准需要分别测量「冷加载」与「预热后加载」。
 */

import { pipeline } from '@huggingface/transformers';
import { MODEL_ID, PROXY_BASE, resolveRemoteHost, currentRemoteHost } from './lib/runtime-env.js';

const params = new URLSearchParams(location.search);
const DTYPE = params.get('dtype') || 'q8';
const DEVICE = params.get('device') || 'wasm';
const BATCH = Number(params.get('batch') || 8);

const statusEl = document.getElementById('status');
const resultsEl = document.getElementById('results');

function setStatus(text) {
  statusEl.textContent = text;
}

/** 文档送入模型的文本：与生产路径（App.jsx）保持一致。 */
function docText(d) {
  return `${d.title}。${d.content}`;
}

async function createExtractor(device, dtype) {
  return pipeline('feature-extraction', MODEL_ID, { dtype, device });
}

/** 分批向量化，返回 {ms, vectors}。 */
async function embedBatched(extractor, texts, batch) {
  const vectors = [];
  const t0 = performance.now();
  for (let i = 0; i < texts.length; i += batch) {
    const chunk = texts.slice(i, i + batch);
    const out = await extractor(chunk, { pooling: 'mean', normalize: true });
    vectors.push(...out.tolist());
  }
  return { ms: performance.now() - t0, vectors };
}

async function postJson(path, body, method = 'POST') {
  const res = await fetch(`${PROXY_BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: method === 'DELETE' ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  const payload = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(payload.error || `HTTP ${res.status}`);
  return payload;
}

/** 端到端：查询向量计算 → 混合检索 → 结果渲染（DOM 真正更新后才算结束）。 */
async function measureEndToEnd(extractor, query, topK = 5) {
  const t0 = performance.now();
  const [vec] = await embedBatched(extractor, [query], 1).then((r) => r.vectors);
  const tEmbed = performance.now();
  const res = await postJson('/api/search', {
    query,
    embedding: vec,
    mode: 'hybrid',
    topK,
  });
  const tSearch = performance.now();
  resultsEl.innerHTML = '';
  for (const r of res.results) {
    const div = document.createElement('div');
    div.className = 'row';
    div.textContent = `#${r.id} ${Number(r.hybrid_score).toFixed(6)}`;
    resultsEl.appendChild(div);
  }
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const tEnd = performance.now();
  return {
    embedMs: tEmbed - t0,
    searchRoundTripMs: tSearch - tEmbed,
    serverSearchMs: res.tookMs,
    totalMs: tEnd - t0,
    count: res.count,
  };
}

window.__benchRun = async (payload) => {
  const { corpus, queries, reps = 5, batches = [1, 8, 16] } = payload;
  const out = {
    dtype: DTYPE,
    device: DEVICE,
    batch: BATCH,
    model: MODEL_ID,
    remoteHost: null,
    coldLoadMs: null,
    warmLoadMs: null,
    reps: [],
    error: null,
  };

  try {
    setStatus('resolve remote host');
    out.remoteHost = await resolveRemoteHost();

    // ---- 1) 冷启动加载（全新浏览器 profile 下含权重下载） ----
    setStatus('cold load');
    const t0 = performance.now();
    const extractor = await createExtractor(DEVICE, DTYPE);
    out.coldLoadMs = performance.now() - t0;

    // ---- 2) 预热后的加载（权重已进浏览器 Cache，重建会话） ----
    setStatus('warm load');
    const t1 = performance.now();
    const extractor2 = await createExtractor(DEVICE, DTYPE);
    out.warmLoadMs = performance.now() - t1;

    const corpusTexts = corpus.map(docText);
    const queryTexts = queries.map((q) => q.query);

    // ---- 3) 重复测量 ----
    for (let rep = 0; rep < reps; rep += 1) {
      setStatus(`rep ${rep + 1}/${reps}`);
      const record = { rep, batchRuns: {}, write: null, queries: [], endToEnd: null };

      // 3.1 不同 batch size 的向量化吞吐
      const vectorsByBatch = {};
      for (const b of batches) {
        const r = await embedBatched(extractor, corpusTexts, b);
        vectorsByBatch[b] = r.vectors;
        record.batchRuns[b] = {
          ms: r.ms,
          docsPerSec: (corpusTexts.length / r.ms) * 1000,
        };
      }

      // 3.2 用配置里的 batch 写入 seekdb（写入 + 索引刷新）
      const vectors = vectorsByBatch[BATCH] || vectorsByBatch[batches[0]];
      await postJson('/api/documents', null, 'DELETE');
      const tWrite = performance.now();
      const writeRes = await postJson('/api/documents', {
        documents: corpus.map((d, i) => ({
          id: d.id,
          title: d.title,
          content: d.content,
          embedding: vectors[i],
          metadata: d.metadata || { domain: d.domain, tags: d.tags },
        })),
      });
      record.write = {
        clientMs: performance.now() - tWrite,
        serverInsertMs: writeRes.insertMs,
        documents: writeRes.documents,
      };

      // 3.3 查询向量化 + 检索
      const qVecs = await embedBatched(extractor2, queryTexts, BATCH);
      for (let qi = 0; qi < queries.length; qi += 1) {
        const tq = performance.now();
        const res = await postJson('/api/search', {
          query: queries[qi].query,
          embedding: qVecs.vectors[qi],
          mode: 'hybrid',
          topK: 5,
        });
        record.queries.push({
          id: queries[qi].id,
          queryMs: tq - tq, // 占位，下面用 batch 总量拆分
          serverSearchMs: res.tookMs,
          roundTripMs: performance.now() - tq,
          ids: res.results.map((r) => r.id),
        });
      }
      record.queryVectorizeTotalMs = qVecs.ms;
      record.queryVectorizePerQueryMs = qVecs.ms / queries.length;

      // 3.4 端到端（含渲染）
      record.endToEnd = await measureEndToEnd(extractor2, queries[0].query);

      out.reps.push(record);
    }
    setStatus('done');
  } catch (err) {
    out.error = err?.message || String(err);
    out.stack = err?.stack || '';
    setStatus(`error: ${out.error}`);
  }
  return out;
};

window.__benchInfo = () => ({
  dtype: DTYPE,
  device: DEVICE,
  batch: BATCH,
  remoteHost: currentRemoteHost(),
  hasWebGPU: typeof navigator !== 'undefined' && 'gpu' in navigator,
});
