/**
 * 建库建表（幂等）。
 *
 * 用法：node scripts/init-db.js [--reset]
 *   --reset  建表后清空表内容
 *
 * 建表 DDL 见 server/db.js 的 createTableSql()，关键点：
 *   - FULLTEXT INDEX ... WITH PARSER ngram
 *   - VECTOR INDEX ... sync_mode=IMMEDIATE
 */

import { connect, ensureSchema, countDocuments, truncateDocuments, createTableSql } from '../server/db.js';

const reset = process.argv.includes('--reset');

const conn = await connect();
try {
  await ensureSchema(conn);
  console.log('✓ 库与表已就绪');
  console.log('DDL:');
  console.log(createTableSql());

  if (reset) {
    await truncateDocuments(conn);
    console.log('✓ 已清空表内容');
  }
  console.log(`✓ 当前文档数 = ${await countDocuments(conn)}`);
} finally {
  await conn.end().catch(() => {});
}
