# stepfun × seekdb：端侧 AI 推理与混合检索

> OceanBase 大赛「seekdb × 阶跃星辰：端侧 AI 推理性能优化与 seekdb 混合检索」赛题作品。

## 合规声明

生产路径（`web/` + `server/`）的文档向量与查询向量**全部在浏览器内计算**，不调用任何云端推理 API。
`scripts/` 下的向量化脚本仅用于**离线基线对比与质量评测**，不属于生产检索链路。
逐条对照见 [`evidence/compliance.md`](./evidence/compliance.md)。

---

> 文档与查询的 embedding **全部在浏览器内计算**（WebGPU 优先，自动降级 WASM，也可手动切换），
> 向量与原文、元数据一起存入 **seekdb**，
> **混合检索由 seekdb 内的一条 SQL 完成**（向量 HNSW + 全文 ngram，RRF 融合）。

---

## 1. 评审 5 分钟上手

**三步跑起来**（假设已装 Node.js ≥ 20、已有一份 seekdb v1.4.0.0 二进制）：

```bash
# ① 起数据库（:2881）
cd stepfun && bash server/start-seekdb.sh

# ② 起薄代理（:8787）+ 建表导入语料
npm install --legacy-peer-deps
node scripts/init-db.js --reset
node scripts/import-corpus.js --reset
node server/proxy.js &

# ③ 起前端（:5173），浏览器打开 http://127.0.0.1:5173/
cd web && npm install --legacy-peer-deps --ignore-scripts && npm rebuild esbuild
npm run dev
```

打开页面后：

1. 右上角徽标显示当前推理后端（`WebGPU` / `WASM`），**可点击手动切换**「自动 / WebGPU / WASM」；
2. 左栏粘贴一段文本或选文件 → 点「向量化并导入」（向量在浏览器内算，带进度条）；
3. 右栏输入查询 → 点「检索」，看结果卡片上的 `hybrid_score` 与「全文命中 / 向量命中」rank。

**一键验证**（真实浏览器驱动，Chrome / Edge 都测过）：

```bash
node scripts/e2e-browser-check.mjs --url http://127.0.0.1:5173/ --reset
```

它会自动完成：读后端徽标 → 导入 10 条 → 混合检索 → 再导入 1 条 → **立刻**检索到新增文档 → 截图。

**一分钟看懂实测数据**：

| | |
|---|---|
| 量化 | q8 把权重从 90.46 MB 压到 **22.90 MB（3.95×）**，Recall@5 不降（0.9067） |
| 端侧 | 核显上 **q8 + WASM 端到端 60.9 ms**，比 q8 + WebGPU（123.3 ms）快 2 倍 |
| seekdb | 混合检索 **一条 SQL**，检索 4.7~5.4 ms，写入+索引刷新 38~48 ms |
| 增量 | `sync_mode=IMMEDIATE`，写入即可检索（默认 ASYNC 会查不到） |

完整数据见 [`evidence/bench.json`](./evidence/bench.json)，结论见 [`tech-notes.md`](./tech-notes.md)。

---

## 2. 架构

```
┌─────────────────────────────────────────────────────────────┐
│  浏览器（Chrome / Edge 最新版）                                │
│                                                             │
│   web/  ── Transformers.js 4.3.2                            │
│            device: 'webgpu'  ──▶ 不支持 ──▶ 'wasm'           │
│            ★ 文档向量、查询向量都在这里算，浏览器本地，零云端   │
│            ★ 右上角徽标可手动切 自动 / WebGPU / WASM          │
│                                                             │
│   fetch('/api/documents') / fetch('/api/search')            │
└──────────────┬──────────────────────────────────────────────┘
               │ HTTP（浏览器不能直连 MySQL 协议端口）
               ▼
┌─────────────────────────────────────────────────────────────┐
│  server/proxy.js —— 薄 HTTP 代理（Node，mysql2）             │
│                                                             │
│   只转发，不推理、不缓存检索结果、不预计算向量                  │
└──────────────┬──────────────────────────────────────────────┘
               │ MySQL 协议
               ▼
┌─────────────────────────────────────────────────────────────┐
│  本地 seekdb server v1.4.0.0（:2881）                       │
│                                                             │
│   documents 表                                               │
│     ├─ FULLTEXT INDEX idx_fts (content) WITH PARSER ngram    │
│     └─ VECTOR  INDEX idx_vec (embedding) WITH(...,          │
│                      sync_mode=IMMEDIATE)                   │
│                                                             │
│   混合检索 = 一条 SQL：                                      │
│     MATCH(content) AGAINST(...)  ⨝  cosine_distance(...)     │
│     ORDER BY hybrid_score DESC                               │
└─────────────────────────────────────────────────────────────┘
```

