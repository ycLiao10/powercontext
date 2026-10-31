/**
 * 拉取 Transformers.js 所需的本地模型权重（浏览器端 embedding 推理）。
 *
 * 默认走 hf-mirror.com 镜像（huggingface.co 在本机不可达），镜像同时提供
 * HF API 与文件 CDN。PowerShell 5.1 无法跟随镜像的 308 跳转，因此这里
 * 一律使用 Node 内置的 fetch（自动跟随重定向）。
 *
 * 用法：
 *   node scripts/fetch-model.mjs                       # 三个模型全量
 *   node scripts/fetch-model.mjs --model Xenova/bge-small-zh-v1.5
 *   node scripts/fetch-model.mjs --dry-run              # 只打印计划
 *   node scripts/fetch-model.mjs --force                # 忽略本地已有文件
 */
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { pipeline } from "node:stream/promises";
import { Readable, Transform } from "node:stream";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const stepfunRoot = path.resolve(__dirname, "..");
const DEFAULT_OUT_DIR = path.join(stepfunRoot, "models");
const ENDPOINT = process.env.HF_ENDPOINT || "https://hf-mirror.com";

/** 默认模型清单：中文主模型 + 英文对照 + 轻量基线 */
const DEFAULT_MODELS = [
  "Xenova/bge-small-zh-v1.5",
  "Xenova/bge-small-en-v1.5",
  "Xenova/all-MiniLM-L6-v2",
];

/** 向量维度（与 shared/models.mjs 中的注册表保持一致） */
const DIMENSIONS = {
  "Xenova/bge-small-zh-v1.5": 512,
  "Xenova/bge-small-en-v1.5": 384,
  "Xenova/all-MiniLM-L6-v2": 384,
};

/** 需要下载的文件 -> 精度变体名（null 表示非权重文件） */
const WANTED_FILES = [
  { rfilename: "config.json", variant: null },
  { rfilename: "tokenizer.json", variant: null },
  { rfilename: "tokenizer_config.json", variant: null },
  { rfilename: "special_tokens_map.json", variant: null },
  { rfilename: "onnx/model.onnx", variant: "fp32" },
  { rfilename: "onnx/model_fp16.onnx", variant: "fp16" },
  { rfilename: "onnx/model_int8.onnx", variant: "int8" },
  { rfilename: "onnx/model_quantized.onnx", variant: "quantized" },
];

function parseArgs(argv) {
  const opts = { models: [], outDir: DEFAULT_OUT_DIR, force: false, dryRun: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--model") {
      const v = argv[++i];
      if (!v) throw new Error("--model 需要一个模型 id 参数");
      opts.models.push(v);
    } else if (arg.startsWith("--model=")) {
      opts.models.push(arg.slice("--model=".length));
    } else if (arg === "--out-dir") {
      const v = argv[++i];
      if (!v) throw new Error("--out-dir 需要一个目录参数");
      opts.outDir = path.resolve(v);
    } else if (arg.startsWith("--out-dir=")) {
      opts.outDir = path.resolve(arg.slice("--out-dir=".length));
    } else if (arg === "--force") {
      opts.force = true;
    } else if (arg === "--dry-run") {
      opts.dryRun = true;
    } else if (arg === "--help" || arg === "-h") {
      printHelp();
      process.exitCode = 0;
      return null;
    } else {
      throw new Error(`未知参数：${arg}`);
    }
  }
  if (opts.models.length === 0) opts.models = [...DEFAULT_MODELS];
  return opts;
}

function printHelp() {
  console.log(`用法: node scripts/fetch-model.mjs [选项]

选项:
  --model <hfModelId>   要拉取的模型 id，可重复；默认 ${DEFAULT_MODELS.join(", ")}
  --out-dir <dir>       输出目录，默认 ${DEFAULT_OUT_DIR}
  --force               忽略本地已有文件，重新下载
  --dry-run             只打印下载计划，不拉取任何文件内容
  -h, --help            显示本帮助

环境变量:
  HF_ENDPOINT           镜像地址，默认 ${ENDPOINT}`);
}

