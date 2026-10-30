# 文献阅读工作台 · Build with Qoder 参赛作品

> 2026 OceanBase 数据库大赛 · Qoder 赛道（`2026-competition-ecosystem` 分支 / `qoder/` 目录）。
> 项目网站：<https://literature-desk-olizvin1uxl.qoder.zone>（公开访问）

## 1. 项目简介

**解决的问题**：读文献时最费时间的不是读，而是「这段话出自哪篇论文的哪一部分」和「上次读到哪里、关注什么」。资料散在多个 .md/.txt 里，关键词回忆不起来就翻不到。

**核心功能**：

| 功能 | 说明 |
| --- | --- |
| 导入与切分 | 粘贴正文或选择 `.md`/`.txt`，按段落/句末切成约 600 字的块，记录字符偏移 |
| 混合检索 | 词法通道（SQLite FTS5 trigram，中文可检索）+ 向量通道（本地 `bge-small-zh-v1.5`，512 维），RRF 融合排序，返回「文献名 + 第几段 + 字符区间 + 原文片段」 |
| 阅读记忆 | 每次导入都写入 PowerContext（来源 + 记忆）；检索时读取 PowerContext 记忆，命中同一篇文献的结果获得加权 |
| 偏好沉淀 | 手动记录阅读偏好，同样进入 PowerContext，下次检索可复用 |

**指定技术**：PowerContext 1.2.0（Scope / Sources / Memory / 检索），承担「记忆与上下文」职责，且真实参与排序——记忆命中会给该文献 1.15 倍加权，界面会标注「按阅读记忆加权」。PowerContext 自身也用同一个本地嵌入模型，`/v1/capabilities` 的 `search_modes` 从 `auto, fts` 扩展到 `auto, fts, vector, hybrid`。

**嵌入服务**：`tools/embed_server.py` 用 fastembed + onnxruntime 在本机跑 `BAAI/bge-small-zh-v1.5`，对外提供 OpenAI 兼容的 `/v1/embeddings`，不依赖任何云端 API，也不需要 GPU。

## 2. 架构

```text
浏览器 (app/index.html)
   │  /api/documents  /api/search  /api/preferences
   ▼
app/server.py  (标准库 http.server，无第三方依赖)
   ├── app/ingest.py  ── 切分 ──► app/store.py (SQLite：FTS5 trigram + chunk_vectors)
   │                   ├───────► 本地嵌入服务 /v1/embeddings（分块向量）
   │                   ├───────► PowerContext /v1/sources/content（逐块来源）
   │                   └───────► PowerContext /v1/memory/remember（文献记忆）
   └── app/search.py  ── RRF 融合 ──► 词法通道 + 向量通道
                      └───────────► PowerContext /v1/memory/search（记忆加权）
```

```text
tools/embed_server.py  (fastembed + onnxruntime, BAAI/bge-small-zh-v1.5, 512 维)
   ├── 工作台 app/embedding.py（分块向量、查询向量）
   └── PowerContext（记忆条目向量：search_modes 含 vector/hybrid）
```

## 3. 目录结构

```text
qoder/
├── app/                          # 应用本体（Python 标准库，无第三方依赖）
│   ├── server.py                 # HTTP 服务与 API
│   ├── ingest.py                 # 导入、切分、写入向量、来源与记忆
│   ├── search.py                 # 词法+向量 RRF 融合与记忆加权
│   ├── embedding.py              # 本地嵌入服务客户端（OpenAI 兼容）
│   ├── store.py                  # SQLite + FTS5(trigram) + chunk_vectors
│   ├── pc_client.py              # PowerContext HTTP 客户端
│   └── index.html                # 单页界面
├── tools/
│   ├── embed_server.py           # 本地嵌入服务（OpenAI 兼容 /v1/embeddings）
│   ├── build_site_data.py        # 从验证证据导出站点演示数据
│   ├── preflight.py              # 提交前自检：字段/凭据/站点/证据/本机服务
│   ├── verify_powercontext.py    # 组件级验证：记忆写入 -> fts/hybrid 召回
│   └── verify_literature.py      # 端到端验证：导入 -> 混合检索 -> 出处 -> 记忆复用
├── site/                         # Qoder Sites 项目网站（静态，index.html + data/demo.json）
├── docs/
│   ├── submission.md             # 提测底稿：表单字段、宣称与证据对照、自检表
│   ├── verification-example.md   # 验证样例（初始数据/步骤/预期结果）
│   ├── qoder-dev-log.md          # Qoder 开发记录
│   └── images/                   # 界面截图
├── data/                         # SQLite 数据与模型缓存（git 忽略）
└── artifacts/                    # 验证证据 JSON（git 忽略）
```

## 4. 运行环境

