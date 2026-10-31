# 合规对照说明（赛题 2.5「通用要求」）

> 本文件逐条对照赛题 2.5「通用要求」中的**四条禁止项**，说明本作品（`stepfun/`）是怎么满足的，
> 并给出**可复现的验证方式**（命令或文件路径），方便评审不读代码也能自行核验。
>
> 说明：下面四条标题按赛题要求的主题归纳，措辞以任务书转述为准，非原文逐字引用。
> 每条后面的「验证方式」都可以在本地直接执行。

---

## 总览

| # | 赛题 2.5 禁止项 | 本作品的落实 | 一键验证 |
|---|---|---|---|
| 1 | 禁止调用云端推理 API（embedding / 生成必须端侧完成） | 文档向量与查询向量**全部在浏览器内**由 Transformers.js + ONNX Runtime Web 计算 | `node scripts/e2e-browser-check.mjs`（浏览器内跑，代理侧无推理代码） |
| 2 | 禁止服务端预计算向量或检索结果再回放 | 代理层只有转发；向量由浏览器算好后随请求提交，检索结果每次由 seekdb 现场执行 SQL 产生 | `grep -n "pipeline\|embedding\|infer" server/*.js`（无命中）＋ `evidence/sql-log.txt` |
| 3 | 向量与文档的存储、检索必须走 seekdb（不得用 IndexedDB / 内存数组等替代） | 原文 + 元数据 + 向量全部存在 seekdb 的 `documents` 表；检索全部是 seekdb SQL | `node scripts/search.js --eval` ＋ `evidence/quality-hybrid.json` |
| 4 | 不得提交真实密钥 / 凭证（配置一律用占位值） | 全部配置走环境变量，仓库内只有占位默认值 | `grep -rn "password\|api_key\|secret\|token" stepfun/ --include=*.js` |

---

## 1. 禁止调用云端推理 API

**本作品的做法**

- 文档 embedding：浏览器内 `transformers.pipeline('feature-extraction', 'Xenova/bge-small-zh-v1.5', { dtype: 'q8' })`，
  后端优先 WebGPU、不可用时降级 WASM（`web/src/lib/browser-embedder.js`）。
- 查询 embedding：同样在浏览器内算（`embedText()`），算完才随请求提交。
- 代理与服务端脚本**没有任何模型推理代码**，只做 HTTP 转发与 SQL 执行。
- 浏览器只从本地代理取**权重文件**（`/models/*`）和 ORT 运行时（`/ort/*`），
  这些是静态文件，不是推理服务；权重默认由 `npm run prepare:model` 从 hf-mirror 预下载到本地，
  运行期可完全离线。

**可验证方式**

| 想验证什么 | 怎么做 |
|---|---|
| 代理里没有推理 | `grep -rn "pipeline\|embedding\|session.run\|InferenceSession" stepfun/server/ stepfun/scripts/offline-eval-embedder.js` |
| 生产路径确实在浏览器里 | 打开页面 → 右上角徽标显示 `WebGPU` 或 `WASM`；DevTools Network 里能看到浏览器向 `http://127.0.0.1:8787/models/...` 取权重，然后 `POST /api/documents` 带上 512 维向量 |
| 全流程离线可跑 | 断网后执行 `node scripts/e2e-browser-check.mjs`，导入 / 检索 / 增量可见性全部通过 |
| 没有调用任何云端推理接口 | DevTools Network 面板过滤 `fetch`/`xhr`：只应出现 `127.0.0.1:8787` 的请求；权重请求指向本地镜像时连外网都不需要 |

---

## 2. 禁止服务端预计算向量或检索结果再回放

**本作品的做法**

- 代理 `server/proxy.js` 只有 4 个 API：`/api/health`、`/api/schema`、`/api/documents`、`/api/search`，
  外加两个**纯静态文件**路由（`/models/*`、`/ort/*`）。没有缓存层，没有预计算表，没有回放逻辑。
- 检索每次都现场把 SQL 发给 seekdb（`server/db.js` 的 `hybridSearchSql` / `vectorSearchSql` / `fulltextSearchSql`）。
- 混合检索是**一条 SQL**（向量 HNSW 分支 + 全文 ngram 分支 + RRF 融合），不是应用层拼两份结果。
- 实际执行的 SQL 原文会落盘到 `evidence/sql-log.txt`，评审可直接核对。

**可验证方式**

