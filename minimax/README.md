# 规有所依 —— 面向软件持续演进的 SDD 上下文管理系统

一个可运行的最小闭环：**规格 → 索引 → 检索 → 上下文 → 大模型改代码 → 验证 → 状态沉淀**。

三个外部组件都真正参与流程，任何一个都不能拔掉：

| 组件 | 角色 | 接入方式 | 版本 |
| --- | --- | --- | --- |
| **seekdb** | 存放规格片段 / 代码 / 测试，并做**全文检索** | MySQL 协议（PyMySQL） | seekdb v1.4.0.0（`5.7.25-OceanBase seekdb-v1.4.0.0`） |
| **PowerContext** | 保存任务进展与验证结果，**重启后读回** | **HTTP API**（REST 服务） | `powercontext[cli,server]==1.2.0`（SQLite 后端） |
| **MiniMax** | 依据检索出的上下文生成代码修改 | OpenAI 兼容的 `POST {base_url}/chat/completions` | 默认模型 `MiniMax-M3`（可用 `MINIMAX_MODEL` 覆盖） |

---

## 1. 目录结构

```
minimax/
├── README.md              本文件
├── tech-notes.md          一页技术说明
├── requirements.txt       依赖与版本
├── init_db.py             seekdb 建表与灌数据
├── run_demo.sh            一键全流程（含「重启后读回状态」），每步打印退出码
├── ctx/                   上下文模块源码（CLI）
│   ├── __main__.py        python -m ctx 入口
│   ├── cli.py             七个子命令
│   ├── config.py          环境变量配置（无任何硬编码密钥）
│   ├── seekdb.py          连接、eng_docs 建表、自纠正 schema、幂等 upsert
│   ├── ingest.py          抽取规格/代码/测试（Markdown 分节 + Python AST）
│   ├── retrieval.py       全文检索（首选）+ 关键词打分（退化路径）
│   ├── state.py           PowerContext HTTP 客户端
│   ├── minimax_api.py     MiniMax 客户端
│   ├── patch.py           解析并落地模型返回（完整文件 / unified diff）
│   ├── fix.py             提示词组装 + 调用 + 落地 + 留证
│   └── build.py           拼装 ctx.json
├── sample_project/        业务工程（含规格与测试）
│   ├── src/pricing.py     total(items, is_member)
│   ├── specs/pricing.md   规格
│   ├── tests/test_pricing.py  6 条回归 + 2 条目标测试
│   └── conftest.py
├── sample_project_before/ 改前代码快照（与 sample_project/ 同一组测试，叠加实现）
│   └── src/pricing.py     改前的 total()：满减与会员折扣**叠加**
├── samples/               任务描述、有效规格、初始数据、预期结果、重置方法
├── logs/                  before.txt / after.txt / minimax.md / seekdb.md / powercontext.md
└── ctx.json               ctx build 的实际产物（参考输出）
```

---

## 2. 依赖与版本

实测环境：Ubuntu (WSL2) + Python 3.12.3。

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| `powercontext[cli,server]` | 1.2.0 | PowerContext 服务端与客户端 |
| `PyMySQL` | 1.2.3 | 连 seekdb |
| `requests` | 2.34.2 | 连 PowerContext 与 MiniMax |
| `pytest` | 9.2.0 | 跑目标测试与回归测试 |

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

---

## 3. 快速开始（从干净环境到跑通）

### 3.1 准备 seekdb

任选一种，**默认连接参数是 `127.0.0.1:2881` / `root` / 空密码**：

```bash
# 容器方式（官方镜像）
docker run -d -p 2881:2881 oceanbase/seekdb

# 或 Debian/Ubuntu 包方式
sudo apt install seekdb && sudo systemctl start seekdb
```

### 3.2 建库建表 + 灌数据

```bash
cd minimax
python init_db.py --reset
```

输出：

