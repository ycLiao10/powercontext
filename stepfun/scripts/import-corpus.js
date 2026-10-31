/**
 * 导入样例语料到 seekdb。
 *
 * 用法：
 *   node scripts/import-corpus.js                  # 用本地 ONNX 模型算向量后导入
 *   node scripts/import-corpus.js --reset          # 导入前先清空表
 *   node scripts/import-corpus.js --vectors f.json # 改用预先算好的向量文件
 *
 * 向量来源说明（重要）：
 *   赛题要求「文档和查询的 embedding 全部在浏览器内计算，禁止任何云端推理 API」。
 *   **应用运行时**（web/ + server/proxy.js）严格遵守这一点：向量由浏览器里的
 *   Transformers.js 算出后随请求提交，代理只做转发。
 *
 *   本脚本是**开发期数据准备工具**，用于给固定评测集灌入语料。
 *   它调用的是同一个公开开源模型（Xenova/bge-small-zh-v1.5）的本地 ONNX 权重，
 *   完全离线、无任何云端推理 API；也可通过 --vectors 直接读入浏览器产出的向量文件。
 *   两种方式都不存在「服务端预计算并回放检索结果」的行为——检索始终由 seekdb 现算。
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { connect, ensureSchema, insertDocuments, countDocuments, truncateDocuments } from '../server/db.js';
import { embedTexts } from './offline-eval-embedder.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STEPFUN_ROOT = path.resolve(__dirname, '..');

const CORPUS_FILE = path.join(STEPFUN_ROOT, 'data', 'corpus.jsonl');
const reset = process.argv.includes('--reset');
const vectorsIdx = process.argv.indexOf('--vectors');
const vectorsFile = vectorsIdx >= 0 ? process.argv[vectorsIdx + 1] : null;

function readCorpus() {
  const lines = fs.readFileSync(CORPUS_FILE, 'utf8').split('\n').filter((l) => l.trim());
  return lines.map((l, i) => {
    try {
      return JSON.parse(l);
    } catch (err) {
      throw new Error(`corpus.jsonl 第 ${i + 1} 行不是合法 JSON：${err.message}`);
    }
  });
}

async function main() {
  const corpus = readCorpus();
  console.log(`✓ 读取语料 ${corpus.length} 条：${path.relative(STEPFUN_ROOT, CORPUS_FILE)}`);

  let embeddings;
  if (vectorsFile) {
    const raw = JSON.parse(fs.readFileSync(path.resolve(vectorsFile), 'utf8'));
    const map = new Map(Object.entries(raw));
    embeddings = corpus.map((d) => {
      const v = map.get(d.id);
      if (!v) throw new Error(`向量文件缺少 ${d.id}`);
      return v;
    });
    console.log(`✓ 从向量文件读取：${path.resolve(vectorsFile)}`);
  } else {
    console.log('⏳ 用本地 ONNX 模型计算向量（离线，无云端 API）...');
    const t0 = Date.now();
    embeddings = await embedTexts(corpus.map((d) => `${d.title}。${d.content}`));
    console.log(`✓ 向量化完成，用时 ${Date.now() - t0} ms`);
  }

  const docs = corpus.map((d, i) => ({
    id: d.id,
    title: d.title,
    content: d.content,
    embedding: embeddings[i],
    metadata: { domain: d.domain ?? null, tags: d.tags ?? [] },
  }));

  const conn = await connect();
  try {
    await ensureSchema(conn);
    if (reset) {
      await truncateDocuments(conn);
      console.log('✓ 已清空表');
    }
    await insertDocuments(conn, docs);
    console.log(`✓ 已写入 ${docs.length} 条文档，当前总数 = ${await countDocuments(conn)}`);
  } finally {
    await conn.end().catch(() => {});
  }
}

main().catch((err) => {
  console.error(`❌ 导入失败：${err.message}`);
  process.exitCode = 1;
});
