/**
 * 运行时配置：全部来自环境变量，代码里只保留本地开发默认值，不含任何真实密钥。
 *
 * 环境变量                     含义                              默认值
 * ---------------------------  --------------------------------  ---------------------------
 * SEEKDB_HOST                 seekdb 主机                       127.0.0.1
 * SEEKDB_PORT                 seekdb 端口                       2881
 * SEEKDB_USER                 seekdb 用户                       root
 * SEEKDB_PASSWORD             seekdb 密码                       （空）
 * SEEKDB_DATABASE             业务库名                          stepfun_kb
 * SEEKDB_TABLE                文档表名                          documents
 * PROXY_PORT                  薄 HTTP 代理端口                   8787
 * PROXY_HOST                  薄 HTTP 代理监听地址               127.0.0.1
 * SQL_LOG_FILE                SQL 日志落盘路径                   ../evidence/sql-log.txt
 * SQL_LOG_ENABLED             是否记录实际执行的 SQL 原文        1
 * VECTOR_DIM                  向量维度                          512
 * MODEL_DIR                   本地模型镜像目录                   ../models
 */

import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** stepfun/ 目录根（server/ 的上一级）。 */
export const STEPFUN_ROOT = path.resolve(__dirname, '..');

function env(name, fallback) {
  const value = process.env[name];
  return value === undefined || value === '' ? fallback : value;
}

function envInt(name, fallback) {
  const raw = env(name, null);
  if (raw === null) return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export const config = {
  seekdb: {
    host: env('SEEKDB_HOST', '127.0.0.1'),
    port: envInt('SEEKDB_PORT', 2881),
    user: env('SEEKDB_USER', 'root'),
    password: env('SEEKDB_PASSWORD', ''),
    database: env('SEEKDB_DATABASE', 'stepfun_kb'),
    table: env('SEEKDB_TABLE', 'documents'),
  },
  proxy: {
    host: env('PROXY_HOST', '127.0.0.1'),
    port: envInt('PROXY_PORT', 8787),
  },
  sqlLog: {
    enabled: env('SQL_LOG_ENABLED', '1') !== '0',
    file: path.resolve(STEPFUN_ROOT, env('SQL_LOG_FILE', 'evidence/sql-log.txt')),
  },
  vectorDim: envInt('VECTOR_DIM', 512),
  /**
   * 本地模型镜像目录（浏览器端 transformers.js 通过 GET /models/* 读取）。
   * 目录里放的是权重等**静态文件**，不涉及任何推理；缺失文件必须返回 404。
   */
  modelDir: path.resolve(STEPFUN_ROOT, env('MODEL_DIR', 'models')),
  /**
   * onnxruntime-web 运行时资源目录（GET /ort/*）。
   * .mjs 胶水文件由浏览器动态 import() 加载，必须以 JS MIME 类型提供。
   */
  ortDir: path.resolve(STEPFUN_ROOT, env('ORT_DIR', 'runtime/ort')),
};

/** 嵌入模型标识（写入文档元数据，便于按模型分区检索）。 */
export const MODEL_ID = env('STEPFUN_MODEL_ID', 'Xenova/bge-small-zh-v1.5');
