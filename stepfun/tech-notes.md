# 技术说明（tech-notes）

> 赛题：seekdb × 阶跃星辰 —— 端侧 AI 推理性能优化与 seekdb 混合检索
> 环境：seekdb v1.4.0.0、Node.js 22.23.2、Transformers.js 4.3.2 + onnxruntime-web 1.31.0-dev
> 机器：i9-13900H + **Intel Iris Xe 核显**（无独显），Chrome 156 / Edge 156
> 展开细节见 [README 附录 A](./README.md#附录-a技术细节)；原始数据见 `evidence/`。

---

## 1. 模型与量化

模型 `Xenova/bge-small-zh-v1.5`（512 维，mean pooling + L2 归一化），生产默认 **q8**。

| dtype | 文件 | 磁盘字节 | MB | 相对 fp32 |
|---|---|---|---|---|
| fp32 | `model.onnx` | 94,851,877 | 90.46 | 1.00× |
| fp16 | `model_fp16.onnx` | 47,497,427 | 45.30 | 2.00× |
| **q8** | `model_quantized.onnx` | 24,010,842 | **22.90** | **3.95×** |

体积取代理 `HEAD` 返回的 `Content-Length`，即文件在磁盘上的实际字节数。

q8 是 **per-channel QInt8 + reduce_range**（模型仓 `quantize_config.json`），不是逐张量量化，
精度损失很小：fp32 / fp16 / q8 三档的 **Recall@5 都是 0.9067**，nDCG@5 最大差异 0.0002。
收益：权重降到 1/3.95，冷加载从 1747 ms 降到 887 ms（快 49%）。

---

## 2. 端侧运行

### 2.1 后端选择：先探测，再决定

**坑：transformers.js 的 `webInitChain` 会被首次失败污染。**
它把建会话串成 `webInitChain = webInitChain.then(load)`，第一次失败后链变成 rejected，
之后任何一次 `pipeline(..., {device:'wasm'})` 都**复用同一个错误**，WASM 一次都没真正跑过。
所以降级必须在**调用 `pipeline()` 之前**决策：

```js
const adapter = await navigator.gpu.requestAdapter();   // navigator.gpu 存在 ≠ 可用
const device = adapter ? 'webgpu' : 'wasm';
```

### 2.2 手动切换（右上角徽标下拉）

优先级：URL 参数 `?backend=` > `localStorage` > 自动探测。
选择持久化在 `localStorage`；选了 WebGPU 但拿不到适配器时**回落到「自动」并显示原因**。

### 2.3 关键实测结论：核显上 WebGPU 不是普遍更快

| 对比 | 结果 |
|---|---|
| fp32：WebGPU vs WASM | WebGPU **快 8.1×**（89.1 vs 11.0 条/秒） |
| fp16：WebGPU vs WASM | WebGPU **快 11.8×**（128.9 条/秒） |
| **q8：WebGPU vs WASM** | **WebGPU 反而慢 24%**（13.7 vs 18.0 条/秒），端到端慢 2 倍（123.3 vs 60.9 ms） |

原因：① INT8 图在 WebGPU EP 无原生算子，要额外反量化；② 核显没有独立显存和 Tensor Core；
③ bge-small 太小（q8 约 24M 参数），GPU 启动与同步开销大于计算本身。
端到端差距（62 ms）远大于纯向量化差距，说明 WebGPU 每次查询还有一次额外同步延迟。

### 2.4 batch 与质量的关系

| 配置 | batch=1 | batch=8 | batch=16 |
|---|---|---|---|
| q8 + WASM | 19.36 | 18.02 | 17.31 |
| fp16 + WebGPU | 41.84 | 128.94 | 148.95 |

**batch 变大不一定更快**：WASM 是逐条推理，batch 只是合并 JS 调用，多出来的只是拷贝开销；
WebGPU 能真正并行，收益显著（fp16 约 3.6×）。

**batch 会轻微影响 top-5 排序**：`q8+WebGPU+batch16` 的 Recall@5 是 0.8867（其余组都是 0.9067），
原因是 batch=16 的浮点累加顺序不同，换回 batch=8 就恢复。
所以**质量指标必须和 batch 一起记录**。

---

## 3. seekdb 使用

```sql
CREATE TABLE `documents` (
  `id` VARCHAR(128) NOT NULL, `title` VARCHAR(512), `content` STRING,
  `embedding` VECTOR(512), `metadata` JSON,
  PRIMARY KEY (`id`),
  FULLTEXT INDEX `idx_fts` (`content`) WITH PARSER ngram,
  VECTOR INDEX `idx_vec` (`embedding`) WITH(
    distance=cosine, type=hnsw, lib=vsag,
    m=16, ef_construction=200, sync_mode=IMMEDIATE)   -- 默认是 ASYNC！
) ORGANIZATION = HEAP;
```

**坑 1：`sync_mode` 默认 ASYNC，会让「刚写入的文档立刻查不到」。**
事务提交后只写 redo log 就返回，由独立 Change Stream 异步把向量灌进内存 HNSW 索引，
新文档**根本不在向量索引里**。实测召回 0/3，且**表现依赖查询形态**：

| 查询形态 | ASYNC | IMMEDIATE |
|---|---|---|
| 全文过滤 + `APPROXIMATE` 向量排序 | **0/3** | 3/3 |
| 两分支 RRF 融合 | 3/3，但 `vector_rank` 全为 0，排序被压低 | 3/3，排名完整 |
| 纯向量检索（`APPROXIMATE`） | **0/3** | 3/3 |

带 `APPROXIMATE` 时执行计划由向量索引主导，文档不在索引里就进不了候选集。
这是「增量导入后刷新即可检索」成立的前提。

**坑 2：ngram 下必须 `NATURAL LANGUAGE MODE`，`BOOLEAN MODE` 恒返回 0 行。**
默认分词器按空白切词，中文整段算一个 token，查询「优惠」返回 0 行。

**坑 3：`TRUNCATE TABLE` 是 DDL，会异步重建 tablet**，
紧随其后的 INSERT/SELECT 有概率撞进重建窗口，报 `tablet does not exist`
（实测约每 30~40 次清表出现 1 次）。修法：`server/db.js` 的 `withTabletRetry()`
对清表/写入/检索/计数统一做指数退避重试。

混合检索是**一条 SQL**：两个 `LEFT JOIN` 子查询分别用 `MATCH(content) AGAINST(...)` 和
`ORDER BY cosine_distance(embedding, ?) APPROXIMATE` 取名次，外层按 `1/(60+ft.r)+1/(60+vv.r)`
（RRF）融合。前端拿到的就是数据库排好的序，不做客户端二次拼装。

实测延迟：**检索 4.7~5.4 ms、写入+索引刷新 38~48 ms**，与量化档位和后端**完全无关**
——这两段都在数据库端，印证了「推理解耦」。---

## 4. 状态与优化

### 4.1 默认配置的取舍

赛题 2.2 要求**优先使用 WebGPU**，所以默认「自动」= 优先 WebGPU、不可用才降级 WASM。
但本机实测（Iris Xe 核显 + q8）显示 **WASM 端到端更快：60.9 ms vs WebGPU 123.3 ms**。
赛题要的是「用上 WebGPU」，用户要的是「跑得快」。做法是：**默认遵守赛题，
同时把选择权交给用户**——右上角徽标可手动切到 WebGPU / WASM，选择持久化在 `localStorage`，
选了 WebGPU 但设备不支持时回落到自动并说明原因。
有独显的机器建议选 fp16 + WebGPU + batch=16（实测 148.9 条/秒，吞吐冠军）。
### 4.2 状态

| 能力 | 验证 |
|---|---|
| 浏览器内推理 + WASM 降级 | `e2e-browser-check.mjs`：Chrome/Edge × WebGPU/WASM 四条路径全通过 |
| 导入→向量化→写入→混合检索闭环 | 同上，含增量可见性断言 |
| 增量导入写入即可检索 | `sync_mode=IMMEDIATE` |
| 手动切换后端 | `backend-switch-check.mjs`（含回落路径） |
| 量化档位可切换 / 离线可跑 | `evidence/bench.json` 三档实测；权重与运行时本地镜像 |

优化清单见 [README 附录 A.4](./README.md#a4优化清单与后续计划)。

---

## 5. 已知限制与坑

五个最值钱的坑已在上文展开：**SYNC_MODE 坑**（3.）、**webInitChain 坑**（2.1）、
**tablet 竞态**（3.）、**q8 在 WebGPU 上慢于 WASM**（2.3）、**batch 与质量的关系**（2.4）。
完整坑表（12 条）与复现命令见 [README 附录 A.3](./README.md#a3已知限制与坑)。