```
数据库 powercontext_minimax 就绪（seekdb root@127.0.0.1:2881/powercontext_minimax）
表 eng_docs：kind/path/symbol/content，全文解析器 ngram
已导入 14 条文档，库内统计：{'code': 2, 'spec': 4, 'test': 8}
```

等价写法（赛题要求的三个子命令之一）：

```bash
python -m ctx index --root sample_project
```

### 3.3 启动 PowerContext（另开一个终端）

```bash
cd minimax
python -m ctx serve
```

```
PowerContext 服务：127.0.0.1:8000
状态文件：/path/to/minimax/.powercontext/powercontext.db
（Ctrl-C 停止；重启后状态仍在同一文件里）
```

`ctx serve` 会把 `POWERCONTEXT_SERVER_DATABASE_URL` **固定**到
`minimax/.powercontext/powercontext.db` 再拉起 `powercontext server run`。

> 为什么不用直接跑 `powercontext server run`：那条命令的数据库路径取自
> `POWERCONTEXT_SERVER_DATABASE_URL`，**不设置时走服务端默认路径**——
> 于是「带环境变量起」和「不带起」会指向两个不同的库，
> 表现为「重启之后状态突然没了」。`ctx serve` 把路径钉死，重启多少次都指向同一个文件。

也可以手工起（务必带上这个变量）：

```bash
export POWERCONTEXT_SERVER_DATABASE_URL="sqlite+aiosqlite:///$PWD/.powercontext/powercontext.db"
powercontext server run --host 127.0.0.1 --port 8000 --no-env-file
```

状态目录已在 `.gitignore` 中。**停掉服务再重启，状态依然在** —— 见 `logs/powercontext.md` 第 5 节。

### 3.4 构建上下文

```bash
python -m ctx build \
  --task "让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个" \
  --out ctx.json
```

```
已写出上下文: ctx.json
检索模式: fulltext（seekdb root@127.0.0.1:2881/powercontext_minimax，解析器 ngram）
规格 4 条 / 代码 2 条 / 测试 8 条 / 整文件 1 份
任务状态: completed=2 todo=2 verification=1（PowerContext 可用）
```

### 3.5 读取任务状态

```bash
python -m ctx status
```

### 3.6 让 MiniMax 生成修改（需要 API Key）

```bash
export MINIMAX_API_KEY=<你的 MiniMax API Key>
python -m ctx fix --ctx ctx.json --out logs/minimax.md
```

模型返回会经过 **AST 语法校验** 才写入 `sample_project/src/pricing.py`，
并把「任务 / 上下文 / 提示词 / 模型返回 / 最终 diff」记进 `logs/minimax.md`。

> API Key **只从环境变量读取**，缺失时命令直接报错退出（不会用任何内置默认值）。
> `sk-api-` 开头的 Key 走**账户余额**计费；`sk-cp-` 开头的 Key 走 **Coding Plan 套餐**计费。

### 3.7 验证并留证

```bash
python -m ctx verify --root sample_project --label "改后（after）" --out logs/after.txt
```

### 3.8 一键全流程（推荐评审直接跑这个）

```bash
cd minimax
bash run_demo.sh
```

它把上面 1～6 步串成一条命令，并且**包含「重启 PowerContext 进程再读回状态」**这一段，
每步都打印 `PASS/FAIL` 和退出码，最后给出总结果：

```
1/6 建库建表 + 灌数据（seekdb）        PASS  init_db.py (exit=0)
2/6 导入工程资料（seekdb）             PASS  ctx index (exit=0)
3/6 启动 PowerContext                  PASS  ctx serve (exit=0)
4/6 按任务检索并输出上下文             PASS  ctx build (exit=0)
5/6 写入任务状态 -> 重启进程 -> 读回   PASS  ctx save / ctx status (exit=0)
6/6 目标测试 + 回归测试                PASS  pytest (exit=0)
全部通过 ✅
```

