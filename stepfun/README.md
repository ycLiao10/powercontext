# 端侧 AI 推理性能优化与 seekdb 混合检索

浏览器本地完成文档向量化，向量 / 原文 / 元数据写入 **seekdb**，查询时在 **seekdb 内用一条 SQL 完成向量检索 + 全文检索的融合排序**。

> 端侧推理完全发生在用户设备上（Transformers.js + ONNX Runtime Web），项目不调用任何云端推理 API；
> 存储与检索全部由 seekdb 承担，客户端不把两路召回结果自行拼合。

---

## 1. 架构

```
┌──────────────────────── 浏览器（用户设备）────────────────────────┐
│  Web 应用 (Vite + 原生 ESM)                                       │
│                                                                   │
│  ① 导入语料 ──► ② embedder.js                                     │
│                  Transformers.js + onnxruntime-web                 │
│                  WebGPU 优先 / WASM 自动降级                        │
│                  INT8(q8) / FP16 / FP32 可选                        │
│                  ↓ 在本机算出向量                                   │
│  ③ POST /api/collections/:name/documents  { text, vector, metadata }│
└───────────────────────────────────────────────────────────────────┘
                              │ HTTP（仅传输向量与原文，无推理）
                              ▼
┌──────────────────────── 本地 Node 桥接服务 ───────────────────────┐
│  server/index.mjs  +  server/seekdb-store.mjs（seekdb-js SDK）     │
│  · 建集合/表、维护集合状态、批量写入、触发索引刷新                   │
│  · 检索：一次请求 -> seekdb 单条候选 SQL 完成融合                   │
└───────────────────────────────────────────────────────────────────┘
                              │ MySQL 协议
                              ▼
┌──────────────────────────── seekdb 1.4.0 ─────────────────────────┐
│  表：id / document / embedding / metadata                         │
│      FULLTEXT INDEX idx_fts (ik)  +  VECTOR INDEX idx_vec (hnsw)   │
│  DBMS_HYBRID_SEARCH：向量召回 + 全文召回 + RRF 融合，库内单条 SQL   │
└───────────────────────────────────────────────────────────────────┘
```

## 2. 目录结构

```
stepfun/
├─ README.md                    本文件（复现指南）
├─ docs/
│  ├─ TECHNICAL_NOTE.md         一页技术说明（交付主文档）
│  ├─ BENCHMARK.md              性能实测结果（由 scripts/bench.mjs 生成）
│  └─ EVALUATION.md             检索质量实测结果（由 scripts/eval-quality.mjs 生成）
├─ shared/models.mjs            模型注册表（服务端与前端唯一数据源）
├─ server/
│  ├─ index.mjs                 本地服务：静态托管 + /api
│  └─ seekdb-store.mjs          seekdb 数据访问层（集合/写入/刷新/混合检索）
├─ web/
│  ├─ index.html                应用界面
│  ├─ src/embedder.js           端侧推理封装（后端选择与降级、批处理）
│  ├─ src/main.js               交互逻辑 + window.__stepfun 测试钩子
│  ├─ src/api.js                桥接服务调用
│  └─ src/styles.css
├─ scripts/
│  ├─ fetch-model.mjs           下载模型权重到本地并记录各精度体积
│  ├─ quantize.py               本地复现 INT8 量化（onnxruntime）
│  ├─ copy-ort.mjs              复制 onnxruntime-web 运行时到本地
│  ├─ dev.mjs                   开发模式（Vite + API 同时启动）
│  ├─ smoke.mjs                 端到端冒烟（真实浏览器跑通全流程）
│  ├─ bench.mjs                 性能基准
│  └─ eval-quality.mjs          量化前后检索质量评测
├─ data/
│  ├─ corpus/                   演示与评测语料（60 篇中文文档）
│  └─ eval/                     带相关性标注的查询集与 qrels
└─ models/                      本地模型权重（.gitignore，通过脚本获取）
```

## 3. 前置条件

| 依赖 | 版本要求 | 说明 |
| --- | --- | --- |
| Node.js | ≥ 20 | 本项目使用 Node 内置 `fetch` 与 ESM |
| seekdb | 1.4.0 | 本地 server 模式，默认 `127.0.0.1:2881`（root / 空密码 / 库 `test`） |
| 浏览器 | Chrome 或 Edge 最新版 | 需支持 WebGPU 才能走 GPU 路径，否则自动降级 WASM |
| Python（可选） | 3.8+ | 仅用于 `scripts/quantize.py` 复现 INT8 量化 |