**为什么要有代理层**：浏览器既不能发 MySQL 协议（请求头不可自定义），
官方 `seekdb` JS SDK 又依赖 `mysql2`（Node-only，浏览器跑不了）。
题目 4.1 允许「随应用提供本地 server 的启动脚本与连接配置」。

---

## 3. 目录结构

```
stepfun/
├── README.md              本文件（前三节 = 评审 5 分钟上手，附录 A = 技术细节）
├── tech-notes.md          一页技术说明（模型与量化 / 端侧运行 / seekdb 使用 / 状态与优化）
├── package.json           根 workspace（mysql2 + transformers.js）
├── web/                   前端应用：浏览器内本地向量化 + 检索界面
│   ├── index.html
│   ├── bench.html         页内基准测量页（供 bench-browser.mjs 驱动）
│   ├── vite.config.js     Vite 配置（transformers.js 必须排除依赖预打包）
│   ├── scripts/
│   │   ├── prepare-ort-assets.mjs  复制 onnxruntime-web 运行时到 runtime/ort/
│   │   └── download-model.mjs       预下载模型权重到 models/（本地离线镜像，支持 --dtypes）
│   └── src/
│       ├── main.jsx
│       ├── App.jsx       左右两栏：导入 / 检索 + 右上角后端徽标下拉
│       ├── bench.js      页内基准测量（window.__benchRun）
│       ├── styles.css
│       └── lib/
│           ├── browser-embedder.js  ★ 生产路径的浏览器内向量化（WebGPU → WASM 降级）
│           ├── backend-pref.js      后端偏好（localStorage 持久化 + 回落逻辑）
│           ├── runtime-env.js       模型/运行时/代理地址（生产与基准共用）
│           ├── api.js               代理调用封装
│           └── parse.js             文本 / JSONL 解析
├── server/
│   ├── config.js          环境变量配置，无任何真实密钥
│   ├── db.js              建表 DDL + 三种检索 SQL + tablet 退避重试（核心）
│   ├── sql-log.js         SQL 日志开关
│   ├── proxy.js           薄 HTTP 代理（转发 + 模型/ORT 静态文件）
│   ├── smoke.js           冒烟测试（阶段二验收）
│   └── start-seekdb.sh    本地 seekdb 启动脚本
├── scripts/
│   ├── init-db.js         建库建表
│   ├── import-corpus.js   导入样例语料
│   ├── search.js          执行检索 / 跑固定评测集
│   ├── verify-sync-mode.js  索引同步模式对照实验
│   ├── gen-sql-log-samples.js 生成 SQL 日志样例
│   ├── e2e-browser-check.mjs    ★ 浏览器端到端验收（真实 Chrome/Edge，CDP 驱动）
│   ├── backend-switch-check.mjs ★ 后端手动切换验收（下拉 / 持久化 / 回落）
│   ├── bench-browser.mjs        ★ 性能基准（7 配置 × 5 次 × 2 浏览器）
│   ├── merge-bench.mjs          合并多浏览器基准结果
│   ├── capture-ui-shot.mjs      抓「导入进行中」界面截图
│   └── offline-eval-embedder.js 【离线评测专用】向量化工具，非生产链路
├── data/
│   ├── corpus.jsonl       36 条测试语料（80% 中文 / 20% 中英混合）
│   └── queries.json       25 条查询 + 人工标注相关性
├── models/                本地模型镜像（脚本生成，不入库）
├── runtime/ort/           onnxruntime-web 运行时（脚本生成，不入库）
└── evidence/
    ├── compliance.md      ★ 赛题 2.5 通用要求逐条对照 + 验证方式
    ├── bench.json         ★ 性能与质量汇总（14 条配置：2 浏览器 × 7 组）
    ├── bench-raw.json     ★ 原始逐次测量数据
    ├── sql-log.txt        实际执行的 SQL 原文（三种模式样例）
    ├── quality-hybrid.json  检索质量（Recall@5 / nDCG@5）
    └── sync-mode-comparison.json  索引同步模式对照实验结果
```

---

## 4. 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Node.js | ≥ 20 | 服务端脚本与代理 |
| seekdb | v1.4.0.0 | 本地数据库，需自带二进制 |
| 浏览器 | Chrome / Edge 最新版 | WebGPU 推理；无 WebGPU 时自动降级 WASM |

### 4.1 浏览器要求与实测版本

**Chrome 156.0.8078.80 与 Edge 156.0.4314.50 均实测通过**（WebGPU 与 WASM 降级两条路径，
`scripts/e2e-browser-check.mjs` 全部断言通过，见第 4.8 节）。

| 浏览器 | 版本 | WebGPU 路径 | WASM 降级路径 |
|---|---|---|---|
| Chrome | 156.0.8078.80 | ✅ 通过 | ✅ 通过 |
| Edge | 156.0.4314.50 | ✅ 通过 | ✅ 通过 |

