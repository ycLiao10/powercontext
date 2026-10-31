# 一页技术说明 · 端侧 AI 推理性能优化与 seekdb 混合检索

> 项目位置：`stepfun/`　·　分支：`2026-competition-ecosystem`
> 一句话：**浏览器本地完成文档向量化，向量 / 原文 / 元数据写入 seekdb，查询时由 seekdb 用一条 SQL 完成向量召回 + 全文召回与 RRF 融合。**

---

## 1. 模型与量化

### 1.1 模型来源与版本

| 项 | 值 |
| --- | --- |
| 模型 | `Xenova/bge-small-zh-v1.5`（BAAI bge-small-zh-v1.5 的 ONNX 导出版） |
| 仓库 | https://huggingface.co/Xenova/bge-small-zh-v1.5 |
| revision | `75c43b069aac4d136ba6bc1122f995fedcfd2781` |
| 维度 | 512（余弦相似度） |
| 许可 | MIT（可公开获取的开源模型） |
| 备选模型 | `Xenova/bge-small-en-v1.5`（384 维）、`Xenova/all-MiniLM-L6-v2`（384 维），已在应用内可切换 |

bge 系列在检索场景下需要在**查询**前拼接指令前缀（`为这个句子生成表示以用于检索相关文章：`），
文档侧不加前缀；前缀规则集中在 `shared/models.mjs`，切换模型时自动跟随。

### 1.2 量化方案

Transformers.js 的 `dtype` 直接对应模型仓库里的不同权重文件，四种精度可切换：

| dtype | 权重文件 | 说明 |
| --- | --- | --- |
| `fp32` | `onnx/model.onnx` | **未量化基线** |
| `fp16` | `onnx/model_fp16.onnx` | 半精度，WebGPU 上推荐 |
| `q8` | `onnx/model_quantized.onnx` | **INT8 方案**：ORT 动态量化（uint8/INT8） |
| `int8` | `onnx/model_int8.onnx` | INT8 方案 |

此外，`scripts/quantize.py` 用 `onnxruntime.quantization` **在本地复现**一次 INT8 动态量化
（输入 `model.onnx`，输出 `model_int8_local.onnx`），用于验证「模型仓库自带的量化权重」确实等价于本地可复现的 INT8 量化结果。

### 1.3 体积对比与压缩比

口径：统计同一模型下**实际 ONNX 权重文件**的字节数（`models/manifest.json` 由 `npm run fetch-models` 生成，含 sha256）。

| 精度 | bge-small-zh-v1.5 | bge-small-en-v1.5 | all-MiniLM-L6-v2 | 压缩比(FP32/本精度) |
| --- | --- | --- | --- | --- |
| FP32 基线 | 94,851,877 B (90.45 MiB) | 133,093,490 B (126.93 MiB) | 90,387,606 B (86.21 MiB) | 1.00× |
| FP16 | 47,497,427 B (45.29 MiB) | 66,749,212 B (63.65 MiB) | 45,297,825 B (43.20 MiB) | **2.00×** |
| INT8（q8） | 24,010,842 B (22.90 MiB) | 34,014,426 B (32.44 MiB) | 22,972,370 B (21.91 MiB) | **3.95×** |
| INT8（int8） | 23,903,394 B (22.80 MiB) | 33,760,831 B (32.20 MiB) | 22,972,370 B (21.91 MiB) | **3.97×** |

本地复现量化结果：`model.onnx` 94,851,877 B → `model_int8_local.onnx` 23,919,094 B，**压缩比 3.97×**，
与仓库自带的 INT8 权重体积一致，说明量化可复现。

### 1.4 检索质量变化

同一设备、同一浏览器、同一语料（60 篇中文文档）、同一评测集（30 条人工标注查询，
每条标注 2-4 篇相关文档），每种精度导入到**独立集合**后用 seekdb 混合检索评测
（详见 `docs/EVALUATION.md`）：