> **关于 seekdb 服务端**：本地 server 模式下由 seekdb 官方发行版提供 `seekdb` 可执行文件；
> 若使用嵌入式（embedded）模式，把 `SEEKDB_HOST` 等环境变量去掉并改用 SDK 的 `path` 参数即可，
> 数据层代码无需改动。

## 4. 快速开始

```bash
cd stepfun

# 1) 安装依赖（仓库内 .npmrc 已关闭 peer 自动安装，见文件内注释）
npm install

# 2) 获取模型权重到本地 models/（默认走 hf-mirror.com，可用 HF_ENDPOINT 覆盖）
npm run fetch-models
#    产出 models/manifest.json：各精度文件体积与压缩比

# 3) 构建前端（同时把 onnxruntime-web 的 wasm 复制到 web/public/ort）
npm run build

# 4) 启动（前端 + API + 静态模型 均在同一个端口，便于复现）
npm start
#    -> http://127.0.0.1:8787
```

开发模式（前后端分离热更新）：

```bash
npm run dev      # Vite :5173 + API :8787，/api /models /ort /corpus 均代理到 8787
```

### 使用步骤

1. 打开页面，点击 **加载模型（本地）**：右上角徽标会显示实际使用的推理后端（WebGPU 或 WASM 降级）与精度；
2. 在「导入语料」粘贴文本（空行分隔多篇）或选择文件，点击 **导入 → 本地向量化 → 写入 seekdb**；
   进度条展示浏览器内向量化的实时进度；
3. 在「混合检索」输入查询并检索，右侧展示各阶段耗时（查询向量化 / seekdb 检索 / 端到端）；
4. 点击 **查看库内候选 SQL**，可看到 seekdb 为本次检索生成并执行的那一条候选查询 SQL；
5. 切换模型会切换到对应的知识库集合（`kb_<模型短名>`），增量导入只影响当前集合。

## 5. 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8787` | 本地服务端口 |
| `SEEKDB_HOST` | `127.0.0.1` | seekdb 地址 |
| `SEEKDB_PORT` | `2881` | seekdb 端口 |
| `SEEKDB_USER` / `SEEKDB_PASSWORD` | `root` / 空 | seekdb 账号 |
| `SEEKDB_DATABASE` | `test` | seekdb 库名 |
| `SEEKDB_TENANT` | 空 | 连接 OceanBase 时填写租户名 |
| `MODELS_DIR` | `stepfun/models` | 本地模型权重目录 |
| `HF_ENDPOINT` | `https://hf-mirror.com` | 模型下载源 |

## 6. 验证与复现

```bash
npm run smoke    # 端到端：真实浏览器加载模型 -> 向量化 -> 写 seekdb -> 混合检索
npm run bench    # 性能基准：加载耗时 / 向量化吞吐 / 检索延迟 / 端到端延迟
npm run eval     # 质量评测：量化前后 Recall@K / nDCG@K
```

- `npm run smoke` 会自动拉起本地服务并用 Edge 跑通全流程，输出每一步的实测耗时；
- `npm run bench` 与 `npm run eval` 需要 Edge 可执行文件，默认路径为
  `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`，可用 `--edge <path>` 覆盖；
- 三个脚本都保留原始数据（`docs/bench-results.json`、`docs/eval-results.json`），
  报告中位数而非最快值；
- `node scripts/screenshot.mjs` 可生成界面截图（`docs/screenshot.png`），用于评审材料。

采集到的结果文件：

| 文件 | 内容 |
| --- | --- |
| `docs/TECHNICAL_NOTE.md` | 一页技术说明（交付主文档） |
| `docs/BENCHMARK.md` | 性能实测：冷启动 / 热路径、WebGPU 与 WASM 两套后端 |
| `docs/EVALUATION.md` | 检索质量实测：量化前后 Recall@K / nDCG@K / MRR@K |
| `docs/bench-results.json` | 性能原始数据 |
| `docs/eval-results.json` | 质量原始数据（含每条查询的排序明细） |
| `docs/screenshot.png` | 应用界面截图 |

## 7. 合规声明

- **端侧推理**：文档向量与查询向量均在浏览器内由 Transformers.js 计算，
  `env.allowRemoteModels = false`，权重从本机 `models/` 目录读取；
  服务端没有任何 embedding 代码，也不缓存检索结果。
- **seekdb 数据层**：向量、原文与元数据存于 seekdb 表中，
  全文索引（IK）与向量索引（HNSW）均在库内；
  混合检索由 `DBMS_HYBRID_SEARCH` 生成的单条 SQL 在库内完成召回与 RRF 融合，
  客户端只负责发起查询与展示结果。
- **增量与刷新**：写入后调用 `dbms_index_manager.refresh()` 刷新索引，
  保证新导入文档刷新完成后立即可被检索。