function formatBytes(n) {
  if (!Number.isFinite(n)) return "-";
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

function humanSize(n) {
  return `${(n / 1048576).toFixed(1)} MB`;
}

/**
 * 读取模型元信息：siblings 列表 + 仓库 sha（revision）。
 * 带 blobs=true 才会返回每个文件的确切字节数，用于本地跳过与校验。
 */
async function fetchModelInfo(modelId) {
  // 注意：模型 id 里的 "/" 不能被编码成 %2F，镜像会拒绝这种仓库名。
  const url = `${ENDPOINT}/api/models/${modelId}?blobs=true`;
  const res = await fetch(url, { headers: { accept: "application/json" } });
  if (!res.ok) {
    const body = (await res.text().catch(() => "")).slice(0, 200);
    throw new Error(`模型元信息请求失败 ${modelId}: HTTP ${res.status} ${body}`);
  }
  const info = await res.json();
  const siblings = new Map();
  for (const s of info.siblings || []) {
    siblings.set(s.rfilename, {
      size: typeof s.size === "number" ? s.size : null,
      // LFS 文件的 lfs.sha256 是内容 sha256；普通文件的 blobId 是 git blob 的 sha1
      sha256: s.lfs && typeof s.lfs.sha256 === "string" ? s.lfs.sha256 : null,
      blobId: typeof s.blobId === "string" ? s.blobId : null,
    });
  }
  return { revision: info.sha || null, siblings };
}

/**
 * 流式下载单个文件：边下边写盘边算 sha256，不把整个文件读进内存。
 */
async function downloadFile(url, dest, expectedSize) {
  await fsp.mkdir(path.dirname(dest), { recursive: true });
  // 临时文件名带上 pid，避免与并发运行的下载进程互相覆盖
  const tmp = `${dest}.part-${process.pid}`;
  const hash = crypto.createHash("sha256");
  let received = 0;
  let lastRender = 0;
  const started = Date.now();

  const res = await fetch(url, {
    redirect: "follow",
    headers: { "accept-encoding": "identity" },
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  const total = expectedSize ?? Number(res.headers.get("content-length")) ?? 0;

  const render = (force) => {
    const now = Date.now();
    if (!force && now - lastRender < 250) return;
    lastRender = now;
    const secs = Math.max((now - started) / 1000, 0.001);
    const speed = received / secs / 1024 / 1024;
    const pct = total > 0 ? ((received / total) * 100).toFixed(1) : "0.0";
    const line = `    ${path.basename(dest).padEnd(24)} ${humanSize(received).padStart(10)} / ${humanSize(total).padStart(10)}  ${pct.padStart(6)}%  ${speed.toFixed(2)} MB/s`;
    process.stdout.write(`\r${line}`);
  };

  // 用 Transform 边流边算 sha256 / 统计字节，避免把整个文件读进内存，
  // 同时避免先挂 'data' 监听导致 flowing 模式下丢字节。
  const meter = new Transform({
    transform(chunk, _enc, cb) {
      hash.update(chunk);
      received += chunk.length;
      render(false);
      cb(null, chunk);
    },
  });
  await pipeline(Readable.fromWeb(res.body), meter, fs.createWriteStream(tmp));
  render(true);
  process.stdout.write("\n");

  const sha256 = hash.digest("hex");
  const { size } = await fsp.stat(tmp);
  await renameWithRetry(tmp, dest);
  return { bytes: size, sha256 };
}

/**
 * 重命名到最终路径：Windows 上目标文件可能正被其它进程占用（杀毒扫描、
 * 并发下载等），EPERM/EBUSY 属于瞬时错误，重试即可。
 */
async function renameWithRetry(from, to, attempts = 10) {
  for (let i = 0; i < attempts; i += 1) {
    try {
      await fsp.rename(from, to);
      return;
    } catch (err) {
      const transient = err.code === "EPERM" || err.code === "EBUSY" || err.code === "EACCES";
      if (!transient || i === attempts - 1) throw err;
      await new Promise((r) => setTimeout(r, 300 * (i + 1)));
    }
  }
}

/**
 * 计算文件的 sha256 与 git blob sha1。
 * git blob sha1 = sha1("blob <size>\0" + content)，用于校验非 LFS 的小文件。
 */
async function hashFile(dest) {
  const sha256 = crypto.createHash("sha256");
  const gitSha1 = crypto.createHash("sha1");
  const size = (await fsp.stat(dest)).size;
  gitSha1.update(`blob ${size}\0`);
  // 直接消费读流，避免把 Transform 当作 pipeline 终点导致背压死锁
  for await (const chunk of fs.createReadStream(dest)) {
    sha256.update(chunk);
    gitSha1.update(chunk);
  }
  return { sha256: sha256.digest("hex"), gitSha1: gitSha1.digest("hex") };
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (!opts) return; // --help 已处理
  const models = [];

  console.log(`[fetch-model] endpoint = ${ENDPOINT}`);
  console.log(`[fetch-model] out-dir  = ${opts.outDir}`);
  console.log(`[fetch-model] models   = ${opts.models.join(", ")}`);

  // 第一阶段：读取每个模型的元信息与下载计划
  for (const modelId of opts.models) {
    let info;
    try {
      info = await fetchModelInfo(modelId);
    } catch (err) {
      // API 调用失败属于致命错误，直接终止
      throw new Error(`无法获取 ${modelId} 的元信息：${err.message}`);
    }

    const plan = [];
    for (const { rfilename, variant } of WANTED_FILES) {
      const meta = info.siblings.get(rfilename);
      if (!meta) {
        console.warn(`[fetch-model] 跳过 ${modelId} 中不存在的文件：${rfilename}`);
        continue;
      }
      plan.push({ rfilename, variant, size: meta.size, sha256: meta.sha256, blobId: meta.blobId, dest: path.join(opts.outDir, modelId, rfilename) });
    }

    models.push({ modelId, revision: info.revision, plan });
  }

  if (opts.dryRun) {
    console.log("\n[fetch-model] --dry-run：下载计划（不会拉取任何文件内容）");
    for (const m of models) {
      console.log(`\n  模型 ${m.modelId}  revision=${m.revision ?? "(未知)"}`);
      for (const f of m.plan) {
        console.log(`    - ${f.rfilename.padEnd(28)} ${String(f.size ?? "?").padStart(10)} B -> ${f.dest}`);
      }
    }
    const total = models.reduce((sum, m) => sum + m.plan.reduce((s, f) => s + (f.size ?? 0), 0), 0);
    console.log(`\n  合计 ${models.length} 个模型，${models.reduce((s, m) => s + m.plan.length, 0)} 个文件，约 ${formatBytes(total)}`);
    return;
  }

  // 第二阶段：逐个模型下载
  const manifestModels = {};
  for (const m of models) {
    console.log(`\n[fetch-model] === ${m.modelId} (revision ${m.revision ?? "unknown"}) ===`);
    const modelDir = path.join(opts.outDir, m.modelId);
    await fsp.mkdir(modelDir, { recursive: true });

    const files = {};
    const weightBytes = {};
    const downloaded = new Set();

    for (const f of m.plan) {
      const exists = fs.existsSync(f.dest);
      if (exists && !opts.force) {
        const { size } = await fsp.stat(f.dest);
        if (f.size !== null && size === f.size) {
          // 已有文件大小正确时再核对内容哈希，避免把半截文件当成完整文件
          const local = await hashFile(f.dest);
          const shaOk = !f.sha256 || local.sha256 === f.sha256;
          const blobOk = f.sha256 ? true : !f.blobId || local.gitSha1 === f.blobId;
          if (shaOk && blobOk) {
            console.log(`    ${f.rfilename.padEnd(28)} 已存在且校验一致（${formatBytes(size)}），跳过`);
            files[f.rfilename] = { bytes: size, sha256: local.sha256 };
            if (f.variant) {
              weightBytes[f.variant] = size;
              downloaded.add(f.variant);
            }
            continue;
          }
          console.log(`    ${f.rfilename.padEnd(28)} 本地 sha256 与仓库不一致，重新下载`);
        } else {
          console.log(`    ${f.rfilename.padEnd(28)} 本地文件大小不符（${size} != ${f.size ?? "?"}），重新下载`);
        }
      } else if (exists && opts.force) {
        console.log(`    ${f.rfilename.padEnd(28)} --force，重新下载`);
      }

      const url = `${ENDPOINT}/${m.modelId}/resolve/${m.revision ?? "main"}/${f.rfilename}`;
      try {
        const { bytes, sha256 } = await downloadFile(url, f.dest, f.size);
        files[f.rfilename] = { bytes, sha256 };
        if (f.variant) {
          weightBytes[f.variant] = bytes;
          downloaded.add(f.variant);
        }
        if (f.sha256 && f.sha256 !== sha256) {
          console.warn(`[fetch-model] 警告：${f.rfilename} 的 sha256 与仓库记录不一致（本地 ${sha256} / 远端 ${f.sha256}）`);
        }
      } catch (err) {
        // 单个文件失败不中断整体流程
        console.warn(`[fetch-model] 警告：下载 ${m.modelId}/${f.rfilename} 失败：${err.message}（继续）`);
      }
    }

    const fp32 = weightBytes.fp32;
    const compressionRatio = {};
    if (fp32) {
      for (const variant of ["fp16", "int8", "quantized"]) {
        if (weightBytes[variant]) {
          compressionRatio[variant] = Number((fp32 / weightBytes[variant]).toFixed(2));
        }
      }
    }

    manifestModels[m.modelId] = {
      revision: m.revision,
      dimension: DIMENSIONS[m.modelId] ?? null,
      files,
      weightBytes,
      compressionRatio,
    };
  }

  // 第三阶段：写 manifest.json
  const manifest = {
    generatedAt: new Date().toISOString(),
    endpoint: ENDPOINT,
    models: manifestModels,
  };
  await fsp.mkdir(opts.outDir, { recursive: true });
  await fsp.writeFile(path.join(opts.outDir, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, "utf8");

  // 最终表格
  console.log("\n[fetch-model] 下载完成，体积对比：");
  console.log(`  ${"模型".padEnd(28)} ${"fp32".padStart(11)} ${"fp16".padStart(11)} ${"int8".padStart(11)} ${"fp32/int8".padStart(11)}`);
  console.log(`  ${"-".repeat(28)} ${"-".repeat(11)} ${"-".repeat(11)} ${"-".repeat(11)} ${"-".repeat(11)}`);
  for (const [modelId, entry] of Object.entries(manifestModels)) {
    const w = entry.weightBytes;
    const ratio = entry.compressionRatio.int8 ? `${entry.compressionRatio.int8.toFixed(2)}x` : "-";
    console.log(
      `  ${modelId.padEnd(28)} ${formatBytes(w.fp32 ?? 0).padStart(11)} ${formatBytes(w.fp16 ?? 0).padStart(11)} ${formatBytes(w.int8 ?? 0).padStart(11)} ${ratio.padStart(11)}`,
    );
  }
  console.log(`\n[fetch-model] manifest: ${path.join(opts.outDir, "manifest.json")}`);
}

main().catch((err) => {
  // 用 exitCode 而非 process.exit()：后者在 undici 保持连接未关闭时
  // 会触发 Windows 上的 libuv assertion 失败。
  console.error(`[fetch-model] 失败：${err.stack || err.message}`);
  process.exitCode = 1;
});
