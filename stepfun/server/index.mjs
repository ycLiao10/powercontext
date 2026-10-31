/**
 * 本地桥接服务（Node）。
 *
 * 浏览器端负责「端侧推理」（Transformers.js 在用户设备上算向量），
 * 本服务负责「seekdb 数据层」（用 seekdb-js 写入向量/原文/元数据，并在 seekdb 内完成混合检索）。
 * 服务本身不做任何 embedding 计算，也不缓存任何检索结果。
 */
import express from "express";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { SeekdbStore, openCollection, tableNameOf } from "./seekdb-store.mjs";
import { listModels, getModel, DEFAULT_MODEL, collectionNameFor } from "../shared/models.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const stepfunRoot = path.resolve(__dirname, "..");

const PORT = Number(process.env.PORT ?? 8787);
const WEB_DIST = path.join(stepfunRoot, "web", "dist");
const MODELS_DIR = process.env.MODELS_DIR
  ? path.resolve(process.env.MODELS_DIR)
  : path.join(stepfunRoot, "models");
const ORT_DIR = path.join(stepfunRoot, "web", "public", "ort");

const store = new SeekdbStore({
  host: process.env.SEEKDB_HOST ?? "127.0.0.1",
  port: Number(process.env.SEEKDB_PORT ?? 2881),
  user: process.env.SEEKDB_USER ?? "root",
  password: process.env.SEEKDB_PASSWORD ?? "",
  database: process.env.SEEKDB_DATABASE ?? "test",
  tenant: process.env.SEEKDB_TENANT ?? "",
});

const app = express();
app.use(express.json({ limit: "256mb" }));

/**
 * 开启跨源隔离（COOP + COEP），使浏览器启用 SharedArrayBuffer，
 * WASM 后端才能真正开多线程（ORT 会自动回退到单线程否则）。
 * 所有静态资源都来自本机，因此 require-corp 不会影响任何子资源。
 */
app.use((_req, res, next) => {
  res.setHeader("Cross-Origin-Opener-Policy", "same-origin");
  res.setHeader("Cross-Origin-Embedder-Policy", "require-corp");
  next();
});

/** 统一的耗时包装：所有阶段计时都基于 performance.now()，返回毫秒 */
const timed = async (fn) => {
  const t0 = performance.now();
  const value = await fn();
  return { value, ms: +(performance.now() - t0).toFixed(2) };
};

/** 连接类错误时重建底层连接，避免一次网络抖动让整个服务不可用 */
const withRetry = async (fn) => {
  try {
    return await fn();
  } catch (err) {
    const msg = String(err?.message ?? err);
    if (/closed state|ECONNRESET|ECONNREFUSED|PROTOCOL_CONNECTION_LOST|connection is closed/i.test(msg)) {
      console.warn("[stepfun] 连接异常，重建后重试一次：", msg);
      store.reset();
      return await fn();
    }
    throw err;
  }
};

app.get("/api/health", async (_req, res) => {
  try {
    const version = await withRetry(() => store.ping());
    res.json({
      ok: true,
      db: {
        host: store.config.host,
        port: store.config.port,
        database: store.config.database,
        version,
      },
      modelsDir: MODELS_DIR,
      modelsDirExists: fs.existsSync(MODELS_DIR),
    });
  } catch (err) {
    res.status(503).json({ ok: false, error: String(err?.message ?? err) });
  }
});

app.get("/api/models", (_req, res) => {
  res.json({
    default: DEFAULT_MODEL,
    models: listModels().map((m) => ({ ...m, collection: collectionNameFor(m.id) })),
  });
});

app.get("/api/collections", async (_req, res) => {
  try {
    const { value, ms } = await timed(() => withRetry(() => store.listCollections()));
    res.json({ collections: value, ms });
  } catch (err) {
    res.status(500).json({ error: String(err?.message ?? err) });
  }
});

app.post("/api/collections", async (req, res) => {
  try {
    const { name, dimension, analyzer, model } = req.body ?? {};
    const resolvedName = name ?? (model ? collectionNameFor(model) : undefined);
    if (!resolvedName) {
      return res.status(400).json({ error: "缺少集合名称或模型标识" });
    }
    const dim = Number(dimension ?? getModel(model)?.dimension ?? 0);
    if (!dim) return res.status(400).json({ error: "缺少向量维度" });
    const { value, ms } = await timed(() =>
      withRetry(() => store.ensureCollection({ name: resolvedName, dimension: dim, analyzer }))
    );
    res.json({
      name: value.name,
      collectionId: value.collectionId,
      table: tableNameOf(value),
      dimension: dim,
      ms,
    });
  } catch (err) {
    res.status(500).json({ error: String(err?.message ?? err) });
  }
});

app.delete("/api/collections/:name", async (req, res) => {
  try {
    await withRetry(() => store.dropCollection(req.params.name));
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: String(err?.message ?? err) });
  }
});

app.get("/api/collections/:name/stats", async (req, res) => {
  try {
    const collection = await withRetry(() => openCollection(store.client, req.params.name));
    const [count, ddl] = await Promise.all([
      collection.count().catch(() => 0),
      store.getTableDdl(collection).catch(() => ""),
    ]);
    const info = await collection.describe().catch(() => ({}));
    res.json({
      name: collection.name,
      collectionId: collection.collectionId,
      table: tableNameOf(collection),
      dimension: info.dimension ?? collection.dimension ?? 0,
      distance: info.distance ?? null,
      documentCount: count,
      ddl,
    });
  } catch (err) {
    res.status(404).json({ error: String(err?.message ?? err) });
  }
});