可用环境变量：`PYTHON`（默认 `python3`）、`POWERCONTEXT_PORT`（默认 8000）、
`POWERCONTEXT_STATE_DIR`（默认 `.powercontext`）、`POWERCONTEXT_SERVER_URL`。

---

## 4. CLI 参考

### 赛题要求的三个子命令

```bash
python -m ctx index --root sample_project                    # 导入规格/代码/测试到 seekdb
python -m ctx build --task "<任务描述>" --out ctx.json        # 按任务检索并输出上下文
python -m ctx status                                          # 读取并打印 PowerContext 任务状态
```

### 辅助子命令

```bash
python -m ctx serve                                  # 启动 PowerContext（状态固定落到 .powercontext/）
python -m ctx save --kind completed --text "<进展>"   # 写入任务进展 / 待办 / 验证结果
python -m ctx fix  --ctx ctx.json --out logs/minimax.md   # 调 MiniMax 生成并落地修改
python -m ctx verify --root sample_project --out logs/after.txt   # 跑测试并记录退出码
```

### `ctx.json` 结构

```json
{
  "task": "<任务描述>",
  "specs": [{"path": "...", "section": "...", "text": "...", "score": 24.32}],
  "code":  [{"path": "...", "symbol": "...", "text": "...", "score": 1.42}],
  "files": [{"path": "...", "symbol": "<file>", "text": "<整份文件原文>"}],
  "tests": [{"path": "...", "case": "...", "text": "...", "score": 4.77}],
  "state": {"completed": ["..."], "todo": ["..."], "verification": ["..."]},
  "retrieval": {"backend": "seekdb", "parser": "ngram", "mode": "fulltext", "...": "..."}
}
```

`specs` / `code` / `tests` / `state` 是赛题规定的四组字段；
`files` 是**整份代码文件**的原文（模型改写的基准，见 `tech-notes.md` 第 3 节 ⑥）；
`task`、`retrieval`、每条命中的 `score` 与 `text` 是额外附加的**可追溯性信息**
（让评审能直接看出检索走了哪条路径、命中来自哪一行）。

---

## 5. 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SEEKDB_HOST` | `127.0.0.1` | seekdb 主机 |
| `SEEKDB_PORT` | `2881` | seekdb 端口 |
| `SEEKDB_USER` | `root` | seekdb 用户 |
| `SEEKDB_PASSWORD` | 空 | seekdb 密码 |
| `SEEKDB_DATABASE` | `powercontext_minimax` | 库名 |
| `POWERCONTEXT_SERVER_URL` | `http://127.0.0.1:8000` | PowerContext 服务地址 |
| `POWERCONTEXT_SCOPE_ID` | `powercontext-minimax` | 任务状态所在 scope 的键 |
| `POWERCONTEXT_CLIENT_API_TOKEN` | 空 | 可选 Bearer token |
| `MINIMAX_API_KEY` | **无默认，必填** | MiniMax API Key |
| `MINIMAX_BASE_URL` | `https://api.minimaxi.com/v1` | MiniMax 接口地址 |
| `MINIMAX_MODEL` | `MiniMax-M3` | 使用的模型 |

仓库里**不含任何真实密钥**；`minimax/.env` 与 `minimax/.powercontext/` 均在 `.gitignore` 中。

---

## 6. 测试命令

```bash
# 目标测试 + 回归测试（赛题要求的同一组命令）
python -m pytest sample_project/tests/test_pricing.py -v

# 或用本项目封装的入口，同时记录退出码
python -m ctx verify --root sample_project --out logs/after.txt
```

| | 改前 | 改后 |
| --- | --- | --- |
| 命令 | `python -m pytest sample_project/tests/test_pricing.py -v` | 同一条命令 |
| 退出码 | **1**（非 0） | **0** |
| 结果 | `2 failed, 6 passed` | `8 passed` |
| 失败原因 | `AssertionError: assert 195.0 == 220` | — |

证据：`logs/before.txt`、`logs/after.txt`。两条记录由**同一个** `ctx verify` 入口生成。