最低要求：

- **Chrome 113+ / Edge 113+**：`navigator.gpu` 的可用版本下限。
- 建议 **Chrome/Edge 131+**：Transformers.js 4.x 的 WebGPU 后端依赖较新的
  `GPUBuffer` / `shader-f16` 能力。
- 无 WebGPU 的环境（老浏览器、`--disable-gpu`、虚拟机无 GPU）会自动降级到 WASM，
  **核心流程（导入 → 向量化 → 写入 → 混合检索 → 增量可见）完全可用**，只是向量化更慢。
- 两条路径都不支持时（浏览器 < 100），页面会显示明确错误提示，不会静默失败。

验证浏览器版本：

```bash
# Windows
powershell -c "(Get-Item '%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe').VersionInfo.ProductVersion"
powershell -c "(Get-Item 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe').VersionInfo.ProductVersion"
```

国内网络环境提示：

- npm 建议用镜像：`npm config set registry https://registry.npmmirror.com`
- HuggingFace 权重建议用镜像：`export HF_ENDPOINT=https://hf-mirror.com`
- `onnxruntime-node` 的原生二进制默认从 GitHub 下载，GitHub 不可达时可手动安装：
  ```bash
  # 见 README「离线环境」小节
  ```

---

## 5. 快速开始

### 5.1 启动本地 seekdb

```bash
cd stepfun
bash server/start-seekdb.sh
# 若 seekdb 不在默认位置，先指定路径：
# SEEKDB_BIN=/path/to/seekdb bash server/start-seekdb.sh
```

默认连接参数（`server/config.js`，全部可用环境变量覆盖）：

```
host=127.0.0.1  port=2881  user=root  password=(空)
database=stepfun_kb  table=documents
```

### 5.2 安装依赖

```bash
cd stepfun
npm install
```

### 5.3 冒烟测试

```bash
node server/smoke.js
```

预期输出：写入 10 条 → 三种检索模式各返回结果 → 新增 1 条后**立即**可检索。

### 5.4 建库建表 + 导入语料

```bash
node scripts/init-db.js --reset
node scripts/import-corpus.js --reset
```

### 5.5 检索

```bash
node scripts/search.js "缓存雪崩和穿透怎么处理"                # 混合检索
node scripts/search.js "向量索引" --mode=fulltext             # 纯全文
node scripts/search.js "缓存雪崩" --mode=vector                # 纯向量
node scripts/search.js --eval                                  # 固定评测集 Recall@5 / nDCG@5
```

### 5.6 启动代理

```bash
node server/proxy.js
# 监听 http://127.0.0.1:8787
```

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 探活 + 当前文档数 + 模型信息 |
| POST | `/api/schema` | 建库建表（幂等） |
| POST | `/api/documents` | 写入文档（**向量由浏览器算好随请求提交**） |
| POST | `/api/search` | 检索，`mode` = `hybrid` / `vector` / `fulltext` |
| DELETE | `/api/documents` | 清空知识库 |
| GET | `/models/*` | 本地模型镜像（静态文件，缺失返回 404） |
| GET | `/ort/*` | onnxruntime-web 运行时（静态文件） |

### 5.7 启动前端（浏览器内本地向量化）

```bash
cd stepfun/web
npm install --legacy-peer-deps --ignore-scripts   # onnxruntime-node 的 postinstall 需要 GitHub，不装它
npm rebuild esbuild
npm run dev        # 自动执行 prepare:ort + prepare:model，然后起 vite
```

打开 http://127.0.0.1:5173/ 即可。右上角常驻显示**当前推理后端徽标**（`WebGPU` / `WASM`），
**点击徽标可手动切换**「自动 / WebGPU / WASM」。

| URL 参数 | 作用 |
|---|---|
| `?backend=wasm` | 强制 WASM 后端（等价于关掉 WebGPU） |
| `?backend=webgpu` | 强制 WebGPU 后端 |

环境变量（都有默认值）：`VITE_PROXY_URL`（默认 `http://127.0.0.1:8787`）、
`VITE_HF_ENDPOINT`（默认走本地镜像）、`VITE_ORT_WASM_PATH`。

**后端选择的三层优先级**：

1. URL 参数 `?backend=webgpu|wasm`（显式一次性覆盖，测试脚本用）
2. `localStorage['stepfun:backend-pref']`（界面上手动选的选择，**刷新后保持**）
3. 默认「自动」：先 `navigator.gpu.requestAdapter()` 探测，可用就用 WebGPU，否则 WASM

**手动切换**：右上角徽标点开下拉，三选一。选了 **WebGPU 但设备不支持**时会自动**回落到「自动」**
并在页面顶部提示原因，不会把你卡在一个用不了的选择上。

