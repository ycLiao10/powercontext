/**
 * SQL 日志：把**实际执行的 SQL 原文**落盘，供评审直接确认
 * 「混合检索是在 seekdb 内一条 SQL 完成的」。
 *
 * 开关：
 *   SQL_LOG_ENABLED=0|1   （默认 1）
 *   SQL_LOG_FILE=../evidence/sql-log.txt
 *
 * 日志格式带分隔标记，便于 grep 与人工阅读。
 */

import fs from 'node:fs';
import path from 'node:path';
import { config } from './config.js';

const SEPARATOR = '='.repeat(78);

function ensureDir(filePath) {
  const dir = path.dirname(filePath);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

/** 向量字面量在日志里只保留开头一小截，避免 512 维参数淹没日志。 */
function formatParam(p) {
  if (typeof p !== 'string') return JSON.stringify(p);
  if (/^\[[-\d.,\s]+\]$/.test(p) && p.length > 120) {
    const nums = p.slice(1, -1).split(',');
    const head = nums.slice(0, 6).join(',');
    return `[${head}, …共 ${nums.length} 维]`;
  }
  return p;
}

/**
 * 记录一次 SQL 执行。
 * @param {string} label   标签，例如 'hybrid' / 'vector' / 'fulltext' / 'insert'
 * @param {string} sql     实际执行的 SQL 原文（含占位符）
 * @param {Array}  params  绑定参数
 * @param {{durationMs?:number, rows?:number, note?:string}} meta
 */
export function logSql(label, sql, params = [], meta = {}) {
  if (!config.sqlLog.enabled) return;

  const timestamp = new Date().toISOString();
  const lines = [];
  lines.push(SEPARATOR);
  lines.push(`[${timestamp}] label=${label}`);
  if (meta.note) lines.push(`note: ${meta.note}`);
  if (typeof meta.durationMs === 'number') lines.push(`duration_ms: ${meta.durationMs}`);
  if (typeof meta.rows === 'number') lines.push(`rows: ${meta.rows}`);
  lines.push('--- SQL ---');
  lines.push(sql.trim());
  lines.push('--- PARAMS ---');
  lines.push(
    params.length
      ? params.map((p, i) => `  [${i}] ${formatParam(p)}`).join('\n')
      : '  (none)',
  );
  lines.push(SEPARATOR);
  lines.push('');

  try {
    ensureDir(config.sqlLog.file);
    fs.appendFileSync(config.sqlLog.file, lines.join('\n'), 'utf8');
  } catch (err) {
    // 日志失败不能影响主流程
    console.error(`[sql-log] 写入失败：${err.message}`);
  }
}

/**
 * 带计时与日志的查询执行器。
 * @param {import('mysql2/promise').Connection} conn
 */
export async function loggedQuery(conn, label, sql, params = [], note = '') {
  const started = process.hrtime.bigint();
  const [rows] = await conn.query(sql, params);
  const durationMs = Number(process.hrtime.bigint() - started) / 1e6;
  logSql(label, sql, params, { durationMs, rows: Array.isArray(rows) ? rows.length : 0, note });
  return rows;
}

/** 清空日志文件（冒烟测试前调用，保证 evidence 里是本次运行的样本）。 */
export function resetSqlLog() {
  if (!config.sqlLog.enabled) return;
  ensureDir(config.sqlLog.file);
  fs.writeFileSync(config.sqlLog.file, '', 'utf8');
}

export const sqlLogPath = config.sqlLog.file;
