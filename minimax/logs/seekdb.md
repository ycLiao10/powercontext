# seekdb 检索证据

本文件记录第二阶段「接 seekdb，实现 index 与 build」的完整执行过程与可复核输出。
所有命令均在仓库 `<repo>/minimax` 目录下执行。

```
分支: 2026-competition-ecosystem
提交: 97026b85 feat(qoder): add literature desk with hybrid retrieval and PowerContext memory
解释器: Python 3.12.3
seekdb: 5.7.25-OceanBase seekdb-v1.4.0.0
```

## 1. 连接参数（全部来自环境变量）

```
SEEKDB_HOST=<未设置，使用默认 127.0.0.1>
SEEKDB_PORT=<未设置，使用默认 2881>
SEEKDB_USER=<未设置，使用默认 root>
SEEKDB_PASSWORD=<未设置，默认空密码>
```

## 2. 表结构：eng_docs(kind, path, symbol, content)

```
$ python init_db.py --reset
数据库 powercontext_minimax 就绪（seekdb root@127.0.0.1:2881/powercontext_minimax）
表 eng_docs：kind/path/symbol/content，全文解析器 ngram
已导入 14 条文档，库内统计：{'code': 2, 'spec': 4, 'test': 8}

$ mysql -e "show create table eng_docs"
  Create Table: CREATE TABLE `eng_docs` (
    `kind` varchar(32) NOT NULL,
    `path` varchar(512) NOT NULL,
    `symbol` varchar(255) NOT NULL DEFAULT '',
    `content` text DEFAULT NULL,
    PRIMARY KEY (`kind`, `path`, `symbol`),
    KEY `idx_path` (`path`) BLOCK_SIZE 16384 LOCAL,
    FULLTEXT KEY `ft_content` (`content`) WITH PARSER ngram PARSER_PROPERTIES=(ngram_token_size=2) BLOCK_SIZE 16384
  ) ORGANIZATION INDEX DEFAULT CHARSET = utf8mb4 ROW_FORMAT = DYNAMIC COMPRESSION = 'zstd_1.3.8' BLOCK_SIZE = 16384 TABLET_SIZE = 134217728 PCTFREE = 0
```

> 说明：赛题要求「先用全文索引；如果当前 seekdb 版本不支持 MATCH AGAINST 的分词器，退化为关键词打分」。
> 实测 seekdb v1.4.0.0 的**默认**分词器按空白切词，中文整段被视为一个 token，查询「优惠」匹配不到任何行；
> 改用 `WITH PARSER ngram`（CJK 按 bigram 切分）后，中英文均可命中，因此本项目走的是**全文索引**路径，
> 关键词打分仅作为代码里的退化分支保留（见 `ctx/retrieval.py`）。

## 3. 导入：python -m ctx index --root sample_project

```
$ python -m ctx index --root sample_project
已从 sample_project 导入 14 条文档到 seekdb root@127.0.0.1:2881/powercontext_minimax
全文索引解析器: ngram
库内统计: {'code': 2, 'spec': 4, 'test': 8}

$ python -m ctx index --root sample_project   # 重复执行验证幂等
已从 sample_project 导入 14 条文档到 seekdb root@127.0.0.1:2881/powercontext_minimax
全文索引解析器: ngram
库内统计: {'code': 2, 'spec': 4, 'test': 8}

$ mysql -e "select kind, count(*) from eng_docs group by kind"
kind	n
code	2
spec	4
test	8

$ mysql -e "select kind, path, symbol from eng_docs order by kind, path, symbol"
kind	path	symbol
code	sample_project/src/pricing.py	<file>
code	sample_project/src/pricing.py	total
spec	sample_project/specs/pricing.md	示例
spec	sample_project/specs/pricing.md	计价规则
spec	sample_project/specs/pricing.md	订单计价规格
spec	sample_project/specs/pricing.md	边界
test	sample_project/tests/test_pricing.py	test_target_discounts_do_not_stack_when_the_full_reduction_is_larger
test	sample_project/tests/test_pricing.py	test_target_discounts_do_not_stack_when_the_member_discount_is_larger
test	sample_project/tests/test_pricing.py	test_total_applies_the_full_reduction
test	sample_project/tests/test_pricing.py	test_total_applies_the_member_discount
test	sample_project/tests/test_pricing.py	test_total_applies_the_member_discount_below_the_threshold
test	sample_project/tests/test_pricing.py	test_total_of_empty_order_is_zero
test	sample_project/tests/test_pricing.py	test_total_sums_the_prices_of_the_items
test	sample_project/tests/test_pricing.py	test_total_without_discount_below_the_threshold
```

## 4. 检索：python -m ctx build

```
$ python -m ctx build --task "让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个" --out ctx.json
已写出上下文: ctx.json
检索模式: fulltext（seekdb root@127.0.0.1:2881/powercontext_minimax，解析器 ngram）
规格 4 条 / 代码 2 条 / 测试 8 条 / 整文件 1 份
任务状态: completed=2 todo=2 verification=1（PowerContext 可用）
```

生成的 `ctx.json` 关键片段（完整文件见仓库根目录 `minimax/ctx.json`）：

