/**
 * 合并多浏览器（--tag）的基准结果，产出最终的 evidence/bench.json 与 evidence/bench-raw.json。
 *
 * 用法：
 *   node scripts/bench-browser.mjs --reps 5 --tag edge   --browser "<edge 路径>"
 *   node scripts/bench-browser.mjs --reps 5 --tag chrome --browser "<chrome 路径>"
 *   node scripts/merge-bench.mjs
 *
 * 合并规则：
 *   - runs[]：按浏览器顺序平铺，每条保留 browser 字段与原始 reps 数据
 *   - configs[]：同样是平铺列表，每条带 browser / browserVersion 字段
 *   - 同一浏览器同一配置出现多次时，后一次覆盖前一次（取最后一次完整运行）
 */

import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STEPFUN_ROOT = path.resolve(__dirname, '..');
const EVIDENCE = path.join(STEPFUN_ROOT, 'evidence');

/** tag 顺序决定最终文档里的展示顺序。 */
const TAGS = ['edge', 'chrome'];

const rawRuns = [];
const configs = [];
const browsers = {};

for (const tag of TAGS) {
  const rawPath = path.join(EVIDENCE, `bench-raw.${tag}.json`);
  const sumPath = path.join(EVIDENCE, `bench.${tag}.json`);
  if (!fs.existsSync(rawPath) || !fs.existsSync(sumPath)) {
    console.log(`跳过 ${tag}：缺少 ${path.basename(rawPath)} / ${path.basename(sumPath)}`);
    continue;
  }
  const raw = JSON.parse(fs.readFileSync(rawPath, 'utf8'));
  const sum = JSON.parse(fs.readFileSync(sumPath, 'utf8'));
  browsers[tag] = {
    executable: raw.browser ?? null,
    version: raw.browserVersion ?? null,
    userAgent: raw.userAgent ?? null,
    reps: raw.reps ?? null,
    host: raw.host ?? null,
  };
  rawRuns.push(...(raw.runs ?? []).map((r) => ({ ...r, tag })));
  configs.push(...(sum.configs ?? []).map((c) => ({ ...c, tag })));
}

/** 浏览器可执行文件的真实版本号（从文件版本信息读）。 */
function exeVersion(exe) {
  if (!exe || !fs.existsSync(exe)) return null;
  try {
    // Windows 下用 PowerShell 读版本信息；非 Windows 直接返回 null
    if (process.platform !== 'win32') return null;
    const out = execFileSync(
      'powershell',
      [
        '-NoProfile',
        '-Command',
        `(Get-Item -LiteralPath '${exe.replace(/'/g, "''")}').VersionInfo.ProductVersion`,
      ],
      { encoding: 'utf8' },
    );
    return out.trim() || null;
  } catch {
    return null;
  }
}

for (const tag of TAGS) {
  const rawPath = path.join(EVIDENCE, `bench-raw.${tag}.json`);
  if (!fs.existsSync(rawPath)) continue;
  const raw = JSON.parse(fs.readFileSync(rawPath, 'utf8'));
  // 记录可执行文件路径 + 版本（版本从文件元信息读）
  const exe = raw.browserExe ?? null;
  browsers[tag].executable = exe;
  browsers[tag].version = exe ? exeVersion(exe) : null;
}

/** fp32 基线的压缩比（同一浏览器内计算）。 */
for (const tag of TAGS) {
  const fp32 = configs.find((c) => c.tag === tag && c.dtype === 'fp32' && c.status === 'ok');
  for (const c of configs) {
    if (c.tag !== tag || c.status !== 'ok') continue;
    c.compressionVsFp32 = fp32?.modelBytes
      ? Number((fp32.modelBytes / c.modelBytes).toFixed(2))
      : null;
  }
}

const generatedAt = new Date().toISOString();

const rawDoc = {
  generatedAt,
  browsers,
  note:
    '每个指标的原始重复测量都在 runs[].page.reps 里；中位数汇总见 bench.json。' +
    'runs[].tag 表示该次测量使用的浏览器（edge / chrome）。',
  runs: rawRuns,
};

const summaryDoc = {
  generatedAt,
  model: 'Xenova/bge-small-zh-v1.5',
  browsers,
  methodology: {
    corpus: '36 条语料（data/corpus.jsonl）',
    queries: '25 条固定查询（data/queries.json，人工标注 relevant / graded）',
    median: '每个指标重复 5 次取中位数',
    coldLoad: '全新浏览器 profile 下的首次 pipeline() 创建，含权重下载',
    warmLoad: '同一页面内第二次创建 pipeline（权重已在浏览器 Cache）',
    vectorize: '36 条语料一次批量向量化（标题+正文，与生产路径一致）',
    writeAndIndex: 'POST /api/documents，代理侧计时含 INSERT 与索引刷新（sync_mode=IMMEDIATE）',
    seekdbSearch: '代理返回的 tookMs：服务端执行混合检索 SQL 的耗时',
    endToEndQuery: '查询向量计算 → 混合检索 → 结果渲染到 DOM（双 rAF）',
    quality: '用同一组向量跑固定评测集，Recall@5 / nDCG@5 公式与 scripts/search.js 一致',
    modelSize: '代理 HEAD 返回的 Content-Length，即模型文件在磁盘上的实际字节数',
    compression: 'compressionVsFp32 = fp32 字节数 / 该配置字节数',
  },
  configs,
};

fs.writeFileSync(path.join(EVIDENCE, 'bench-raw.json'), JSON.stringify(rawDoc, null, 2), 'utf8');
fs.writeFileSync(path.join(EVIDENCE, 'bench.json'), JSON.stringify(summaryDoc, null, 2), 'utf8');

console.log(`✓ evidence/bench-raw.json（${rawRuns.length} 组运行）`);
console.log(`✓ evidence/bench.json（${configs.length} 条配置汇总）`);

console.log('\n配置            浏览器   量化  后端    batch  MB     压缩比  条/秒    R@5     nDCG@5');
for (const c of configs) {
  const ok = c.status === 'ok';
  console.log(
    [
      c.id.padEnd(15),
      String(c.tag).padEnd(8),
      String(c.dtype).padEnd(5),
      String(c.device).padEnd(7),
      String(c.batch).padEnd(6),
      ok ? String(c.modelMB) : 'N/A',
      ok && c.compressionVsFp32 ? `${c.compressionVsFp32}x` : 'N/A',
      ok ? String(c.vectorize36?.docsPerSec) : 'N/A',
      ok ? String(c.recallAt5) : 'N/A',
      ok ? String(c.ndcgAt5) : 'N/A',
    ].join('  '),
  );
}