### 改前代码快照

赛题要求交付「**修改前后代码快照**」。仓库里两份都在：

| 快照 | 路径 | 内容 |
| --- | --- | --- |
| 改前 | `sample_project_before/src/pricing.py` | 满减与会员折扣**叠加**（`discount += amount * 0.1`） |
| 改后 | `sample_project/src/pricing.py` | 取优惠金额更大的那个（`discount = max(discount, amount * 0.1)`） |

两者只有 1 行差异：

```diff
--- sample_project_before/src/pricing.py
+++ sample_project/src/pricing.py
@@ -19,5 +19,5 @@
     if amount >= 200:
         discount = 30.0
     if is_member:
-        discount += amount * 0.1
+        discount = max(discount, amount * 0.1)
     return round(amount - discount, 2)
```

**复现「改前失败」**（用改前快照覆盖当前文件，跑**同一条**测试命令）：

```bash
cd minimax
cp sample_project_before/src/pricing.py sample_project/src/pricing.py
python -m ctx verify --root sample_project --label "改前（before）" --out logs/before.txt
# => 退出码 1，2 failed / 6 passed，AssertionError: assert 195.0 == 220
```

改回去：

```bash
cp sample_project/src/pricing.py /tmp/pricing.after.bak   # 或用 git checkout
git checkout -- sample_project/src/pricing.py
```

---

## 7. 证据文件

| 文件 | 证明什么 |
| --- | --- |
| `logs/before.txt` | 改前目标测试**断言失败**、退出码非 0 |
| `logs/after.txt` | 改后同一组测试全过、退出码 0 |
| `logs/minimax.md` | 每次 MiniMax 调用的任务、上下文、提示词、模型返回、最终 diff |
| `logs/seekdb.md` | 建表 DDL、导入、检索；**证明检索结果来自数据库**且随规格变化 |
| `logs/powercontext.md` | 状态保存 → **重启服务进程 → 完整读回** |

---

## 8. 已知限制

1. **MiniMax 账户需有可用额度**。`sk-api-` Key 走**余额**计费，`sk-cp-` Key 走 **Coding Plan 套餐**额度；
   额度耗尽时返回 `HTTP 402 insufficient_balance_error (1008)`。本项目不内置 Key，也不做任何兜底伪造。
2. **全文检索依赖 ngram 解析器**。seekdb v1.4.0.0 默认分词器按空白切词，中文整段算一个 token，
   查「优惠」匹配不到任何行；必须用 `FULLTEXT KEY ... WITH PARSER ngram`。
   若目标 seekdb 版本不支持 ngram，`ensure_schema()` 会退回默认解析器，
   检索侧再退化为 SQL 关键词打分（`ctx/retrieval.py` 的 `keyword_search`），**两条路径都是真实 SQL 查询**。
3. **`eng_docs` 是派生索引**。它可由源文件完全重建；`ensure_schema()` 发现主键结构不符时会直接重建表，
   `ctx index` 还会裁掉「文件已删除 / 符号已改名」的残留记录。
4. **PowerContext 的 scope id 由服务端生成**（形如 `scp_xxx`），不能指定；
   本项目靠固定 `title + summary + idempotency_key` 幂等取得同一个 scope。
5. **检索是关键词/全文匹配，不是语义向量检索**。当前 seekdb 未启用向量索引；
   赛题允许的退化路径已实现，但语义召回能力有限。
6. **`ctx fix` 只改一个文件**。目标文件由 `ctx.json` 的 `code` 条目唯一确定；
   多个代码文件时需用 `--target` 指定。
7. **未做的部分**：没有接入向量检索、没有做多轮对话式任务分解、
   `ctx build` 的召回用固定条数（`--limit`，默认 8）而非自适应阈值、
   `ctx fix` 目前只支持单轮生成，没有实现「验证不通过就带着报错再问一次」的自愈循环。