```json
{
  "task": "让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个",
  "specs": [
    {
      "path": "sample_project/specs/pricing.md",
      "section": "计价规则",
      "score": 24.324898
    },
    {
      "path": "sample_project/specs/pricing.md",
      "section": "示例",
      "score": 6.854198
    },
    {
      "path": "sample_project/specs/pricing.md",
      "section": "订单计价规格",
      "score": 2.618445
    },
    {
      "path": "sample_project/specs/pricing.md",
      "section": "边界",
      "score": 0.813743
    }
  ],
  "code": [
    {
      "path": "sample_project/src/pricing.py",
      "symbol": "<file>",
      "score": 11.371056
    },
    {
      "path": "sample_project/src/pricing.py",
      "symbol": "total",
      "score": 1.41874
    }
  ],
  "tests": [
    {
      "path": "sample_project/tests/test_pricing.py",
      "case": "test_target_discounts_do_not_stack_when_the_full_reduction_is_larger",
      "score": 4.768879
    },
    {
      "path": "sample_project/tests/test_pricing.py",
      "case": "test_target_discounts_do_not_stack_when_the_member_discount_is_larger",
      "score": 4.693177
    },
    {
      "path": "sample_project/tests/test_pricing.py",
      "case": "test_total_of_empty_order_is_zero",
      "score": 1.573823
    },
    {
      "path": "sample_project/tests/test_pricing.py",
      "case": "test_total_applies_the_full_reduction",
      "score": 1.507608
    },
    {
      "path": "sample_project/tests/test_pricing.py",
      "case": "test_total_sums_the_prices_of_the_items",
      "score": 1.497919
    },
    {
      "path": "sample_project/tests/test_pricing.py",
      "case": "test_total_applies_the_member_discount",
      "score": 1.466667
    },
    {
      "path": "sample_project/tests/test_pricing.py",
      "case": "test_total_without_discount_below_the_threshold",
      "score": 1.455838
    },
    {
      "path": "sample_project/tests/test_pricing.py",
      "case": "test_total_applies_the_member_discount_below_the_threshold",
      "score": 1.366713
    }
  ],
  "state": {
    "completed": [
      "已用 ctx index 把 sample_project 的规格/代码/测试导入 seekdb",
      "MiniMax 已生成并落地 sample_project/src/pricing.py 的修改（+1/-1 行）"
    ],
    "todo": [
      "等 MiniMax 生成 pricing.py 的修改",
      "跑目标测试确认 8 条全通过"
    ],
    "verification": [
      "改后验证：python -m pytest sample_project/tests/test_pricing.py -v -> 退出码 0，8 passed（目标测试 2 条 + 回归 6 条）"
    ],
    "task": "让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个"
  },
  "retrieval": {
    "backend": "seekdb",
    "database": "powercontext_minimax",
    "dsn": "root@127.0.0.1:2881/powercontext_minimax",
    "parser": "ngram",
    "mode": "fulltext",
    "query": "让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个",
    "limit": 8,
    "doc_counts": {
      "code": 2,
      "spec": 4,
      "test": 8
    },
    "powercontext_available": true
  }
}
```

## 5. 证明一：ctx build 的命中与直接查库逐条一致

同一个 MATCH 查询分别走 `ctx build` 和 `mysql` 客户端，分数与内容长度完全相同，
说明 `ctx build` 的结果**来自数据库查询**而不是读本地文件。

```
$ mysql -e "select kind, path, symbol, char_length(content), match(content) against(...) ... and kind in (spec) order by score desc"
kind	path	symbol	len	score
spec	sample_project/specs/pricing.md	计价规则	199	24.3248984181501
spec	sample_project/specs/pricing.md	示例	239	6.854197549345473
spec	sample_project/specs/pricing.md	订单计价规格	8	2.618445345394994
spec	sample_project/specs/pricing.md	边界	32	0.8137428517035709
```

## 6. 证明二：只改数据库，ctx build 的输出跟着变（检索源是库，不是文件）

```
$ mysql -e "update eng_docs set content = replace(content, '满 200 减 30', '满 999 减 77') where kind='spec' and symbol='计价规则'"

$ grep -c '满 999 减 77' sample_project/specs/pricing.md   # 本地文件里没有这个数值
0

$ python -m ctx build --task "..." --out /tmp/ctx_B2.json
ctx.json 第一条 spec.section = 计价规则
输出含 DB 里的新数值 '满 999 减 77' : True
输出含文件里的旧数值 '满 200 减 30' : False

$ python -m ctx index --root sample_project   # 重新导入，库内容回到文件内容
已从 sample_project 导入 14 条文档到 seekdb root@127.0.0.1:2881/powercontext_minimax
全文索引解析器: ngram
库内统计: {'code': 2, 'spec': 4, 'test': 8}
```

## 7. 证明三：改规格 → 重新导入 → 输出跟着变（不是固定答案）

```
$ cp sample_project/specs/pricing.md /tmp/pricing.md.bak
$ sed -i 's/满 200 减 30/满 200 减 50/g' sample_project/specs/pricing.md

$ python -m ctx index --root sample_project
已从 sample_project 导入 14 条文档到 seekdb root@127.0.0.1:2881/powercontext_minimax
全文索引解析器: ngram
库内统计: {'code': 2, 'spec': 4, 'test': 8}
$ python -m ctx build --task "..." --out /tmp/ctx_C.json
输出含新规则 '满 200 减 50': True
输出仍含旧规则 '满 200 减 30': False

$ cp /tmp/pricing.md.bak sample_project/specs/pricing.md   # 复原
已从 sample_project 导入 14 条文档到 seekdb root@127.0.0.1:2881/powercontext_minimax
全文索引解析器: ngram
库内统计: {'code': 2, 'spec': 4, 'test': 8}
复原后含 '满 200 减 30': True
复原后含 '满 200 减 50': False
```

## 8. 清理与重置

```
重置检索底座：
  python init_db.py --reset          # 清空 eng_docs 后按当前文件重新导入
完全重来（连库一起删）：
  DROP DATABASE powercontext_minimax; python init_db.py
```
