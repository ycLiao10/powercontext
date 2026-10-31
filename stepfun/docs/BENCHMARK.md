# 性能实测结果

> 本文件由 `npm run bench` 自动生成，所有数字来自提交应用的真实运行。

## 测试条件

| 项 | 值 |
| --- | --- |
| 浏览器 | Edg/156.0.4314.50 |
| 平台 | win32 10.0.26300 x64 |
| CPU | 13th Gen Intel(R) Core(TM) i5-13420H（12 逻辑核） |
| Node.js | v24.18.0 |
| 语料规模 | 60 篇中文文档 |
| 查询集 | 30 条 |
| 重复次数 | 每种配置 20 次，报告中位数（另附 p90 / min） |
| 批大小 | 8 |
| 采集时间 | 2026-10-31T10:20:02.969Z |

说明：模型加载耗时属于**冷启动**（含权重下载/解析与 ORT 会话建立），与预热后的推理延迟分开统计；
seekdb 检索耗时为浏览器发起请求到拿到排序结果的往返时间，包含网络与库内检索；
端到端 = 查询向量化 + seekdb 检索。

## 结果总表

| 模型 | 推理后端 | 精度 | 模型加载(冷) | 文档向量化 | 写入 seekdb | 索引刷新 | 查询向量化(热,中位) | seekdb 检索(热,中位) | 端到端(热,中位) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Xenova/bge-small-zh-v1.5 | webgpu → **webgpu** | fp32 → **fp32** | 1611 ms | 11135.88 ms (5.39 篇/秒) | 427.56 ms | 503.13 ms | 20.92 ms | 47.24 ms | 54.45 ms |
| Xenova/bge-small-zh-v1.5 | webgpu → **webgpu** | fp16 → **fp16** | 565.81 ms | 5052.39 ms (11.88 篇/秒) | 445.01 ms | 594.28 ms | 26.41 ms | 48.68 ms | 56.13 ms |
| Xenova/bge-small-zh-v1.5 | webgpu → **webgpu** | q8 → **q8** | 295.44 ms | 10126.09 ms (5.93 篇/秒) | 390.54 ms | 588.74 ms | 81.19 ms | 48.14 ms | 56.52 ms |
| Xenova/bge-small-zh-v1.5 | wasm → **wasm** | fp32 → **fp32** | 212.25 ms | 10883.72 ms (5.51 篇/秒) | 323.41 ms | 492.79 ms | 13.79 ms | 49.2 ms | 57.02 ms |
| Xenova/bge-small-zh-v1.5 | wasm → **wasm** | fp16 → **q8** | 87.95 ms | 7244.98 ms (8.28 篇/秒) | 397.19 ms | 589.25 ms | 8.49 ms | 47.71 ms | 54 ms |
| Xenova/bge-small-zh-v1.5 | wasm → **wasm** | q8 → **q8** | 70.88 ms | 6525.66 ms (9.19 篇/秒) | 392.64 ms | 787.89 ms | 7.15 ms | 47.07 ms | 53.87 ms |

## 分配置明细

### Xenova/bge-small-zh-v1.5 · 请求 webgpu/fp32 → 实际 webgpu/fp32

- 集合：`bench_bge_small_zh_v1_5_webgpu_fp32`（60 篇），批大小 8，采样 20 次
- 冷启动：模型加载 1611 ms；文档向量化 11135.88 ms（5.39 篇/秒）；写入 427.56 ms；索引刷新 503.13 ms
- 热路径中位数：查询向量化 20.92 ms（p90 42.22 / min 11.82）；seekdb 检索 47.24 ms（p90 50.14 / min 45.13）；端到端 54.45 ms（p90 57.97 / min 51.07）
- WebGPU：可用（intel gen-12lp）

### Xenova/bge-small-zh-v1.5 · 请求 webgpu/fp16 → 实际 webgpu/fp16

- 集合：`bench_bge_small_zh_v1_5_webgpu_fp16`（60 篇），批大小 8，采样 20 次
- 冷启动：模型加载 565.81 ms；文档向量化 5052.39 ms（11.88 篇/秒）；写入 445.01 ms；索引刷新 594.28 ms
- 热路径中位数：查询向量化 26.41 ms（p90 59.2 / min 11.89）；seekdb 检索 48.68 ms（p90 50.75 / min 45.25）；端到端 56.13 ms（p90 61.86 / min 51.82）
- WebGPU：可用（intel gen-12lp）

### Xenova/bge-small-zh-v1.5 · 请求 webgpu/q8 → 实际 webgpu/q8

- 集合：`bench_bge_small_zh_v1_5_webgpu_q8`（60 篇），批大小 8，采样 20 次
- 冷启动：模型加载 295.44 ms；文档向量化 10126.09 ms（5.93 篇/秒）；写入 390.54 ms；索引刷新 588.74 ms
- 热路径中位数：查询向量化 81.19 ms（p90 109.33 / min 74.95）；seekdb 检索 48.14 ms（p90 52.93 / min 46.53）；端到端 56.52 ms（p90 60.04 / min 53.38）
- WebGPU：可用（intel gen-12lp）

### Xenova/bge-small-zh-v1.5 · 请求 wasm/fp32 → 实际 wasm/fp32

- 集合：`bench_bge_small_zh_v1_5_wasm_fp32`（60 篇），批大小 8，采样 20 次
- 冷启动：模型加载 212.25 ms；文档向量化 10883.72 ms（5.51 篇/秒）；写入 323.41 ms；索引刷新 492.79 ms
- 热路径中位数：查询向量化 13.79 ms（p90 16.47 / min 8.64）；seekdb 检索 49.2 ms（p90 54.4 / min 45.69）；端到端 57.02 ms（p90 62.29 / min 52.99）
- WebGPU：可用（intel gen-12lp）

### Xenova/bge-small-zh-v1.5 · 请求 wasm/fp16 → 实际 wasm/q8

- 集合：`bench_bge_small_zh_v1_5_wasm_fp16`（60 篇），批大小 8，采样 20 次
- 冷启动：模型加载 87.95 ms；文档向量化 7244.98 ms（8.28 篇/秒）；写入 397.19 ms；索引刷新 589.25 ms
- 热路径中位数：查询向量化 8.49 ms（p90 11.83 / min 5.4）；seekdb 检索 47.71 ms（p90 50.27 / min 44.1）；端到端 54 ms（p90 58.67 / min 49.85）
- WebGPU：可用（intel gen-12lp）

### Xenova/bge-small-zh-v1.5 · 请求 wasm/q8 → 实际 wasm/q8

- 集合：`bench_bge_small_zh_v1_5_wasm_q8`（60 篇），批大小 8，采样 20 次
- 冷启动：模型加载 70.88 ms；文档向量化 6525.66 ms（9.19 篇/秒）；写入 392.64 ms；索引刷新 787.89 ms
- 热路径中位数：查询向量化 7.15 ms（p90 10.76 / min 5.25）；seekdb 检索 47.07 ms（p90 49.02 / min 44.63）；端到端 53.87 ms（p90 56.93 / min 50.12）
- WebGPU：可用（intel gen-12lp）