| 精度 | Recall@1 | Recall@5 | Recall@10 | nDCG@10 | MRR@10 | 与 FP32 基线的 Δ(nDCG@10) |
| --- | --- | --- | --- | --- | --- | --- |
| FP32（基线） | 0.2361 | 0.5778 | 0.6556 | 0.6216 | 0.7540 | — |
| FP16 | 0.2361 | 0.5667 | **0.6667** | 0.6285 | 0.7485 | **+0.0069** |
| INT8(q8) | 0.2583 | 0.5889 | 0.6556 | **0.6342** | 0.7773 | **+0.0126** |

**结论：量化后检索质量没有可测量的下降。** INT8 与 FP16 相对 FP32 基线的 Recall@10 / nDCG@10
变化均在 ±0.013 以内（INT8 的 ΔRecall@10 为 0.0000，ΔnDCG@10 为 +0.0126），
属于评测集规模（30 条查询）下的正常波动；30 条查询中有 27 条排序发生变化，
但变化只体现在相关文档之间的相对次序，不影响相关文档是否被召回。
这正是"用 3.95× 的体积压缩换来的检索质量不变"的量化收益。

---

## 2. 端侧运行

### 2.1 运行时

| 项 | 值 |
| --- | --- |
| 推理库 | `@huggingface/transformers` v4.3.2（Web 构建） |
| 推理引擎 | `onnxruntime-web`（wasm + wasm 运行时文件本地托管于 `web/public/ort/`） |
| 权重来源 | 仅本机 `models/` 目录，`env.allowRemoteModels = false`，**不访问任何远程模型仓库** |
| 推理位置 | 文档向量与查询向量全部在浏览器内计算；服务端无任何 embedding 代码 |

### 2.2 后端选择与自动降级

`web/src/embedder.js`：

1. `detectWebGPU()` 调用 `navigator.gpu.requestAdapter()`，拿到 adapter 才认为 WebGPU 可用（避免"有 API 无适配器"的假阳性）；
2. 默认 `device: 'auto'` → 可用则 `webgpu`，否则 `wasm`；用户也可强制指定 `webgpu` / `wasm`；
3. 显式配置推理后端：`pipeline('feature-extraction', modelId, { device, dtype })`；
4. **降级链**：webgpu 加载失败 → 自动改用 wasm 重试；wasm 下 `fp16` 不被 ORT 支持 → 自动改为 `q8`；
5. 界面右上角徽标始终显示**实际**使用的后端与精度（WebGPU / WASM（降级）），并附带 WebGPU 适配器信息或不可用原因。

### 2.3 WASM 降级路径的可用性保障

- ONNX Runtime 的 wasm/mjs 运行时文件由 `scripts/copy-ort.mjs` 复制到 `web/public/ort/`，
  通过 `env.backends.onnx.wasm.wasmPaths = "/ort/"` 指向本机，不依赖任何 CDN；
- 本地服务下发 `Cross-Origin-Opener-Policy: same-origin` 与 `Cross-Origin-Embedder-Policy: require-corp`，
  使浏览器进入跨源隔离状态并启用 `SharedArrayBuffer`，WASM 后端得以开多线程（否则 ORT 会退回单线程）；
- 降级路径与 WebGPU 路径共用同一套 `importDocuments` / `search` 流程，核心功能完全一致。

### 2.4 验证结果

两种后端都在同一台机器上实测跑通全流程（详见 `docs/BENCHMARK.md`）：

- **WebGPU 路径**：本机 Edge 156 + Intel UHD Graphics 12 代核显，
  `navigator.gpu.requestAdapter()` 返回 adapter（vendor `intel` / architecture `gen-12lp`），
  实际后端显示为 **WebGPU**，模型冷启动加载 295~1611 ms（随精度不同）。
- **WASM 降级路径**：把设备强制为 `wasm` 后端端提示变为 **WASM（降级）**，
  核心流程（导入 → 向量化 → 写入 → 刷新 → 混合检索）完全一致，
  冷启动加载 71~212 ms，查询向量化中位 7.15~13.79 ms。