**为什么默认仍然是 WebGPU 优先**：赛题 2.2 要求优先使用 WebGPU，所以「自动」= WebGPU 优先。
但实测（Iris Xe 核显 + q8）WASM 端到端更快（60.9 ms vs 123.3 ms），
所以把选择权交给用户——有独显就用 WebGPU，核显建议切 WASM。详见 `tech-notes.md` 4.1。

**降级是怎么做的**：先用 `navigator.gpu.requestAdapter()` 探测能不能拿到 GPU 适配器，
拿不到就直接用 WASM 后端。
（不能写成「先试 webgpu、catch 到异常再试 wasm」——transformers.js 内部用
`webInitChain = webInitChain.then(load)` 串行化建会话，首次失败后这条链会变成 rejected，
后续任何一次 `device:'wasm'` 都会复用同一个错误，根本不会真的降级。详见 `tech-notes.md`。）

### 5.8 浏览器端到端验收

```bash
# 需要本地 Chrome / Edge；--disable-gpu 用来模拟「没有 WebGPU」的环境
# 浏览器路径可自动探测，也可用 --browser（或旧名 --edge）显式指定
node scripts/e2e-browser-check.mjs --url http://127.0.0.1:5173/ --reset
node scripts/e2e-browser-check.mjs --url http://127.0.0.1:5173/ --reset --disable-gpu

# 显式指定浏览器（Windows 按用户安装的 Chrome 路径）
node scripts/e2e-browser-check.mjs --url http://127.0.0.1:5173/ --reset \
     --browser "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
```

脚本用 CDP 驱动真实浏览器，自动完成：读后端徽标 → 导入 10 条 → 混合检索 →
再导入 1 条 → **立刻**检索到新增文档 → 截图。

**后端手动切换的验收**：

```bash
# 正常路径：切 WASM → 刷新后仍是 WASM；切 WebGPU → 生效
node scripts/backend-switch-check.mjs --url http://127.0.0.1:5173/

# 回落路径：模拟「拿不到 GPU 适配器」→ 选 WebGPU 应回落到「自动」并提示原因
node scripts/backend-switch-check.mjs --url http://127.0.0.1:5173/ --mock-no-webgpu
```

生成一张「导入进行中」的界面证据图（进度条可见）：

```bash
node scripts/capture-ui-shot.mjs --url http://127.0.0.1:5173/ \
     --file ./data/corpus.jsonl --out /tmp/ui-progress.png
```

### 5.9 性能与质量基准

```bash
# 单浏览器跑全部 7 组配置（量化档位 × 后端 × batch），每组重复 5 次取中位数
node scripts/bench-browser.mjs --url http://127.0.0.1:5173/ --reps 5 --tag edge \
     --browser "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

node scripts/bench-browser.mjs --url http://127.0.0.1:5173/ --reps 5 --tag chrome \
     --browser "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"

# 合并成最终的 evidence/bench.json 与 evidence/bench-raw.json
node scripts/merge-bench.mjs
```

产物：

| 文件 | 内容 |
|---|---|
| `evidence/bench.json` | 汇总表：每组配置的模型体积、压缩比、各阶段耗时中位数、Recall@5 / nDCG@5 |
| `evidence/bench-raw.json` | 原始数据：每组配置每次重复的逐项测量 |

复现前需要先备好多档权重（默认只下 q8）：

```bash
cd web
node scripts/download-model.mjs --dtypes=q8,fp32,fp16
```

---

## 6. SQL 日志开关

代理与脚本会把**实际执行的 SQL 原文**写入 `evidence/sql-log.txt`：

```bash
SQL_LOG_ENABLED=0 node server/proxy.js          # 关闭
SQL_LOG_FILE=../evidence/my-log.txt node ...    # 换路径
```

生成三份评审样例：

```bash
node scripts/gen-sql-log-samples.js
grep -A 40 "label=hybrid" evidence/sql-log.txt
```

样例覆盖：`insert`（写入）、`vector`（纯向量）、`fulltext`（纯全文）、`hybrid`（混合）。

---

## 7. 索引同步模式与增量可见性（重要）

**一句话：HNSW 向量索引默认 `SYNC_MODE=ASYNC`，会让「刚写入的文档立刻查不到」；
必须显式配置 `sync_mode=IMMEDIATE`。**

