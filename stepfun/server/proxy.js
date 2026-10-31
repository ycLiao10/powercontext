/**
 * 薄 HTTP 代理：浏览器 <-> seekdb。
 *
 * 为什么必须有这一层：
 *   1. 浏览器无法直连 MySQL 协议端口（只能发 HTTP/HTTPS，请求头不能自定义）；
 *   2. 官方 seekdb JS SDK 依赖 mysql2，是 Node-only，浏览器里跑不了。
 *
 * 代理**只做转发**，不做任何推理、不预计算向量、不缓存检索结果：
 *   - 浏览器算好文档向量 → POST /api/documents → 代理写入 seekdb
 *   - 浏览器算好查询向量 → POST /api/search    → 代理在 seekdb 内执行一条 SQL
 *   - 代理把 seekdb 返回的原始行直接回传，前端只负责显示
 */

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { config, MODEL_ID } from './config.js';
import {
  connect,
  ensureSchema,
  insertDocuments,
  countDocuments,
  truncateDocuments,
  withTabletRetry,
  search,
  hybridSearchSql,
  vectorSearchSql,
  fulltextSearchSql,
} from './db.js';
import { loggedQuery } from './sql-log.js';

const JSON_HEADERS = {
  'Content-Type': 'application/json; charset=utf-8',
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Content-Type',
  // 注意：DELETE 必须在列表里。浏览器对带 Content-Type: application/json 的
  // DELETE 会先发 OPTIONS 预检，少了 DELETE 预检就会失败（表现为 "Failed to fetch"）。
  'Access-Control-Allow-Methods': 'GET,POST,DELETE,OPTIONS',
};

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload, null, 2);
  res.writeHead(status, JSON_HEADERS);
  res.end(body);
}

