/**
 * 应用主逻辑：导入 → 浏览器本地向量化 → 写入 seekdb → seekdb 内混合检索 → 展示结果。
 */
import { detectWebGPU, loadPipeline, embedTexts, embedOne, toPlainVector } from "./embedder.js";
import * as api from "./api.js";

const $ = (sel) => document.querySelector(sel);
const fmtMs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${ms.toFixed(1)} ms`);
const fmtBytes = (b) =>
  b >= 1024 * 1024 ? `${(b / 1024 / 1024).toFixed(2)} MB` : `${(b / 1024).toFixed(1)} KB`;

const state = {
  models: [],
  modelId: null,
  model: null,
  deviceChoice: "auto",
  dtype: null,
  extractor: null,
  backend: null,
  gpu: { available: false },
  collection: null,
  manifest: null,
  loadMs: 0,
  lastDocs: [],
};

const median = (arr) => {
  if (!arr.length) return 0;
  const s = [...arr].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
};

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */
async function init() {
  bindEvents();
  try {
    const h = await api.health();
    if (h.ok) {
      setBadge("db-badge", `seekdb ${h.db.version}`, "badge-ok");
      $("#model-info").innerHTML =
        `数据库 <code>${h.db.host}:${h.db.port}/${h.db.database}</code> · ` +
        `权重目录 <code>${h.modelsDir}</code>${h.modelsDirExists ? "" : "（缺失，请先执行 <code>npm run fetch-models</code>）"}`;
    } else {
      setBadge("db-badge", "seekdb 不可用", "badge-warn");
    }
  } catch (err) {
    setBadge("db-badge", "seekdb 不可用", "badge-warn");
    $("#model-info").textContent = `无法连接桥接服务：${err.message}`;
  }

  try {
    const m = await api.listModels();
    state.models = m.models;
    state.modelId = m.default;
    renderModelOptions();
    await refreshCollections();
  } catch (err) {
    console.error(err);
  }
  await loadManifest();
  detectWebGPU().then((gpu) => {
    state.gpu = gpu;
    if (gpu.available) {
      $("#device-select").querySelector('option[value="webgpu"]').disabled = false;
    }
  });
}

function setBadge(id, text, cls) {
  const b = document.getElementById(id);
  b.textContent = text;
  b.className = `badge ${cls}`;
}

function bindEvents() {
  $("#model-select").addEventListener("change", (e) => {
    state.modelId = e.target.value;
    state.model = state.models.find((x) => x.id === state.modelId);
    renderDtypeOptions();
    loadManifest();
  });
  $("#dtype-select").addEventListener("change", (e) => {
    state.dtype = e.target.value;
  });
  $("#device-select").addEventListener("change", (e) => {
    state.deviceChoice = e.target.value;
  });
  $("#load-model").addEventListener("click", onLoadModel);
  $("#import-btn").addEventListener("click", onImport);
  $("#search-btn").addEventListener("click", onSearch);
  $("#query-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") onSearch();
  });
  $("#explain-btn").addEventListener("click", onExplain);
  $("#bench-btn").addEventListener("click", onBenchmark);
  $("#load-sample").addEventListener("click", loadSampleCorpus);
  $("#corpus-file").addEventListener("change", onFilesPicked);
  $("#collection-select").addEventListener("change", async (e) => {
    state.collection = e.target.value;
    await renderStats();
  });
}

function renderModelOptions() {
  const sel = $("#model-select");
  sel.innerHTML = "";
  for (const m of state.models) {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = `${m.label} · ${m.dimension}d`;
    sel.appendChild(o);
  }
  sel.value = state.modelId;
  state.model = state.models.find((x) => x.id === state.modelId);
  renderDtypeOptions();
}

/** 精度选项按后端能力给出：WebGPU 支持 fp16/fp32/q8，WASM 仅 fp32/q8 */
function renderDtypeOptions() {
  const sel = $("#dtype-select");
  const prev = state.dtype;
  sel.innerHTML = "";
  const options =
    state.deviceChoice === "wasm"
      ? [["q8", "INT8（动态量化，q8）"], ["fp32", "FP32（未量化基线）"]]
      : [
          ["fp16", "FP16（半精度）"],
          ["q8", "INT8（动态量化，q8）"],
          ["fp32", "FP32（未量化基线）"],
        ];
  for (const [value, label] of options) {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    sel.appendChild(o);
  }
  state.dtype = options.some(([v]) => v === prev) ? prev : options[0][0];
  sel.value = state.dtype;
}

async function loadManifest() {
  try {
    const res = await fetch("/models/manifest.json");
    if (!res.ok) return;
    const manifest = await res.json();
    state.manifest = manifest;
    const entry = manifest.models?.[state.modelId];
    if (!entry) return;
    const w = entry.weightBytes ?? {};
    const ratio = entry.compressionRatio ?? {};
    $("#model-meta").innerHTML = [
      cell("FP32 权重", fmtBytes(w.fp32 ?? 0)),
      cell("FP16 权重", fmtBytes(w.fp16 ?? 0)),
      cell("INT8 权重", fmtBytes(w.int8 ?? 0)),
      cell("压缩比 (FP32/INT8)", ratio.int8 ? `${ratio.int8.toFixed(2)}×` : "-"),
      cell("压缩比 (FP32/FP16)", ratio.fp16 ? `${ratio.fp16.toFixed(2)}×` : "-"),
    ].join("");
  } catch {
    /* manifest 缺失不影响主流程 */
  }
}

const cell = (k, v) => `<div class="cell"><span class="k">${k}</span><span class="v">${v}</span></div>`;

/* ------------------------------------------------------------------ */
/* 模型加载与后端降级                                                   */
/* ------------------------------------------------------------------ */
/**
 * 按「device 显式指定 + 失败自动降级」的规则加载模型。
 * 返回 { extractor, backend, dtype, loadMs, gpu }。
 */
async function loadModel({ modelId, dtype, device: deviceChoice } = {}) {
  const targetModel = modelId ?? state.modelId;
  const gpu = await detectWebGPU();
  state.gpu = gpu;

  let device = deviceChoice ?? state.deviceChoice;
  if (device === "auto") device = gpu.available ? "webgpu" : "wasm";
  if (device === "webgpu" && !gpu.available) {
    console.warn("WebGPU 不可用，降级到 WASM：", gpu.reason);
    device = "wasm";
  }

  // 精度与后端匹配：WASM 不支持 fp16，回退到 INT8(q8)
  let targetDtype = dtype ?? state.dtype;
  if (device === "wasm" && targetDtype === "fp16") targetDtype = "q8";

  const attempts = [{ device, dtype: targetDtype }];
  if (device === "webgpu") {
    attempts.push({ device: "wasm", dtype: targetDtype === "fp16" ? "q8" : targetDtype });
  }

  let extractor = null;
  let used = null;
  let lastErr = null;
  const t0 = performance.now();
  for (const a of attempts) {
    try {
      extractor = await loadPipeline({
        modelId: targetModel,
        dtype: a.dtype,
        device: a.device,
        onProgress: (p) => {
          if (p?.status === "progress" && p.file?.endsWith(".onnx")) {
            const pct = Math.round((p.loaded ?? 0) * 100);
            const btn = $("#load-model");
            if (btn) btn.textContent = `下载/加载 ${p.file} ${pct}%`;
          }
        },
      });
      used = a;
      break;
    } catch (err) {
      lastErr = err;
      console.warn(`${a.device}/${a.dtype} 加载失败，尝试降级：`, err);
    }
  }
  if (!extractor) throw lastErr ?? new Error("模型加载失败");

  state.extractor = extractor;
  state.backend = used.device;
  state.dtype = used.dtype;
  state.loadMs = performance.now() - t0;
  return { extractor, backend: used.device, dtype: used.dtype, loadMs: state.loadMs, gpu };
}

/** 把实际使用的后端/精度同步到界面徽标（UI 与自动化测试共用） */
function updateRuntimeBadges(used) {
  const cls = used.backend === "webgpu" ? "badge-ok" : "badge-warn";
  const label = used.backend === "webgpu" ? "WebGPU" : "WASM（降级）";
  setBadge("backend-badge", `推理后端：${label} · ${used.dtype}`, cls);
  setBadge(
    "model-badge",
    `${state.modelId.split("/").pop()} · ${state.model?.dimension ?? "?"}d`,
    "badge-ok"
  );
  const btn = $("#load-model");
  if (btn) btn.textContent = "加载模型（本地）";
  return { label, cls };
}

async function onLoadModel() {
  const btn = $("#load-model");
  btn.disabled = true;
  btn.textContent = "加载中…";
  try {
    const used = await loadModel();
    updateRuntimeBadges(used);
    $("#model-meta").innerHTML =
      [
        cell("实际后端", used.backend === "webgpu" ? "WebGPU" : "WASM（降级）"),
        cell("实际精度", used.dtype),
        cell("模型加载耗时", fmtMs(used.loadMs)),
        cell("WebGPU 可用", used.gpu.available ? "是" : `否（${used.gpu.reason ?? ""}）`),
      ].join("") + $("#model-meta").innerHTML;
  } catch (err) {
    console.error(err);
    setBadge("backend-badge", "模型加载失败", "badge-warn");
    alert(`模型加载失败：${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "加载模型（本地）";
  }
}