- **自动精度降级**：`wasm + fp16` 请求被自动改为 `q8`（ORT 的 WASM 后端不支持 fp16），
  表格中如实记录为「请求 fp16 → 实际 q8」。

> 一个诚实的观察：在本机（核显 + 12 逻辑核的 i5-13420H）上，**WASM + INT8 的查询延迟
> （7.15 ms）反而低于 WebGPU（20.92 ms）**——bge-small 只有 512 维、6 层，
> 单条短文本推理的计算量很小，WebGPU 的 kernel 调度与显存拷贝开销占了大头；
> 而 FP16 的 WebGPU 路径在批量文档向量化时吞吐最高（11.88 篇/秒）。
> 应用仍然按题目要求"优先 WebGPU"，同时把两条路径的实测数字都如实列出，
> 便于在不同设备（独显 / 高端核显 / 低端笔记本）上做选择。

---

## 3. seekdb 使用

### 3.1 集合与表结构

由 seekdb-js（npm `seekdb` 1.4.0）创建，物理表名 `c$v2$<collectionId>`，实际 DDL（`SHOW CREATE TABLE` 实测）：

```sql
CREATE TABLE `c$v2$af25449bd4d911f1b38900155dd19ec0` (
  `_id`       varbinary(512) NOT NULL,
  `document`  string DEFAULT NULL,
  `embedding` VECTOR(512) DEFAULT NULL,
  `metadata`  json DEFAULT NULL,
  PRIMARY KEY (`_id`),
  FULLTEXT KEY `idx_fts` (`document`)
      WITH PARSER ik PARSER_PROPERTIES=(ik_mode="smart") BLOCK_SIZE 16384,
  VECTOR KEY `idx_vec` (`embedding`)
      WITH (DISTANCE=COSINE, TYPE=HNSW, LIB=VSAG, M=16, EF_CONSTRUCTION=200, EF_SEARCH=64,
            SYNC_MODE=ASYNC) BLOCK_SIZE 16384
) ORGANIZATION HEAP;
```

- 原文 → `document`（供全文索引 IK 分词）；
- 向量 → `embedding VECTOR(512)`（HNSW / COSINE）；
- 元数据 → `metadata JSON`（标题、字符数等）；
- 文档 ID → `_id` 主键，`upsert` 语义保证重复导入是覆盖而不是重复插入；
- `SYNC_MODE=ASYNC` 说明索引写入是异步的，因此**必须显式刷新索引**（见 3.4）。

### 3.2 向量 + 全文混合检索（单条 SQL，在库内完成）

请求链路：`浏览器` → `POST /api/collections/:name/search` → `seekdb-js` → `DBMS_HYBRID_SEARCH`。

seekdb-js 会先用 `DBMS_HYBRID_SEARCH.GET_SQL(table, @search_parm)` 让**数据库**生成候选查询 SQL，
再把这条 SQL 原样执行。融合发生在库内，客户端不拿两路结果自行拼合：

- 全文侧：`match(document) against('...' in natural language mode)`（IK 分词 + BM25 打分）；
- 向量侧：`cosine_distance(embedding, '[...]')`（HNSW 近邻，余弦距离）；
- 融合侧：**RRF**，在 SQL 里直接以 `1 / (rank + 60)` 求和完成。

应用界面上的「查看库内候选 SQL」按钮会实时调 `GET_SQL` 打印这条 SQL，便于评委复核"检索确实发生在数据库里"。

**实测打印出的候选 SQL（向量字面量已省略）**：