| 组件 | 版本/说明 |
| --- | --- |
| Python | 3.11+（本机验证 3.12.7，自带 SQLite 3.45 含 FTS5） |
| uv | 0.12+ |
| PowerContext | `powercontext[cli,server]==1.2.0`（PyPI，默认 SQLite 后端） |
| 嵌入模型 | fastembed 0.9.1 + onnxruntime，`BAAI/bge-small-zh-v1.5`（512 维，CPU 可跑） |
| 操作系统 | Windows/macOS/Linux（Windows 下 PowerContext 为实验性支持，SQLite 后端可用） |
| 工作台第三方依赖 | 无（仅嵌入服务需要 fastembed） |

## 5. 从零启动

```bash
# 1) 安装 PowerContext 与嵌入依赖
uv tool install --force "powercontext[cli,server]==1.2.0"
pip install "fastembed==0.9.1" truststore

# 2) 启动本地嵌入服务（终端 A），首次运行会下载模型到 qoder/data/models
cd qoder
python tools/embed_server.py --port 8081
curl -s http://127.0.0.1:8081/health   # 期望 {"dimension": 512, ...}

# 3) 准备配置并启动 PowerContext（终端 B）
cd qoder
cp .env.example .env
# 需要固定数据库位置时，在 .env 中填绝对路径：
# POWERCONTEXT_SERVER_DATABASE_URL=sqlite+aiosqlite:///<绝对路径>/qoder/data/powercontext.db
# 启用本地嵌入（取消 .env 中 embedding 相关行的注释），然后：
powercontext config validate --env-file .env
powercontext server run --env-file .env
curl -s http://127.0.0.1:8000/v1/capabilities | grep -o '"search_modes":[^]]*]'  # 期望含 vector/hybrid

# 4) 启动工作台（终端 C）
cd qoder
python -m app --port 8080

# 5) 打开 http://127.0.0.1:8080
```

工作台启动参数：`--host`、`--port`、`--db`（默认 `qoder/data/literature.db`）、`--pc-url`（默认 `http://127.0.0.1:8000`）、`--embed-url`（默认 `http://127.0.0.1:8081/v1`）、`--embed-model`（默认 `BAAI/bge-small-zh-v1.5`）。嵌入服务不可用时会自动降级为纯词法检索，接口不受影响。

## 6. 验证

```bash
# 组件级：PowerContext 记忆写入 + fts 召回 + hybrid（本地嵌入）召回
python tools/verify_powercontext.py

# 端到端：导入（含向量）-> 混合检索 -> 出处定位 -> 记忆复用
python tools/verify_literature.py
```

`verify_literature.py` 包含两组用例：关键词式查询（词法+向量双通道命中）与换说法查询（几乎不共享原词，只能靠向量通道召回），并断言 `/api/health` 里嵌入服务可用、PowerContext `search_modes` 含 `vector`。两者的预期输出与证据文件说明见 [docs/verification-example.md](docs/verification-example.md)。

## 7. 提测前自检

```bash
cd qoder
python tools/preflight.py --live
```

一条命令校验：提测字段（仓库/分支/Commit SHA/站点）、必需文件、`.env` 未被跟踪、仓库内无凭据或本机绝对路径、公开站点可匿名访问且内容匹配、两份验证证据 `passed=true`、本机 8000/8080/8081 服务在线。结果写入 `artifacts/preflight.json`，退出码 0 才提测。表单字段与逐条判题证据见 [docs/submission.md](docs/submission.md)。

## 8. 当前边界

- 向量索引在内存中按余弦暴力比对（`chunk_vectors` 存 raw float blob），千级分块够用；更大规模应换 seekdb/向量索引。
- 未接入大模型：不做自动摘要与生成式回答，避免把未验证的生成内容当成「出处」。
- PowerContext 的 `vector/hybrid` 模式要求该 Scope 内所有记忆都已有向量：新写入的记忆会立即嵌入，历史 Scope（嵌入服务上线前写入的）会回退为 `fts`。`verify_powercontext.py` 每次用新 Scope 验证这一点。

## 9. 提交清单

- [x] 项目网站：<https://literature-desk-olizvin1uxl.qoder.zone>（Qoder Sites，公开访问，匿名可打开）
- [ ] 代码仓库：团队仓库 `2026-competition-ecosystem` 分支，提交 SHA 已推送
- [x] 运行说明：本文件第 4-6 节
- [x] 验证样例：`tools/verify_literature.py` + `docs/verification-example.md`
- [x] Qoder 开发记录：`docs/qoder-dev-log.md`（含账号 UID）
- [x] 提测底稿与自检：`docs/submission.md` + `tools/preflight.py`
- [x] 使用效果材料：站点 7 天 PV/UV（见 `docs/submission.md` 第 8 节）
- [x] 仓库内无真实 API Key / 密码
