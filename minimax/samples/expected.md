# 预期结果

## 1. `ctx index`

```
$ python -m ctx index --root sample_project
已从 sample_project 导入 14 条文档到 seekdb root@127.0.0.1:2881/powercontext_minimax
全文索引解析器: ngram
库内统计: {'code': 2, 'spec': 4, 'test': 8}
退出码: 0
```

再跑一次，行数不变（幂等）。

## 2. `ctx build`

```
$ python -m ctx build --task "让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个" --out ctx.json
已写出上下文: ctx.json
检索模式: fulltext（seekdb root@127.0.0.1:2881/powercontext_minimax，解析器 ngram）
规格 4 条 / 代码 2 条 / 测试 8 条 / 整文件 1 份
任务状态: completed=2 todo=2 verification=1（PowerContext 可用）
退出码: 0
```

`ctx.json` 必须包含这四组字段（赛题规定）：

| 字段 | 形状 | 预期内容 |
| --- | --- | --- |
| `specs` | `[{path, section, text}]` | 4 条，`section` 为 `计价规则` / `示例` / `边界` / `订单计价规格`，`text` 为该小节正文 |
| `code` | `[{path, symbol}]` | 2 条，`symbol` = `total`（函数源码）与 `<file>`（整份文件原文） |
| `tests` | `[{path, case}]` | 8 条，`case` 为测试函数名，含 2 条 `test_target_*` |
| `state` | `{completed, todo, verification}` | 三个数组，来自 PowerContext |

另有 `files` 段（1 份整文件原文），是模型改写的基准。

排序应当是合理的：`specs[0]` 是 `计价规则`（得分最高，24.32），
`tests` 里两条 `test_target_*` 排在前面（得分 4.77 / 4.69，高于普通回归测试的 1.37~1.57）。

## 3. `ctx status`

```
$ python -m ctx status
PowerContext: http://127.0.0.1:8000
scope 键: powercontext-minimax
scope id: scp_xxxxxxxx
当前任务: 让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个
completed (2):
  - 已用 ctx index 把 sample_project 的规格/代码/测试导入 seekdb
  - MiniMax 已生成并落地 sample_project/src/pricing.py 的修改（+1/-1 行）
todo (2):
  - 等 MiniMax 生成 pricing.py 的修改
  - 跑目标测试确认 8 条全通过
verification (1):
  - 改后验证：python -m pytest sample_project/tests/test_pricing.py -v -> 退出码 0，8 passed（目标测试 2 条 + 回归 6 条）
退出码: 0
```

**停掉服务进程再重启，输出必须完全一致**（状态在磁盘上的 SQLite 里）。

## 4. `ctx fix`（第四阶段）

```
$ export MINIMAX_API_KEY=<你的 key>
$ python -m ctx fix --ctx ctx.json --out logs/minimax.md
模型：MiniMax-M3
目标文件：sample_project/src/pricing.py
是否落地：是
是否有改动：是
--- diff ---
--- a/sample_project/src/pricing.py
+++ b/sample_project/src/pricing.py
...
```

模型返回必须先通过 **AST 语法校验**才会写进文件。

## 5. `ctx verify` —— 改前 / 改后对照

**改前**（`logs/before.txt`）：

| 项 | 预期 |
| --- | --- |
| 命令 | `python -m pytest sample_project/tests/test_pricing.py -v` |
| 退出码 | **1**（非 0） |
| 结果 | `2 failed, 6 passed` |
| 失败断言 | `AssertionError: assert 195.0 == 220` / `AssertionError: assert 870.0 == 900` |
| 失败性质 | **断言失败**，不能是 import error 或环境错误 |

**改后**（`logs/after.txt`）：

| 项 | 预期 |
| --- | --- |
| 命令 | **同一条命令** |
| 退出码 | **0** |
| 结果 | `8 passed` |
| 用例完整性 | 8 条用例一条不少，断言与预期值一字未改 |

## 6. 正确的改法（参考，不强制）

```python
def total(items, is_member):
    amount = sum(float(item["price"]) for item in items)
    full_reduction = 30.0 if amount >= 200 else 0.0
    member_discount = amount * 0.1 if is_member else 0.0
    discount = max(full_reduction, member_discount)     # 取优惠大的，不叠加
    return round(amount - discount, 2)
```

核对：
- `total(_items(250), is_member=True)` → `max(30, 25) = 30` → `250 - 30 = 220` ✅
- `total(_items(1000), is_member=True)` → `max(30, 100) = 100` → `1000 - 100 = 900` ✅
- `total(_items(250), is_member=False)` → `max(30, 0) = 30` → `220` ✅（回归不破）
- `total(_items(199), is_member=True)` → `max(0, 19.9) = 19.9` → `179.1` ✅（回归不破）
- `total(_items(100), is_member=True)` → `max(0, 10) = 10` → `90` ✅（回归不破）