/**
 * 增量写入：浏览器已完成向量化，这里只把向量 + 原文 + 元数据写入 seekdb。
 * 写入后立即刷新索引，保证新文档马上可检索。
 */
app.post("/api/collections/:name/documents", async (req, res) => {
  try {
    const { documents = [], refresh = true } = req.body ?? {};
    if (!Array.isArray(documents) || documents.length === 0) {
      return res.status(400).json({ error: "documents 不能为空" });
    }
    const collection = await withRetry(() => openCollection(store.client, req.params.name));
    const ids = documents.map((d) => String(d.id));
    const texts = documents.map((d) => String(d.text ?? ""));
    const vectors = documents.map((d) => d.vector);
    const metadatas = documents.map((d) => d.metadata ?? {});
    if (vectors.some((v) => !Array.isArray(v))) {
      return res.status(400).json({ error: "每条文档都必须带向量" });
    }
    const write = await timed(() =>
      withRetry(() =>
        store.upsertDocuments(collection, {
          ids,
          documents: texts,
          embeddings: vectors,
          metadatas,
        })
      )
    );
    let refreshMs = 0;
    if (refresh) {
      const r = await timed(() => withRetry(() => store.refreshIndexes(collection)));
      refreshMs = r.ms;
    }
    const count = await collection.count().catch(() => 0);
    res.json({
      written: documents.length,
      total: count,
      writeMs: write.ms,
      refreshMs,
      ms: +(write.ms + refreshMs).toFixed(2),
    });
  } catch (err) {
    res.status(500).json({ error: String(err?.message ?? err) });
  }
});

app.post("/api/collections/:name/refresh", async (req, res) => {
  try {
    const collection = await withRetry(() => openCollection(store.client, req.params.name));
    const { ms } = await timed(() => withRetry(() => store.refreshIndexes(collection)));
    res.json({ ok: true, refreshMs: ms });
  } catch (err) {
    res.status(500).json({ error: String(err?.message ?? err) });
  }
});

/** 混合检索：一次请求 -> seekdb 内单条候选 SQL 完成向量 + 全文融合 */
app.post("/api/collections/:name/search", async (req, res) => {
  try {
    const { vector, text = "", topK = 10, rrf } = req.body ?? {};
    if (!Array.isArray(vector)) {
      return res.status(400).json({ error: "缺少查询向量" });
    }
    const collection = await withRetry(() => openCollection(store.client, req.params.name));
    const { value, ms } = await timed(() =>
      withRetry(() =>
        store.hybridSearch(collection, {
          vector,
          text,
          topK: Number(topK) || 10,
          rrf: rrf === null ? null : rrf ?? { rankWindowSize: 100, rankConstant: 60 },
        })
      )
    );
    res.json({ results: value, dbMs: ms, count: value.length });
  } catch (err) {
    res.status(500).json({ error: String(err?.message ?? err) });
  }
});

/** 返回 seekdb 为本次混合检索生成的单条候选 SQL，便于复核「检索在库内完成」 */
app.post("/api/collections/:name/explain", async (req, res) => {
  try {
    const { vector, text = "", topK = 10, rrf } = req.body ?? {};
    const collection = await withRetry(() => openCollection(store.client, req.params.name));
    const sql = await withRetry(() =>
      store.explainHybridSearch(collection, {
        vector,
        text,
        topK: Number(topK) || 10,
        rrf: rrf ?? { rankWindowSize: 100, rankConstant: 60 },
      })
    );
    res.json({ sql });
  } catch (err) {
    res.status(500).json({ error: String(err?.message ?? err) });
  }
});

// ---- 静态资源 ----
if (fs.existsSync(MODELS_DIR)) {
  app.use("/models", express.static(MODELS_DIR, { maxAge: "1h", fallthrough: true }));
}
if (fs.existsSync(ORT_DIR)) {
  app.use("/ort", express.static(ORT_DIR, { maxAge: "1h", fallthrough: true }));
}
const CORPUS_DIR = path.join(stepfunRoot, "data", "corpus");
if (fs.existsSync(CORPUS_DIR)) {
  app.use("/corpus", express.static(CORPUS_DIR, { maxAge: "5m", fallthrough: true }));
}
if (fs.existsSync(WEB_DIST)) {
  app.use(express.static(WEB_DIST, { index: "index.html" }));
  app.get("*", (req, res, next) => {
    if (req.path.startsWith("/api")) return next();
    res.sendFile(path.join(WEB_DIST, "index.html"));
  });
}

app.listen(PORT, "127.0.0.1", () => {
  console.log(`[stepfun] web + api  -> http://127.0.0.1:${PORT}`);
  console.log(`[stepfun] seekdb    -> ${store.config.host}:${store.config.port}/${store.config.database}`);
  console.log(
    `[stepfun] models    -> ${MODELS_DIR} (${fs.existsSync(MODELS_DIR) ? "found" : "MISSING, run scripts/fetch-model.mjs"})`
  );
  if (!fs.existsSync(WEB_DIST)) {
    console.log(`[stepfun] WARN: ${WEB_DIST} 不存在，请先执行 npm run build`);
  }
});