这是本题最有价值的工程发现，一页速览见 [`tech-notes.md`](./tech-notes.md) 第 3 节，
完整复现步骤与四种查询形态的对照见 [附录 A.3](#a3已知限制与坑)。

对照实验可一键复现：

```bash
node scripts/verify-sync-mode.js
```

实测结果（3 条文档，2 张结构相同的表，唯一差别是 sync_mode）：

| 查询形态 | ASYNC（默认） | IMMEDIATE |
|---|---|---|
| 全文过滤 + `APPROXIMATE` 向量排序（单扫描） | **0 / 3** | 3 / 3 |
| 全文过滤 + 精确向量排序（单扫描） | 3 / 3 | 3 / 3 |
| 两分支 RRF 融合 | 3 / 3（但 `vector_rank` 全为 0，排序被拉低） | 3 / 3 |
| 纯向量检索（`APPROXIMATE`） | **0 / 3** | 3 / 3 |

---

## 8. 配置项

全部来自环境变量，**不提交任何真实密钥**：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SEEKDB_HOST` | `127.0.0.1` | seekdb 主机 |
| `SEEKDB_PORT` | `2881` | seekdb 端口 |
| `SEEKDB_USER` | `root` | seekdb 用户 |
| `SEEKDB_PASSWORD` | （空） | seekdb 密码 |
| `SEEKDB_DATABASE` | `stepfun_kb` | 业务库名 |
| `SEEKDB_TABLE` | `documents` | 文档表名 |
| `PROXY_HOST` | `127.0.0.1` | 代理监听地址 |
| `PROXY_PORT` | `8787` | 代理端口 |
| `SQL_LOG_ENABLED` | `1` | 是否记录 SQL 原文 |
| `SQL_LOG_FILE` | `evidence/sql-log.txt` | SQL 日志路径 |
| `VECTOR_DIM` | `512` | 向量维度（bge-small-zh-v1.5） |
| `STEPFUN_MODEL_ID` | `Xenova/bge-small-zh-v1.5` | 嵌入模型 |
| `HF_ENDPOINT` | 未设置 | HuggingFace 镜像（如 `https://hf-mirror.com`） |
| `MODEL_DIR` | `models` | 本地模型镜像目录（代理 `GET /models/*`） |
| `ORT_DIR` | `runtime/ort` | onnxruntime-web 运行时目录（代理 `GET /ort/*`） |

前端侧（`web/`，构建期注入）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VITE_PROXY_URL` | `http://127.0.0.1:8787` | 薄代理地址 |
| `VITE_HF_ENDPOINT` | 未设置 | 权重源；不设时优先用代理的本地镜像，取不到再退回 `https://hf-mirror.com/` |
| `VITE_ORT_WASM_PATH` | `<proxy>/ort/` | onnxruntime-web 运行时地址 |

---

## 9. 合规性对照

逐条对照与可复现的验证方式见 **[`evidence/compliance.md`](./evidence/compliance.md)**。速览：

| 赛题约束 | 本作品的做法 |
|---|---|
| 禁止调用云端推理 API | embedding 由浏览器内 Transformers.js 本地计算；代理无推理代码，权重走本地镜像可完全离线 |
| 禁止服务端预计算向量或检索结果再回放 | 代理无缓存层；检索每次由 seekdb 现场执行 SQL，SQL 原文落盘到 `evidence/sql-log.txt` |
| 向量与文档存储、检索必须走 seekdb | 表 `documents` 存原文 + 元数据 + `VECTOR` 列，全文/向量索引同表；前端不存向量副本 |
| 增量导入：写入后刷新即可检索 | 向量索引建为 `sync_mode=IMMEDIATE`，写入即可见（已验证） |
| 不提交真实密钥 | 全部配置走环境变量 + 占位默认值 |

---

## 10. 已知限制

### 10.1 功能与部署

| 限制 | 说明 |
|---|---|
| **必须自带 seekdb 二进制** | 仓库不含数据库二进制，需自行编译或获取 v1.4.0.0 |
| **代理是单进程 Node 服务** | 未做鉴权、限流、并发压测；仅用于本地开发与评审演示 |
| **代理与 seekdb 需同机或同网络** | 浏览器不能直连 MySQL 协议端口（见 [附录 A.1](#a1选型理由)） |
| **只在本机 Windows + WSL 环境验证过** | Linux / macOS 未实测；路径处理按 POSIX 写死，Windows 侧脚本有 `.sh` / `.ps1` 混用 |
| **无多用户与权限模型** | 知识库是单库单表，没有用户/会话隔离 |

### 10.2 性能

| 限制 | 说明 |
|---|---|
| **核显上 WebGPU 不一定快于 WASM** | 本机为集成显卡，量化模型（q8）在 WebGPU 上**反而更慢**——小模型的调度与数据上传开销占主导，详见 `tech-notes.md` 的实测数据与解释 |
| **fp32 权重在 WebGPU 上很快、fp16 更快，但体积翻倍** | 追求吞吐可用 fp32/fp16 + WebGPU；追求体积与离线可用性用 q8 + WASM |
| **batch 增大不一定更快** | 核显上 batch=16 常比 batch=8 慢，实测数据见 `evidence/bench.json` |
| **冷启动含权重下载** | 首次访问需要下载模型权重（q8 约 23 MB / fp32 约 90 MB），走本地代理镜像，局域网内约百毫秒级 |
| **浏览器端向量化受设备影响大** | 同一台机器不同浏览器、不同 GPU 状态差异明显，实测数据按浏览器分别记录 |

### 10.3 已知坑

完整的坑表见 [附录 A.3](#a3已知限制与坑)（含 `TRUNCATE TABLE` 偶发 `tablet does not exist`、
transformers.js 降级链污染、Vite `public/` 的 SPA fallback 等）。

---

## 附录 A：技术细节

> 一页速览见 [`tech-notes.md`](./tech-notes.md)。本附录是它的展开。

### A.1 选型理由

**嵌入模型**：`Xenova/bge-small-zh-v1.5`（BAAI/bge-small-zh-v1.5，MIT，512 维）。
语料以中文为主，bge-small-zh 是中文语料上训练的模型；
对比基线 `Xenova/all-MiniLM-L6-v2`（384 维，Apache-2.0）偏英文，中文语义召回明显弱。

| 文件 | all-MiniLM-L6-v2 | bge-small-zh-v1.5 |
|---|---|---|
| `model.onnx`（fp32） | 90,387,606 B | 94,851,877 B |
| `model_fp16.onnx` | 45,297,825 B | 47,497,427 B |
| `model_quantized.onnx`（q8） | 22,972,370 B | 24,010,842 B |

**为什么不用官方 `seekdb` SDK**：

- `seekdb-js` 这个包名已于 2025-12-05 unpublish（npm 404），现役包名是 `seekdb`（v1.4.0）。
- 该 SDK 只依赖 `mysql2`，**Node-only**，浏览器无法使用。
- SDK 的 `hybridSearch()` 内部依赖 `dbms_hybrid_search` 包与 `HYBRID_SEARCH()` 表达式，
  这两者在 seekdb v1.4.0.0 的二进制里调用会报语法错误（`SELECT * FROM dual` 也会报 "No tables used"）。

因此代理层采用**裸 mysql2 + 手写 SQL**，检索逻辑完全可控、可被 `evidence/sql-log.txt` 逐条验证。

**为什么需要代理层**：浏览器无法直连 MySQL 协议端口（不能自定义 TCP 报文头），而 SDK 又是 Node-only。
题目 4.1 明确允许「随应用提供本地 server 的启动脚本与连接配置」。

### A.2 混合检索的 SQL 与分词

两路排名分别在 seekdb 的两个索引上产生，再在**数据库内**用 RRF 融合：

```sql
SELECT d.id, d.title, d.content, d.metadata,
       COALESCE(ft.r, 0) AS fulltext_rank,
       COALESCE(vv.r, 0) AS vector_rank,
       ROUND(COALESCE(1.0/(60+ft.r),0) + COALESCE(1.0/(60+vv.r),0), 6) AS hybrid_score
FROM `documents` d
LEFT JOIN (
  SELECT id, ROW_NUMBER() OVER (
           ORDER BY MATCH(content) AGAINST (? IN NATURAL LANGUAGE MODE) DESC) AS r
  FROM `documents`
  WHERE MATCH(content) AGAINST (? IN NATURAL LANGUAGE MODE)
  LIMIT 50
) ft ON ft.id = d.id
LEFT JOIN (
  SELECT id, ROW_NUMBER() OVER (ORDER BY cosine_distance(embedding, ?)) AS r
  FROM (SELECT id, embedding FROM `documents`
        ORDER BY cosine_distance(embedding, ?) APPROXIMATE LIMIT 50) z
) vv ON vv.id = d.id
WHERE ft.id IS NOT NULL OR vv.id IS NOT NULL
ORDER BY hybrid_score DESC
LIMIT 5;
```

**EXPLAIN 证实两个索引都真的被用上了**：

```
|0 |TEXT RETRIEVAL SCAN             |d(idx_fts)|   ← 全文分支
|0 |VECTOR INDEX SCAN               |d(idx_vec)|   ← 向量分支
```

**为什么用 RRF 而不是加权分数**：`MATCH(...) AGAINST(...)` 的 relevance 分值与 `cosine_distance`
的距离**量纲完全不同**，直接加权必须先归一化且对分值分布敏感。
RRF 只用**名次**（`1/(k+rank)`，k=60），对两路分值的尺度差异免疫，对单路异常更鲁棒，无需归一化。

**全文分词：必须 ngram + NATURAL LANGUAGE MODE**

| 写法 | 结果 |
|---|---|
| 默认分词器（按空白切词） | 中文整段算一个 token，查询「优惠」返回 **0 行** |
| `WITH PARSER ngram` + `AGAINST('优惠')` | 正常命中 |
| `WITH PARSER ngram` + `IN NATURAL LANGUAGE MODE` | 正常命中（与不写 MODE 等价） |
| `WITH PARSER ngram` + `IN BOOLEAN MODE` | **返回 0 行**，必须避免 |

ngram 解析器默认 `ngram_token_size=2`，中英文都能命中。
（`ik`、`ngram2` 解析器也可用，`charset` 不可用。）

### A.3 已知限制与坑

| 坑 | 现象 | 处理 |
|---|---|---|
| `sync_mode` 默认 ASYNC | 新文档查不到 | 建表写 `sync_mode=IMMEDIATE` |
| ngram + `BOOLEAN MODE` | 返回 0 行 | 用 `NATURAL LANGUAGE MODE` |
| transformers.js `webInitChain` | 首次失败后无法真正降级 | 调用 `pipeline()` 前先 `requestAdapter()` 探测 |
| `TRUNCATE TABLE` 偶发失败 | `tablet does not exist` | `withTabletRetry()` 指数退避 |
| `CREATE INDEX ... USING HNSW` | 语法错误 | 用表内 `VECTOR INDEX` 子句 |
| `dbms_hybrid_search` / `HYBRID_SEARCH()` | v1.4.0.0 报语法错 | 改用手写 RRF SQL |
| `SELECT * FROM dual` | "No tables used" | 用 `SELECT 1` 或带 FROM 的写法 |
| 服务端无 AI/embedding 函数 | `SHOW FUNCTION STATUS` 为空 | 端侧算向量（正合赛题要求） |
| `npm view seekdb-js` | 404 Unpublished（2025-12-05） | 包名是 `seekdb` |
| `npm install seekdb` | `EUNSUPPORTEDPROTOCOL "workspace:*"` | 加 `--legacy-peer-deps` |
| `onnxruntime-node` 原生二进制从 GitHub 下载 | GitHub 不可达时安装失败 | 用 npmmirror 镜像手动装 |
| `huggingface.co` 不可达 | 模型下载失败 | `HF_ENDPOINT=https://hf-mirror.com` |
| Vite `public/` 里的模型文件缺失 | 回 `index.html`（200），transformers.js 当 JSON 解析 → `Unexpected token '<'` | 模型镜像由代理提供，缺失返回 404 |
| Vite `public/` 里的 ORT `.mjs` | 被动态 `import()` 时报 "should not be imported from source code" | 运行时资源也由代理提供 |
| `wasmPaths` 传字符串 | transformers.js 不下载二进制，走 CDN | 传对象 `{ mjs, wasm }` |
| `npm install` 跑 postinstall | `onnxruntime-node` 从 GitHub 拉二进制失败 | `npm install --ignore-scripts` + `npm rebuild esbuild` |
| 非管理员装 Chrome | 落到 `%LOCALAPPDATA%\Google\Chrome\Application\`，不在 `C:\Program Files` | 浏览器候选列表已加入该路径 |
| 浏览器端 `DELETE` 预检失败 | 表现为 `Failed to fetch` | 代理 CORS 的 `Access-Control-Allow-Methods` 必须含 `DELETE` |

**`sync_mode` 对照实验**（3 条文档、两张只差 `sync_mode` 的表，唯一差别就是它）：

| 查询形态 | 执行计划 | ASYNC（默认） | IMMEDIATE |
|---|---|---|---|
| 全文过滤 + `APPROXIMATE` 向量排序 | `VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)` | **0 / 3** | 3 / 3 |
| 全文过滤 + 精确向量排序（无 `APPROXIMATE`） | 基表精确扫描 | 3 / 3 | 3 / 3 |
| 两分支 RRF 融合 | 全文支 + 向量分支 | 3 / 3，但 `vector_rank` 全为 **0** | 3 / 3，排名完整 |
| 纯向量检索（`APPROXIMATE`） | `VECTOR INDEX SCAN` | **0 / 3** | 3 / 3 |

一键复现：

```bash
node scripts/verify-sync-mode.js   # 结果写入 evidence/sync-mode-comparison.json
```

### A.4 优化清单与后续计划

**已做的优化**

| 优化 | 效果 | 位置 |
|---|---|---|
| q8 量化 | 权重 90.46 MB → 22.90 MB（3.95×）；冷加载快 49%；检索质量无损 | `web/src/lib/runtime-env.js` |
| batch=8 批量向量化 | 相比逐条调用减少 JS↔推理往返 | `web/src/App.jsx` |
| 权重与 ORT 运行时本地镜像 | 冷加载含下载从「取决于公网」变成确定值；断网可跑 | `server/proxy.js` 的 `/models/*`、`/ort/*` |
| 代理返回 `insertMs` | 能把「写入 + 索引刷新」从端到端里单独拆出来 | `server/proxy.js` |
| HEAD 静态路由 | 基准能取到模型文件的真实磁盘字节数 | `server/proxy.js` |
| tablet 错误退避重试 | 消除「清表后偶发写入失败」 | `server/db.js` 的 `withTabletRetry()` |
| 前端查询埋点 | 界面可显示 `embedMs / searchMs / totalMs` | `web/src/App.jsx` |
| 后端徽标常驻 + 可手动切换 | 用户能立刻知道当前后端，也能自己权衡 | `web/src/App.jsx`、`web/src/lib/backend-pref.js` |
| 降级原因回显 | 徽标下方提示条说明为什么降级 | `web/src/App.jsx` |

**后续可做**（按收益排序）

| 事项 | 预期收益 | 说明 |
|---|---|---|
| 按设备能力自动选档位 | 核显走 q8+WASM、独显走 fp16+WebGPU | 目前手动切 `dtype` / 徽标下拉；可依据 `requestAdapter().info` 与一次微基准自动决定 |
| Worker 里跑推理 | 大批量导入不卡 UI | 把 ONNX session 放进 Web Worker，主线程只收结果 |
| 批内并行 + 流式写入 | 导入 36 条从 2 s 降到 1 s 量级 | 向量化一批就写一批，而不是全部算完再写 |
| 代理连接池 | 降低每请求建连开销 | 当前每请求新建连接，正好放大了 tablet 窗口的命中率 |
| fp16 兜底探测 | 让 fp16 在不支持 `shader-f16` 的设备上自动回落 | 当前 fp16 在本机可用，老设备需显式降级 |
| 更大的评测集 | 让 nDCG 的置信区间更窄 | 当前 25 条查询，指标差异 <0.01 时无法区分 |

### A.5 评测集与基准方法

- `data/corpus.jsonl`：36 条语料，**80% 中文 + 20% 中英混合**（doc-32/33/34 为英文主题）。
- `data/queries.json`：25 条查询（20 条中文 + 5 条混合），
  每条带人工标注的 `relevant`（二元）与 `graded`（分级 1–3，用于 nDCG）。
- 服务端离线基线：`node scripts/search.js --eval`，结果落盘 `evidence/quality-<mode>.json`
  （hybrid 基线 Recall@5 = 0.8867 / nDCG@5 = 0.9663）。
- 浏览器端基准：`scripts/bench-browser.mjs` 驱动 CDP，每组配置开**全新浏览器 profile**
  （保证冷加载含真实权重下载），每项指标重复 5 次取中位数。
  质量指标用浏览器算出的向量跑同一套评测集，公式与 `scripts/search.js` 完全一致。
- 体积口径：代理 `HEAD` 返回的 `Content-Length`，即文件在磁盘上的实际字节数；
  压缩比 = fp32 字节数 ÷ 该配置字节数。

**分阶段耗时（Edge 156，中位数，毫秒）**

| 配置 | 冷加载(含下载) | 权重下载 | 预热加载 | 文档向量化(36条) | 写入+索引刷新 | 查询向量化/条 | seekdb检索 | 端到端 |
|---|---|---|---|---|---|---|---|---|
| fp32 + WASM | 1747 | 503 | 347 | 3285 | 45.4 | 13.26 | 5.11 | 73.8 |
| fp32 + WebGPU | 1843 | 439 | 318 | 404 | 43.2 | 4.69 | 4.91 | 75.9 |
| q8 + WebGPU | 1117 | 119 | 185 | 2630 | 42.2 | 17.96 | 4.95 | 123.3 |
| **q8 + WASM** | **887** | 110 | **117** | **1997** | 40.7 | 8.13 | 5.07 | **60.9** |
| q8 + WASM + b16 | 956 | 130 | 136 | 2014 | 40.1 | 7.80 | 4.84 | 60.7 |
| q8 + WebGPU + b16 | 1368 | 264 | 191 | 2467 | 41.6 | 13.29 | 5.00 | 124.2 |
| **fp16 + WebGPU** | 1277 | 174 | 232 | **279** | 42.6 | **3.18** | 5.12 | 81.3 |

「写入+索引刷新」在所有配置下都是 38~48 ms、「seekdb 检索」都是 4.7~5.4 ms，
**与量化档位和后端完全无关**——这两段都在 seekdb 端，正好印证了「推理解耦」的设计。

**浏览器差异**：Chrome 156 与 Edge 156 的数值接近（如 q8+WASM 端到端 61.8 ms vs 60.9 ms），
完整 14 条配置见 `evidence/bench.json`。
