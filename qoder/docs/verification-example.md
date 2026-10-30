# 验证样例

两组验证：一组核对 PowerContext 组件本身，一组核对工作台的完整链路。评审按顺序执行即可复现。

## 前置条件

1. `powercontext[cli,server]==1.2.0` 与 `fastembed==0.9.1` 已安装；
2. `qoder/.env` 已由 `.env.example` 生成（含本地 embedding 配置），`powercontext config validate --env-file .env` 通过；
3. 本地嵌入服务运行在 `127.0.0.1:8081`（`curl http://127.0.0.1:8081/health` 返回 `"dimension": 512`）；
4. PowerContext Server 运行在 `127.0.0.1:8000`（`powercontext ready` 返回 `Status: ready`，`/v1/capabilities` 的 `search_modes` 含 `vector`、`hybrid`）；
5. 工作台运行在 `127.0.0.1:8080`（`python -m app --port 8080`）。

## 样例一：组件级（PowerContext 记忆写入 + fts/hybrid 召回）

```bash
python tools/verify_powercontext.py
```

| 项 | 值 |
| --- | --- |
| 初始数据 | 无；脚本每次生成新的 run_id 并创建独立 Scope（`Qoder 赛题验证样例 <run_id>`），保证该 Scope 内记忆全部带向量 |
| 写入 | kind=`preference`，文本：`评审验证样例 <run_id>：该用户正在参加 2026 OceanBase 数据库大赛 Qoder 赛题，偏好中文回答。` |
| 检索 1 | mode 默认（auto），query：`这个用户正在参加什么比赛？` |
| 检索 2 | mode=`hybrid`，query：`参赛选手参加的是哪一个赛事？`（换说法，靠本地嵌入召回） |
| 预期结果 | 输出 `PASS`；`artifacts/verify-powercontext.json` 中 `passed=true`；`search_mode=hybrid`；`hybrid.status=200`、`hybrid.matched=true`、`hybrid.hits[0].matched_by=["vector"]` |

`matched_by` 出现 `vector` 说明 PowerContext 用的是 `127.0.0.1:8081` 上的本地 `bge-small-zh-v1.5`，而不是自造的假索引。

## 样例二：端到端（导入 → 混合检索 → 出处定位 → 记忆复用）

```bash
python tools/verify_literature.py
```

| 项 | 值 |
| --- | --- |
| 初始数据 | 两篇文献：`Transformer 综述`、`混合检索实践`（正文见脚本内 `DOCS`），首次运行为空库 |
| 操作 | 通过 `POST /api/documents` 导入两篇（分块 + 向量 + PowerContext 来源/记忆）；再执行四次检索 |
| 关键词检索 | `混合检索如何融合排序` / `自注意力如何缓解长程依赖` → 前 3 条命中对应文献，`channels` 同时含 `lexical` 与 `vector` |
| 换说法检索 | `两种检索方式的结果怎么合到一起排序` / `长距离依赖为什么不再是问题` → 首位命中对应文献，且首位结果的 `channels` 含 `vector`（词法几乎无词面重合） |
| 预期结果 | 输出 `PASS`；`artifacts/verify-literature.json` 中 `passed=true`，`health.embedding.available=true`、`channels_used` 含 `vector`，每条检索记录满足 `matched_title=true`、`memory_reused=true`、`results[0].memory_boost=true`、`results[0].score=100.0` |

`memory_boost=true` 的来源：导入时该文献被写入 PowerContext 记忆（`文献《标题》已导入…`），检索时 `/v1/memory/search` 命中同一篇文献，`app/search.py` 对其候选赋予 1.15 倍加权。这就是「记忆被保存并在后续使用中复用」的可核对证据。

## 界面人工复核（可选）

打开 `http://127.0.0.1:8080`：

1. 右上角显示 `PowerContext 已连接 · N 篇 / M 段` 与 `嵌入 BAAI/bge-small-zh-v1.5 · 512 维`；
2. 输入标题与正文 → 点「导入正文」→ 提示「共 N 段，生成 M 条向量，来源与记忆已写入 PowerContext」；
3. 检索后结果卡片显示「文献名 / 第几段 / 字符区间 / 相关度」，并带「词法 BM25」「向量语义」「按阅读记忆加权」标签；结果上方显示本次使用的检索通道；
4. 右侧「我的阅读记忆」列出带 `entry_id` 的记忆条目；
5. 输入一条偏好 → 「记住这条偏好」→ 返回新的 `entry_id`。

## 失败判定

| 现象 | 可能原因 |
| --- | --- |
| `/api/health` 返回 `powercontext.available: false` | PowerContext Server 未启动，或 `--pc-url` 配错 |
| `/api/health` 返回 `embedding.available: false` | 嵌入服务未启动（`python tools/embed_server.py --port 8081`），或 `--embed-url` 配错 |
| `search_modes` 只有 `auto, fts` | `.env` 里 embedding 配置被注释或未重启 PowerContext Server |
| `capability_not_supported: vector/hybrid` | 该 Scope 里有嵌入服务上线前写入、尚未带向量的记忆；用新 Scope 复测（验证脚本已自动这样做） |
| `Connection refused` | 工作台未启动或端口被占用 |
| `SQLite URL must use an absolute database path` | `.env` 中数据库路径写成了相对路径 |
| 检索结果为空且查询为纯中文短语 | 需要 3 字以上；三元组索引对 1-2 字查询走 `LIKE` 兜底 |
| `memory_reused=false` | 导入时记忆写入失败（看 `warnings`），或检索 query 与文献主题完全无关 |
| 首次启动嵌入服务很慢 | `bge-small-zh-v1.5` 正在下载到 `qoder/data/models`（约 100MB） |