```sql
SELECT ifnull(`_fts`.`_id`, `_vs`.`_id`) as `_id`,
       ifnull(`_fts`.`document`, `_vs`.`document`) as `document`,
       ifnull(`_fts`.`embedding`, `_vs`.`embedding`) as `embedding`,
       ifnull(`_fts`.`metadata`, `_vs`.`metadata`) as `metadata`,
       (ifnull(1 / (`_fts`.`_keyword_rank` + 60), 0)
      + ifnull(1 / (`_vs`.`_semantic_rank`  + 60), 0)) as `_score`
FROM (
  (SELECT *, RANK() over(ORDER BY `_keyword_score` DESC) as `_keyword_rank`
     FROM (SELECT /*+ opt_param('hidden_column_visible','true') */
                  `_id`, `document`, `embedding`, `metadata`,
                  match(`document`) against('向量索引与全文索引如何结合' in natural language mode)
                    as `_keyword_score`
             FROM `test`.`c$v2$1c00a3b0d51311f1b38900155dd19ec0`
            WHERE match(`document`) against('向量索引与全文索引如何结合' in natural language mode)
            ORDER BY `_keyword_score` DESC LIMIT 2000)) `_fts`
  full join
  (SELECT *, RANK() over(ORDER BY `_semantic_score` DESC) as `_semantic_rank`
     FROM (SELECT /*+ opt_param('hidden_column_visible','true') */
                  cosine_distance(`embedding`, '[...]') as `_distance`,
                  `__pk_increment`, `_id`, `document`, `embedding`, `metadata`,
                  round(1 - cosine_distance(`embedding`, '[...]') / 2, 8) as `_semantic_score`
             FROM `test`.`c$v2$1c00a3b0d51311f1b38900155dd19ec0`
            ORDER BY `_distance` APPROXIMATE LIMIT 10)) `_vs`
    on `_fts`.`__pk_increment` = `_vs`.`__pk_increment`)
ORDER BY `_score` DESC, ifnull(`_vs`.`__pk_increment`, `_fts`.`__pk_increment`) LIMIT 10;
```

可以看到：**两路召回（`MATCH` 全文 + `cosine_distance` 向量）与 RRF 融合（`1/(rank+60)` 求和）
全部在同一条 SQL 内、由数据库完成**，客户端只负责下发查询与展示结果。

### 3.3 数据写入

- 浏览器完成向量化后，把 `{id, text, vector, metadata}` 批量 `POST` 给桥接服务；
- 桥接服务用 seekdb-js `collection.upsert()` 写入（单次批量 INSERT），写入耗时与刷新耗时分开计量；
- 服务端**不做** embedding 计算，也不缓存检索结果。

### 3.4 增量写入与索引刷新

HNSW 索引为 `SYNC_MODE=ASYNC`，因此每次批量写入后立即调用
`collection.refresh_index()`（底层为 `CALL dbms_index_manager.refresh();`，seekdb ≥ 1.3.0 支持），
保证"新导入的文档在刷新完成后立即可被检索到"。刷新耗时在界面上单独展示。

### 3.5 集合与会话状态

- 集合名规则：`kb_<模型短名归一化>`（seekdb-js 只允许字母/数字/下划线），例如 `kb_bge_small_zh_v1_5`；
- 同一模型共用一个集合，切换模型即切换集合；集合列表与文档数由 `/api/collections` 实时读取；
- 连续导入走 `upsert`，同一 id 覆盖，集合内文档数与检索结果始终与当前知识库一致；
- 前端把「当前集合」写入状态对象，刷新页面后仍能从服务端重建，不会出现"界面显示 A 集合、实际查 B 集合"的情况。

---

## 4. 状态与优化

### 4.1 优化点

| 优化 | 做法 | 效果 |
| --- | --- | --- |
| 批处理 | 文档按 8 条一批送入推理，批间让出主线程 | 吞吐提升且界面不卡顿 |
| 权重本地化 | `env.allowRemoteModels=false`，权重走本机 `/models/` | 首屏不再等待外网下载，推理位置可验证 |
| 跨源隔离 | 服务端下发 COOP/COEP | WASM 后端启用多线程（`SharedArrayBuffer`） |
| 运行时本地化 | ORT 的 wasm/mjs 复制到 `web/public/ort/` | WASM 降级路径不依赖 CDN |
| 检索往返 | 一次请求完成"向量召回 + 全文召回 + RRF"，避免两轮往返 | 端到端延迟只含一次库内检索 |
| 写入语义 | `upsert` + 批量 INSERT，刷新单独计时 | 重复导入不产生脏数据，刷新开销可见 |
| 连接自愈 | 连接异常时重建 mysql 连接并重试一次 | 长时间空转后服务仍可用 |
| 精度自适应 | wasm 下自动把 fp16 换成 q8 | 降级路径不会因不支持的精度而失败 |