/* ------------------------------------------------------------------ */
/* 集合                                                                */
/* ------------------------------------------------------------------ */
async function refreshCollections() {
  try {
    const { collections } = await api.listCollections();
    const sel = $("#collection-select");
    const prev = state.collection;
    sel.innerHTML = "";
    if (!collections.length) {
      const o = document.createElement("option");
      o.value = "";
      o.textContent = "（尚无集合，导入时自动创建）";
      sel.appendChild(o);
      state.collection = null;
      return;
    }
    for (const c of collections) {
      const o = document.createElement("option");
      o.value = c.name;
      o.textContent = `${c.name} · ${c.documentCount} 篇`;
      sel.appendChild(o);
    }
    state.collection =
      prev && collections.some((c) => c.name === prev) ? prev : collections[0].name;
    sel.value = state.collection;
    await renderStats();
  } catch (err) {
    console.error(err);
  }
}

/** 创建一个（或复用）指定名称的集合，并把它设为当前集合 */
async function ensureCollectionNamed(name, dimension) {
  await api.createCollection({ name, dimension, model: state.modelId });
  const sel = $("#collection-select");
  if (![...sel.options].some((o) => o.value === name)) {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name;
    sel.appendChild(o);
  }
  sel.value = name;
  state.collection = name;
  return name;
}

