# 技术说明（MiniMax × seekdb × PowerContext）

> 一页读完整个方案的设计、接口契约与关键取舍。细节证据在 `logs/` 下四个文件里。

## 1. 要解决的问题

软件持续演进时，「这次改动该依据什么」通常散落在三个地方：Markdown 规格、源码、测试。
模型看不到全貌，人工翻找又不可复现。

本方案的答案是：**把三者变成可检索的一等公民**，
让每一次给模型的输入都能追溯到具体的文件、具体的行，
并把「做过什么、验证过什么」沉淀到可跨重启读取的记忆里。

```
sample_project/                 seekdb                  PowerContext
  specs/pricing.md  ──index──▶  eng_docs ◀──search──┐   memory entries
  src/pricing.py    ──index──▶  (fulltext) │         │   (task/completed/
  tests/*.py        ──index──▶             │         │    todo/verification)
                                                │         │        ▲
                                     build ────┴─────────┼────────┘
                                                │         │
                                                ▼         │
                                        ctx.json ─────────┘
                                     specs / code / tests / files
                                                │
                                                ▼
                                    MiniMax 生成修改（AST 校验后落地）
                                                │
                                                ▼
                                          pytest 验证
```

## 2. 接口契约（全部实测自安装包源码，非推测）

### seekdb

| 接口 | 用法 |
| --- | --- |
| `MATCH(content) AGAINST (? IN NATURAL LANGUAGE MODE)` | 检索，`score` 为相关度 |
| `ON DUPLICATE KEY UPDATE content = VALUES(content)` | 幂等写入 |
| `FULLTEXT KEY ft_content (content) WITH PARSER ngram` | 全文索引（中文必需） |

### PowerContext 1.2.0（HTTP API）

| 端点 | 请求 | 用途 |
| --- | --- | --- |
| `GET /v1/capabilities` | — | 探活 |
| `POST /v1/scopes` | `{title, summary, idempotency_key}` | 幂等取得 scope |
| `POST /v1/memory/remember` | `{scope_id, kind, text, reason?}` | 写入任务进展 |
| `POST /v1/memory/search` | `{scope_id, query, limit}` | 语义检索记忆 |
| `POST /v1/memory/entries/list` | `{scope_id, include_inactive}` | 枚举全部记忆条目 |

`kind` 取 `task` / `completed` / `todo` / `verification`，正好对应 `ctx.json` 的 `state`。

### MiniMax

`POST {MINIMAX_BASE_URL}/chat/completions`，OpenAI 兼容格式，Bearer 认证。
响应取 `choices[0].message.content`。

## 3. 七个关键取舍

### ① 全文索引必须用 ngram 解析器

seekdb v1.4.0.0 的**默认**分词器按空白切词，中文整段算一个 token：

| 查询 | 默认解析器 | ngram 解析器 |
| --- | --- | --- |
| `discount` | ✅ 3 行 | ✅ 3 行 |
| `优惠` | ❌ **0 行** | ✅ 2 行 |
| `量子纠缠字符串` | 0 行 | 0 行 |

默认解析器查中文**一片空白**，所以本项目用 `WITH PARSER ngram`（CJK bigram）。
赛题允许的「退化为关键词打分」没有成为主路径，但作为退化分支保留在
`ctx/retrieval.py::keyword_search`——它同样是 SQL 查询（`LIKE` + `CASE WHEN` 计数）。

### ② 幂等靠复合主键，不靠 `id` 自增

```sql
PRIMARY KEY (kind, path, symbol)
```

`CREATE TABLE IF NOT EXISTS` 在已有旧结构的表上**会静默跳过**——早期实验留下的
`PRIMARY KEY (id)` 表就是这样悄悄长出了重复行。因此 `ensure_schema()` 会自检主键，
不符就重建表（`eng_docs` 是可从源文件完全重建的派生索引，重建是安全的），
并保证表上**恰好一个**全文索引，避免 `MATCH()` 选索引产生歧义。

`sync_documents()` 在 upsert 之外还会**裁剪**本工程前缀下已不存在的记录——
文件被删除、符号被改名之后，旧记录留在库里就会被检索召回（拿到过期内容）。

### ③ scope id 不能指定，只能幂等取得

PowerContext 的 scope id 由服务端生成（`scp_xxx`），`CreateScopeRequest` 里没有 id 字段。
`idempotency_key` 与 `title`/`summary` **绑定**——参数不一致会报
`scope_idempotency_conflict`。因此本项目固定一组
（`title="SDD 上下文管理"` + `summary="scope for <键>"` + `key="ctx-<键>"`），
重复调用稳定拿到同一个 scope。

### ④ 抽取用 AST，不用正则