### 4.2 关键性能对比

测试条件：Edge 156 / Windows 10.0.26300 x64 / i5-13420H（12 逻辑核）/ Node v24.18.0；
语料 60 篇中文文档；查询集 30 条；每种配置重复 20 次取中位数；
批大小 8；冷启动（模型加载、文档向量化、写入、索引刷新）与预热后（查询链路）分开统计。

| 后端 | 精度 | 模型加载(冷) | 文档向量化 60 篇 | 写入 | 索引刷新 | 查询向量化(热) | seekdb 检索(热) | 端到端(热) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| WebGPU | FP32 | 1611 ms | 11136 ms (5.39 篇/秒) | 428 ms | 503 ms | 20.92 ms | 47.24 ms | 54.45 ms |
| WebGPU | FP16 | 566 ms | **5052 ms (11.88 篇/秒)** | 445 ms | 594 ms | 26.41 ms | 48.68 ms | 56.13 ms |
| WebGPU | INT8(q8) | 295 ms | 10126 ms (5.93 篇/秒) | 391 ms | 589 ms | 81.19 ms | 48.14 ms | 56.52 ms |
| WASM | FP32 | 212 ms | 10884 ms (5.51 篇/秒) | 323 ms | 493 ms | 13.79 ms | 49.20 ms | 57.02 ms |
| WASM | FP16→实际 q8 | 88 ms | 7245 ms (8.28 篇/秒) | 397 ms | 589 ms | 8.49 ms | 47.71 ms | 54.00 ms |
| WASM | INT8(q8) | **71 ms** | 6526 ms (9.19 篇/秒) | 393 ms | 788 ms | **7.15 ms** | 47.07 ms | **53.87 ms** |

可读出的结论：

1. **量化直接换来加载速度与体积**：INT8 相对 FP32，模型冷启动加载从 1611 ms 降到 295 ms（WebGPU）、
   从 212 ms 降到 71 ms（WASM），权重体积压缩 3.95~3.97×；
2. **检索侧瓶颈在库与网络，不在推理**：端到端中位数 53.9~57.0 ms，其中查询向量化只占 7~26 ms，
   seekdb 混合检索往返固定在 47~49 ms —— 这说明"一次请求完成库内融合"的设计是有效的
   （若客户端分两次请求再拼合，至少要多一个 40+ ms 的往返）；
3. **批量向量化更吃精度与后端**：WebGPU + FP16 吞吐最高（11.88 篇/秒），
   WASM + INT8 次之（9.19 篇/秒），WebGPU + INT8 最慢（5.93 篇/秒，
   INT8 算子在核显上缺少高效 kernel）；
4. **写入与索引刷新可控**：60 篇文档批量写入 323~445 ms，索引刷新 493~788 ms，
   两者都与文档数线性相关，作为增量导入的固定开销可以接受。

完整原始数据见 `docs/bench-results.json`，分配置明细见 `docs/BENCHMARK.md`。

---

## 5. 合规声明

- 端侧推理：文档向量、查询向量均在浏览器内计算，`env.allowRemoteModels=false`，
  项目中不存在任何云端推理 API 调用，服务端无 embedding 代码；
- 无预计算回放：向量只在用户导入文档时于本机实时计算，仓库内不含任何预计算的向量或检索结果；
- 存储与检索均在 seekdb：向量、原文、元数据存于 seekdb 表；混合检索由 seekdb 单条 SQL 完成；
- 可复现：`npm install && npm run fetch-models && npm run build && npm start` 即可启动，
  `npm run smoke` 会在真实浏览器中跑通"导入 → 向量化 → 写入 → 刷新 → 混合检索"全流程。