| 想验证什么 | 怎么做 |
|---|---|
| 代理只转发不缓存 | `grep -n "cache\|Map()\|precompute" stepfun/server/proxy.js`（无结果命中缓存逻辑） |
| 检索真的发生在数据库里 | 打开 `stepfun/evidence/sql-log.txt`，看 `label=hybrid` 那段：**同一条 SQL** 里同时出现 `MATCH(content) AGAINST(... IN NATURAL LANGUAGE MODE)`、`cosine_distance(...) APPROXIMATE` 和 `ORDER BY hybrid_score DESC` |
| SQL 走的是索引而不是全表扫 | `node scripts/search.js "缓存雪崩"` 后对同一条 SQL 做 `EXPLAIN`，向量分支为 `VECTOR INDEX SCAN`、全文分支为 `TEXT RETRIEVAL SCAN` |
| 没有「回放」 | 每次检索的 SQL 与返回结果都写进 `evidence/sql-log.txt`；日志里的时间戳与调用次数一一对应 |

---

## 3. 向量与文档的存储、检索必须走 seekdb

**本作品的做法**

- 建表 DDL（`server/db.js` 的 `ensureSchema`）：

  ```sql
  CREATE TABLE documents (
    id        VARCHAR(128) PRIMARY KEY,
    title     VARCHAR(512),
    content   MEDIUMTEXT,
    embedding VECTOR(512),
    metadata  JSON,
    FULLTEXT INDEX idx_fts (content) WITH PARSER ngram,
    VECTOR  INDEX idx_vec (embedding) WITH(distance=cosine, type=hnsw, lib=vsag,
                                     m=16, ef_construction=200, sync_mode=IMMEDIATE)
  );
  ```

- 原文、标题、元数据、向量**同表存储**；全文索引与向量索引建在**同一张表**上，混合检索才能在一条 SQL 里融合。
- 前端**不保存**任何向量副本：浏览器算完直接 POST 给代理，代理直接 INSERT，检索结果直接渲染。
  前端没有 IndexedDB、没有 localStorage 存向量、也没有内存数组缓存向量。
- `sync_mode=IMMEDIATE` 保证「写入后立刻可检索」（见 `tech-notes.md` 第 3 节）。

**可验证方式**

| 想验证什么 | 怎么做 |
|---|---|
| 数据确实在 seekdb 里 | `node scripts/init-db.js` 后 `node scripts/import-corpus.js`；用 mysql 客户端连 `127.0.0.1:2881` 执行 `SELECT id, title, VEC_DIMS(embedding) FROM stepfun_kb.documents;` |
| 前端没有用浏览器本地存储当数据库 | `grep -rn "indexedDB\|localStorage\|sessionStorage" stepfun/web/src/`（除本文件所述的降级提示标志外无命中，见下） |
| 检索质量可量化 | `node scripts/search.js --eval` → `evidence/quality-hybrid.json`（Recall@5 = 0.8867，nDCG@5 = 0.9663） |
| 增量可见性 | `node scripts/verify-sync-mode.js`；浏览器端 `node scripts/e2e-browser-check.mjs` 的第 7 步 |

> 关于 `sessionStorage`：前端只在**一次**场景下用到它 —— 「选了 WebGPU 但建会话失败，自动切到 WASM 重新加载」时，
> 把失败原因暂存一行字符串以便刷新后展示。它不存放任何向量或检索结果。

---

## 4. 不得提交真实密钥 / 凭证

**本作品的做法**

- 所有连接参数、端口、路径都来自环境变量（`server/config.js`），仓库里的默认值是本地开发占位值：
  `host=127.0.0.1 port=2881 user=root password=(空)`。
- 仓库内**没有任何** API Key、Token、账号口令；`.gitignore` 里排除了 `.env` / `.env.local`。
- 模型权重走公开镜像（hf-mirror.com），不需要任何凭证。

**可验证方式**

| 想验证什么 | 怎么做 |
|---|---|
| 没有硬编码凭证 | `grep -rniE "password\s*=\s*['\"][^'\"]+|api[_-]?key|secret|access[_-]?token|bearer " stepfun/ --include=*.js --include=*.json --include=*.md` |
| 敏感配置不入库 | `cat stepfun/.gitignore`（含 `.env`、`.env.local`） |
| 配置表可审 | `README.md` 第 7 节列出了全部环境变量及其默认值 |

---

## 附：评审可一键执行的三条命令

```bash
# 1) 服务端冒烟：写入 10 条 → 三种检索 → 新增 1 条立即可检索
node server/smoke.js

# 2) 检索质量（固定评测集）
node scripts/search.js --eval

# 3) 浏览器端到端（真实 Chrome/Edge，验证端侧推理 + 增量可见性）
node scripts/e2e-browser-check.mjs --url http://127.0.0.1:5173/
node scripts/e2e-browser-check.mjs --url http://127.0.0.1:5173/ --disable-gpu   # 模拟无 WebGPU
```