`ctx/ingest.py` 用标准库 `ast` 抽函数与类（类方法记作 `Cls.meth`），
用标题切 Markdown 小节。抽出来的是**源码原文**，可以直接喂给模型，
也避免了正则误伤（字符串里的 `def` 不会被当成函数定义）。

### ⑤ 落地前必须过 AST 语法校验

模型返回先经 `ast.parse`，失败直接抛错、**不写文件**。
模型偶尔会给出少一个括号、甚至写成 `def total...` 这样的残缺文件，
没有这道闸就会把坏代码写进工程。实测中这道闸真的拦下过一次。

### ⑥ 代码文件要索引「整份原文」(`<file>`)，不能只索引符号

按符号抽取只能拿到函数体；文件顶部的**模块级 docstring** 属于模块，不属于任何符号。
如果只喂函数体，模型根本没见过那段 docstring，生成的「完整文件」就会把它丢掉——
实测中模型连续两次都删掉了模块 docstring，改动从「最小改动」变成了「顺手删注释」。

修法：索引时对每个代码文件**额外存一份整文件原文**（`symbol='<file>'`），
`ctx build` 用 `fetch_documents()` 按路径**精确取回**（不走相关度排序，避免被淘汰），
放进 `ctx.json` 的 `files` 段，作为提示词里「当前文件完整内容（改写基准）」。

改完这一处，MiniMax 生成的 diff 从「删掉 9 行 docstring」变成了**只改 1 行**：

```diff
     if is_member:
-        discount += amount * 0.1
+        discount = max(discount, amount * 0.1)
```

### ⑦ 状态文件路径必须钉死，否则「重启后状态没了」

`powercontext server run` 的数据库路径取自 `POWERCONTEXT_SERVER_DATABASE_URL`，
**不设置时走服务端默认路径**。于是「带环境变量起服务」和「不带起服务」会指向
两个不同的 SQLite 文件，表现为**重启之后状态突然没了**——
看起来像状态丢了，其实连到了另一个库。

本项目用 `python -m ctx serve` 把路径固定到 `minimax/.powercontext/powercontext.db`
再拉起服务（`ctx/serve` 实现在 `ctx/cli.py::_cmd_serve`），
并把 `POWERCONTEXT_SERVER_DATABASE_URL` 一并设好。
验收脚本里就踩过这个坑——第一版验收脚本手工起服务时漏了这个变量，
重启后 scope id 变了、状态为空，一度被误判成「状态复用功能有 bug」。

## 4. 检索怎么打分

`ctx build` 对三类 `kind` 各跑一次全文检索，按 `score` 降序取前 N 条：

```
任务：让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个

specs  计价规则      24.32   ← 规则本身，命中「满/减/会员/折扣/不叠加/优惠/金额/大」
       示例           6.85
       订单计价规格    2.62
       边界           0.81
code   <file>       11.37   ← 整份文件原文（模型改写的基准）
       total         1.42
tests  test_target_..._full_reduction_is_larger    4.77   ← 两条目标测试排在最前
       test_target_..._member_discount_is_larger   4.69
       ... 其余 6 条回归测试                        1.37 ~ 1.57
```

ngram 的 IDF 起了作用：越是通用的小节（「边界」「订单计价规格」）得分越低，
越是含任务关键词的小节（「计价规则」）得分越高。

## 5. 可复现性

- **同样的命令**产出 before / after：两条记录都由 `python -m ctx verify` 生成。
- **改规格 → 重新导入 → 输出跟着变**（`logs/seekdb.md` 第 7 节）。
- **只改数据库 → 输出跟着变**（`logs/seekdb.md` 第 6 节）——证明检索源是库不是文件。
- **重启服务进程 → 状态完整读回**（`logs/powercontext.md` 第 5 节）。
- **幂等**：`ctx index` 连跑三次都是 14 条。
- **模型改写受控**：模型输出先过 `ast.parse`，语法错误直接拒绝落地。

## 6. 已知限制

1. MiniMax 额度：`sk-api-` Key 走账户余额，`sk-cp-` Key 走 Coding Plan 套餐额度；
   额度耗尽会返回 `HTTP 402 (1008)`，本项目不兜底、不伪造。
2. 检索是词法匹配而非语义向量；当前 seekdb 未启用向量索引。
3. `ctx fix` 一次只改一个文件（由 `ctx.json` 的 `code` 条目唯一确定）。
4. `ctx build` 用固定条数召回（`--limit`，默认 8），没有自适应阈值。
5. 全文索引依赖 ngram；老版本 seekdb 需退化为关键词打分路径。
6. `ctx fix` 只做单轮生成，没有「验证不通过就带着报错再问一次」的自愈循环。