/** 校验浏览器提交的向量维度，避免把底层 SQL 错误抛给前端。 */
function checkDimension(embedding) {
  if (embedding === undefined || embedding === null) return '缺少 embedding';
  if (typeof embedding === 'string') {
    const dim = embedding.trim().replace(/^\[|\]$/g, '').split(',').filter((s) => s.trim()).length;
    if (dim !== config.vectorDim) {
      return `向量维度不匹配：期望 ${config.vectorDim}，实际 ${dim}`;
    }
    return null;
  }
  if (!Array.isArray(embedding)) return 'embedding 必须是数组或字符串';
  if (embedding.length !== config.vectorDim) {
    return `向量维度不匹配：期望 ${config.vectorDim}，实际 ${embedding.length}`;
  }
  return null;
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

/** 静态文件的 Content-Type（仅模型镜像用）。 */
const CONTENT_TYPES = {
  '.json': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.model': 'text/plain; charset=utf-8',
  '.onnx': 'application/octet-stream',
  '.bin': 'application/octet-stream',
};

/**
 * GET /models/* —— 本地模型镜像（权重等静态文件）。
 *
 * 这里只做「把文件原样吐给浏览器」，不做任何推理，也不缓存结果。
 * 关键点：**文件缺失必须返回 404**。
 * 如果缺文件时回 200 + HTML，transformers.js 会把 HTML 当 JSON 解析而报错。
 */
function serveModelFile(req, res, url) {
  const rel = decodeURIComponent(url.pathname.slice('/models/'.length));
  const target = path.resolve(config.modelDir, rel);
  // 目录穿越防护
  if (target !== config.modelDir && !target.startsWith(config.modelDir + path.sep)) {
    sendJson(res, 403, { ok: false, error: '非法路径' });
    return;
  }
  if (!fs.existsSync(target) || !fs.statSync(target).isFile()) {
    sendJson(res, 404, { ok: false, error: `模型文件不存在：${rel}` });
    return;
  }
  const stat = fs.statSync(target);
  const type = CONTENT_TYPES[path.extname(target).toLowerCase()] || 'application/octet-stream';
  res.writeHead(200, {
    'Content-Type': type,
    'Content-Length': stat.size,
    'Cache-Control': 'public, max-age=86400',
    'Access-Control-Allow-Origin': '*',
  });
  if (req.method === 'HEAD') {
    res.end();
    return;
  }
  fs.createReadStream(target).pipe(res);
}

/**
 * GET /ort/* —— onnxruntime-web 运行时资源（wasm 二进制 + 胶水 .mjs）。
 *
 * .mjs 是被浏览器用动态 import() 加载的，必须返回 JS MIME 类型；
 * 跨源加载要求 CORS 头。
 */
function serveOrtFile(req, res, url) {
  const rel = decodeURIComponent(url.pathname.slice('/ort/'.length));
  const target = path.resolve(config.ortDir, rel);
  if (target !== config.ortDir && !target.startsWith(config.ortDir + path.sep)) {
    sendJson(res, 403, { ok: false, error: '非法路径' });
    return;
  }
  if (!fs.existsSync(target) || !fs.statSync(target).isFile()) {
    sendJson(res, 404, { ok: false, error: `运行时文件不存在：${rel}` });
    return;
  }
  const stat = fs.statSync(target);
  const ext = path.extname(target).toLowerCase();
  const type =
    ext === '.mjs' || ext === '.js'
      ? 'text/javascript; charset=utf-8'
      : ext === '.wasm'
        ? 'application/wasm'
        : 'application/octet-stream';
  res.writeHead(200, {
    'Content-Type': type,
    'Content-Length': stat.size,
    'Cache-Control': 'public, max-age=86400',
    'Access-Control-Allow-Origin': '*',
  });
  fs.createReadStream(target).pipe(res);
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'OPTIONS') {
    res.writeHead(204, JSON_HEADERS);
    res.end();
    return;
  }

  const url = new URL(req.url, `http://${req.headers.host}`);
  let conn;
  try {
    conn = await connect();

    // GET /models/* —— 本地模型镜像（静态文件）
    if ((req.method === 'GET' || req.method === 'HEAD') && url.pathname.startsWith('/models/')) {
      serveModelFile(req, res, url);
      return;
    }

    // GET /ort/* —— onnxruntime-web 运行时资源（静态文件）
    if ((req.method === 'GET' || req.method === 'HEAD') && url.pathname.startsWith('/ort/')) {
      serveOrtFile(req, res, url);
      return;
    }

    // GET /api/health —— 探活 + 自检信息
    if (req.method === 'GET' && url.pathname === '/api/health') {
      const n = await countDocuments(conn);
      sendJson(res, 200, {
        ok: true,
        seekdb: `${config.seekdb.host}:${config.seekdb.port}`,
        database: config.seekdb.database,
        table: config.seekdb.table,
        documents: n,
        vectorDim: config.vectorDim,
        model: MODEL_ID,
        modelDir: config.modelDir,
        sqlLog: config.sqlLog.enabled ? config.sqlLog.file : '(disabled)',
      });
      return;
    }

    // POST /api/schema —— 建库建表（幂等）
    if (req.method === 'POST' && url.pathname === '/api/schema') {
      await ensureSchema(conn);
      sendJson(res, 200, { ok: true, schema: 'ready' });
      return;
    }

    // POST /api/documents —— 写入文档（向量由浏览器算好）
    if (req.method === 'POST' && url.pathname === '/api/documents') {
      const body = await readBody(req);
      const docs = Array.isArray(body.documents) ? body.documents : [];
      if (!docs.length) {
        sendJson(res, 400, { ok: false, error: 'documents 不能为空' });
        return;
      }
      for (const [i, d] of docs.entries()) {
        const err = checkDimension(d.embedding);
        if (err) {
          sendJson(res, 400, { ok: false, error: `documents[${i}] (${d.id ?? '?'}): ${err}` });
          return;
        }
      }
      const t = config.seekdb.table;
      const placeholders = docs.map(() => '(?,?,?,?,?)').join(',');
      const values = docs.flatMap((d) => [
        String(d.id),
        d.title ?? null,
        d.content ?? '',
        typeof d.embedding === 'string'
          ? d.embedding
          : `[${d.embedding.map((v) => Number(v).toFixed(6)).join(',')}]`,
        d.metadata ? JSON.stringify(d.metadata) : null,
      ]);
      const sql = `INSERT INTO \`${t}\` (id,title,content,embedding,metadata) VALUES ${placeholders}
        ON DUPLICATE KEY UPDATE title=VALUES(title), content=VALUES(content),
                                embedding=VALUES(embedding), metadata=VALUES(metadata)`;
      // insertMs 只覆盖 INSERT 本身（含索引同步），不含连接建立与参数校验
      const insertStart = process.hrtime.bigint();
      // 清表是 DDL，紧随其后的 INSERT 可能撞上 tablet 重建窗口，套一层退避重试
      await withTabletRetry(() =>
        loggedQuery(conn, 'insert', sql, values, '浏览器提交的文档向量'),
      );
      const insertMs = Number(process.hrtime.bigint() - insertStart) / 1e6;
      const n = await withTabletRetry(() => countDocuments(conn));
      sendJson(res, 200, { ok: true, inserted: docs.length, documents: n, insertMs });
      return;
    }

    // POST /api/search —— 检索（hybrid / vector / fulltext）
    if (req.method === 'POST' && url.pathname === '/api/search') {
      const body = await readBody(req);
      const mode = body.mode ?? 'hybrid';
      const topK = Number(body.topK ?? 5);
      const query = String(body.query ?? '');
      const embedding = body.embedding;

      if (mode === 'hybrid' || mode === 'vector') {
        const err = checkDimension(embedding);
        if (err) {
          sendJson(res, 400, { ok: false, error: err });
          return;
        }
      }

      const started = process.hrtime.bigint();
      let rows;
      if (mode === 'hybrid') {
        const vector =
          typeof embedding === 'string'
            ? embedding
            : `[${embedding.map((v) => Number(v).toFixed(6)).join(',')}]`;
        const sql = hybridSearchSql(topK);
        rows = await withTabletRetry(() =>
          loggedQuery(
            conn,
            'hybrid',
            sql,
            [query, query, vector, vector],
            '向量 + 全文混合检索：一条 SQL，RRF 融合',
          ),
        );
      } else if (mode === 'vector') {
        const vector =
          typeof embedding === 'string'
            ? embedding
            : `[${embedding.map((v) => Number(v).toFixed(6)).join(',')}]`;
        rows = await withTabletRetry(() =>
          loggedQuery(conn, 'vector', vectorSearchSql(topK), [vector, vector], '纯向量检索'),
        );
      } else if (mode === 'fulltext') {
        rows = await withTabletRetry(() =>
          loggedQuery(
            conn,
            'fulltext',
            fulltextSearchSql(topK),
            [query, query],
            '纯全文检索',
          ),
        );
      } else {
        sendJson(res, 400, { ok: false, error: `未知模式：${mode}` });
        return;
      }
      const durationMs = Number(process.hrtime.bigint() - started) / 1e6;

      sendJson(res, 200, {
        ok: true,
        mode,
        query,
        tookMs: Number(durationMs.toFixed(3)),
        count: rows.length,
        results: rows,
      });
      return;
    }

    // DELETE /api/documents —— 清空知识库
    if (req.method === 'DELETE' && url.pathname === '/api/documents') {
      await truncateDocuments(conn);
      sendJson(res, 200, { ok: true, documents: 0 });
      return;
    }

    sendJson(res, 404, { ok: false, error: 'not found' });
  } catch (err) {
    console.error('[proxy] 处理失败：', err);
    sendJson(res, 500, { ok: false, error: err.message });
  } finally {
    if (conn) await conn.end().catch(() => {});
  }
});

server.listen(config.proxy.port, config.proxy.host, () => {
  console.log(`[proxy] 监听 http://${config.proxy.host}:${config.proxy.port}`);
  console.log(`[proxy] seekdb = ${config.seekdb.host}:${config.seekdb.port}/${config.seekdb.database}`);
  console.log(`[proxy] SQL 日志 = ${config.sqlLog.enabled ? config.sqlLog.file : '(关闭)'}`);
});

export default server;