async function ensureCurrentCollection() {
  // 集合名由服务端统一生成（seekdb-js 只允许字母/数字/下划线）
  const name =
    state.model.collection ??
    `kb_${state.modelId.split("/").pop().replace(/[^A-Za-z0-9]+/g, "_")}`;
  return await ensureCollectionNamed(name, state.model.dimension);
}

async function renderStats() {
  if (!state.collection) {
    $("#kb-stats").innerHTML = "";
    return;
  }
  try {
    const s = await api.collectionStats(state.collection);
    $("#kb-stats").innerHTML = [
      cell("集合", s.name),
      cell("seekdb 表", s.table),
      cell("文档数", s.documentCount),
      cell("向量维度", s.dimension),
      cell("距离度量", s.distance ?? "-"),
    ].join("");
  } catch {
    $("#kb-stats").innerHTML = "";
  }
}

/* ------------------------------------------------------------------ */
/* 导入                                                                */
/* ------------------------------------------------------------------ */
const fnv1a = (str) => {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
};

/** 空行分隔为多个文档；同一段文本得到稳定 id，重复导入走 upsert 而非重复插入 */
function parseCorpus(raw) {
  return raw
    .split(/\n\s*\n/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((text) => {
      const title = text.split("\n")[0].replace(/^#+\s*/, "").trim();
      return { id: `doc-${fnv1a(text)}`, title, text };
    });
}

async function onFilesPicked(e) {
  const files = [...(e.target.files ?? [])];
  if (!files.length) return;
  const parts = [];
  for (const f of files) {
    const text = await f.text();
    if (f.name.endsWith(".json")) {
      try {
        const arr = JSON.parse(text);
        const items = Array.isArray(arr) ? arr : arr.documents ?? [];
        for (const it of items) {
          const body = typeof it === "string" ? it : it.text ?? it.content ?? "";
          if (body) parts.push(body.trim());
        }
      } catch {
        parts.push(text.trim());
      }
    } else {
      parts.push(text.trim());
    }
  }
  $("#corpus-input").value = parts.join("\n\n");
  $("#import-status").textContent = `已读取 ${files.length} 个文件`;
}

function setProgress(done, total, label) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  $("#import-bar").style.width = `${pct}%`;
  $("#import-status").textContent = label;
}

async function onImport() {
  if (!state.extractor) {
    alert("请先加载模型");
    return;
  }
  const docs = parseCorpus($("#corpus-input").value);
  if (!docs.length) {
    alert("没有可导入的内容");
    return;
  }
  const btn = $("#import-btn");
  btn.disabled = true;
  try {
    const res = await importDocuments(docs, {
      batchSize: 8,
      onProgress: (done, total) => setProgress(done, total, `本地向量化 ${done}/${total}`),
    });
    setProgress(docs.length, docs.length, "完成");
    state.lastDocs = docs;
    await refreshCollections();
    $("#timing").innerHTML = [
      cell("本次文档数", docs.length),
      cell("本地向量化耗时", fmtMs(res.embedMs)),
      cell("写入+索引刷新", fmtMs(res.ms)),
      cell("其中索引刷新", fmtMs(res.refreshMs)),
      cell("端到端导入", fmtMs(res.embedMs + res.writeMs)),
    ].join("");
  } catch (err) {
    console.error(err);
    $("#import-status").textContent = `失败：${err.message}`;
    alert(`导入失败：${err.message}`);
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* 检索                                                                */
/* ------------------------------------------------------------------ */
function highlight(text, query) {
  const div = document.createElement("div");
  div.textContent = text;
  const terms = query.split(/\s+/).filter((t) => t.length > 1);
  if (!terms.length) return div;
  const html = div.innerHTML.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
  let marked = html;
  for (const t of terms) {
    const safe = t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    marked = marked.replace(new RegExp(`(${safe})`, "gi"), "<mark>$1</mark>");
  }
  const out = document.createElement("div");
  out.innerHTML = marked;
  return out;
}

async function onSearch() {
  const query = $("#query-input").value.trim();
  if (!query) return;
  if (!state.extractor) {
    alert("请先加载模型");
    return;
  }
  if (!state.collection) {
    alert("请先导入语料");
    return;
  }
  const btn = $("#search-btn");
  btn.disabled = true;
  try {
    const r = await runSearch(query, Number($("#topk-input").value) || 10);
    const res = { results: r.results, dbMs: r.dbMs };

    $("#timing").innerHTML = [
      cell("查询向量化", fmtMs(r.queryEmbedMs)),
      cell("seekdb 检索", fmtMs(r.dbMs)),
      cell("端到端", fmtMs(r.endToEndMs)),
      cell("命中", r.results.length),
    ].join("");

    const list = $("#results");
    list.innerHTML = "";
    res.results.forEach((r, i) => {
      const li = document.createElement("li");
      li.className = "result";
      const head = document.createElement("div");
      head.className = "result-head";
      head.innerHTML = `<span class="rank">#${i + 1}</span><span class="score">score ${r.score.toFixed(4)}</span><span class="rid">${r.id}</span>`;
      const title = document.createElement("div");
      title.className = "result-title";
      title.textContent = r.metadata?.title ?? "";
      const body = document.createElement("div");
      body.className = "result-body";
      body.appendChild(highlight(r.document ?? "", query));
      li.append(head, title, body);
      list.appendChild(li);
    });
    if (!res.results.length) {
      list.innerHTML = '<li class="empty">没有命中结果</li>';
    }
  } catch (err) {
    console.error(err);
    alert(`检索失败：${err.message}`);
  } finally {
    btn.disabled = false;
  }
}

async function onExplain() {
  const query = $("#query-input").value.trim() || "示例查询";
  if (!state.extractor || !state.collection) {
    alert("请先加载模型并导入语料");
    return;
  }
  const vec = await embedOne(state.extractor, state.model.queryPrefix + query);
  const { sql } = await api.explain(state.collection, {
    vector: toPlainVector(vec),
    text: query,
    topK: Number($("#topk-input").value) || 10,
  });
  const pre = $("#sql-preview");
  pre.textContent = sql;
  pre.hidden = false;
}

/* ------------------------------------------------------------------ */
/* 页内基准                                                            */
/* ------------------------------------------------------------------ */
async function onBenchmark() {
  const out = $("#bench-output");
  if (!state.extractor) {
    out.textContent = "请先加载模型后再运行基准。";
    return;
  }
  const docs = state.lastDocs.length
    ? state.lastDocs.map((d) => d.text)
    : parseCorpus($("#corpus-input").value).map((d) => d.text);
  if (!docs.length) {
    out.textContent = "没有可用于基准的文本，请先导入语料。";
    return;
  }
  out.textContent = "基准运行中…";

  const sample = docs.slice(0, Math.min(docs.length, 32));
  // 文档向量化吞吐
  const t0 = performance.now();
  await embedTexts(state.extractor, sample, { batchSize: 8 });
  const docEmbedMs = performance.now() - t0;

  // 查询向量化延迟（预热后取中位数）
  const q = "向量检索与全文检索的混合排序";
  await embedOne(state.extractor, q);
  const qEmbed = [];
  for (let i = 0; i < 10; i++) {
    const s = performance.now();
    await embedOne(state.extractor, q);
    qEmbed.push(performance.now() - s);
  }

  // seekdb 检索延迟 + 端到端
  const vec = await embedOne(state.extractor, state.model.queryPrefix + q);
  const db = [];
  const e2e = [];
  for (let i = 0; i < 10; i++) {
    const s = performance.now();
    await api.search(state.collection, {
      vector: toPlainVector(vec),
      text: q,
      topK: 10,
    });
    db.push(performance.now() - s);
    const s2 = performance.now();
    await runSearch(q, 10);
    e2e.push(performance.now() - s2);
  }

  const rows = [
    ["推理后端", state.backend],
    ["精度", state.dtype],
    ["模型加载耗时", fmtMs(state.loadMs)],
    [
      "向量化吞吐",
      `${(sample.length / (docEmbedMs / 1000)).toFixed(2)} 篇/秒（${sample.length} 篇 / ${fmtMs(docEmbedMs)}）`,
    ],
    ["查询向量化 (中位数)", fmtMs(median(qEmbed))],
    ["seekdb 检索往返 (中位数)", fmtMs(median(db))],
    ["端到端查询 (中位数)", fmtMs(median(e2e))],
  ];
  out.innerHTML =
    `<table class="bench"><tbody>${rows
      .map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`)
      .join("")}</tbody></table>` +
    `<p class="hint">以上为当前浏览器实测值；完整的多配置对比见 <code>docs/BENCHMARK.md</code>。</p>`;
}

/* ------------------------------------------------------------------ */
/* 演示语料                                                            */
/* ------------------------------------------------------------------ */
async function loadSampleCorpus() {
  {
    const names = ["doc-001.md", "doc-002.md", "doc-003.md", "doc-004.md", "doc-005.md"];
    const parts = [];
    for (const n of names) {
      const res = await fetch(`/corpus/${n}`);
      if (!res.ok) continue;
      parts.push((await res.text()).trim());
    }
    if (!parts.length) {
      $("#import-status").textContent =
        "未找到演示语料，请把 data/corpus 下的文档复制到 web/public/corpus/ 后重试";
      return;
    }
    $("#corpus-input").value = parts.join("\n\n");
    $("#import-status").textContent = `已载入 ${parts.length} 篇演示文档`;
  }
}

/* ------------------------------------------------------------------ */
/* 可复用的核心流程（UI 与自动化基准共用同一份实现）                    */
/* ------------------------------------------------------------------ */
/** 端侧向量化 + 写入 seekdb + 索引刷新，返回各阶段耗时 */
async function importDocuments(docs, { batchSize = 8, onProgress, collection } = {}) {
  if (!state.extractor) throw new Error("模型尚未加载");
  const tEmbed0 = performance.now();
  const vectors = await embedTexts(state.extractor, docs.map((d) => d.text), {
    batchSize,
    onProgress,
  });
  const embedMs = performance.now() - tEmbed0;

  const name = collection
    ? await ensureCollectionNamed(collection, state.model.dimension)
    : await ensureCurrentCollection();
  const tWrite0 = performance.now();
  const res = await api.addDocuments(
    name,
    docs.map((d, i) => ({
      id: d.id,
      text: d.text,
      vector: toPlainVector(vectors[i]),
      metadata: d.metadata ?? { title: d.title ?? "", chars: d.text.length },
    }))
  );
  const writeMs = performance.now() - tWrite0;
  return { collection: name, count: docs.length, embedMs, writeMs, ...res };
}

/** 查询向量化 + seekdb 混合检索，返回各阶段耗时 */
async function runSearch(query, topK = 10) {
  if (!state.extractor) throw new Error("模型尚未加载");
  if (!state.collection) throw new Error("尚无知识库集合");
  const tQuery0 = performance.now();
  const vec = await embedOne(state.extractor, state.model.queryPrefix + query);
  const queryEmbedMs = performance.now() - tQuery0;

  const tSearch0 = performance.now();
  const res = await api.search(state.collection, {
    vector: toPlainVector(vec),
    text: query,
    topK,
  });
  const endToEndMs = performance.now() - tSearch0;
  return {
    results: res.results,
    dbMs: res.dbMs,
    queryEmbedMs,
    endToEndMs,
    collection: state.collection,
  };
}

/* ------------------------------------------------------------------ */
/* 自动化测试钩子：供 Puppeteer 基准 / 评测脚本驱动真实浏览器推理        */
/* ------------------------------------------------------------------ */
window.__stepfun = {
  ready: true,
  state: () => ({
    modelId: state.modelId,
    backend: state.backend,
    dtype: state.dtype,
    collection: state.collection,
    loadMs: state.loadMs,
    gpu: state.gpu,
    dimension: state.model?.dimension,
    queryPrefix: state.model?.queryPrefix ?? "",
    crossOriginIsolated: typeof crossOriginIsolated !== "undefined" ? crossOriginIsolated : false,
    hardwareConcurrency: navigator.hardwareConcurrency ?? 0,
    userAgent: navigator.userAgent,
  }),
  setModel: async (modelId) => {
    state.modelId = modelId;
    state.model = state.models.find((m) => m.id === modelId) ?? state.model;
    $("#model-select").value = modelId;
    return state.model;
  },
  loadModel: async (opts) => {
    const r = await loadModel(opts);
    updateRuntimeBadges(r);
    return { backend: r.backend, dtype: r.dtype, loadMs: r.loadMs, gpu: r.gpu };
  },
  embedTexts: async (texts, batchSize = 8) =>
    (await embedTexts(state.extractor, texts, { batchSize })).map(toPlainVector),
  importDocuments: async (docs, opts) => importDocuments(docs, opts),
  search: async (query, topK) => runSearch(query, topK),
  refreshCollections,
  setCollection: async (name) => {
    state.collection = name;
    $("#collection-select").value = name;
    await renderStats();
    return name;
  },
};

init();
