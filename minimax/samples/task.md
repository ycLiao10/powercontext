# 任务描述

本目录是赛题要求的 `samples/`：把一次完整任务的**输入、权威依据、起点、终点**固化下来，
让评审不用读代码就能复现整个流程。

## 本次任务的原文

> 让 `total` 按规格实现「满减与会员折扣**不叠加**，取优惠金额**更大的那个**」。

喂给 CLI 的任务串（`ctx build --task` 的实参）：

```
让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个
```

## 任务边界

| 项 | 内容 |
| --- | --- |
| 目标函数 | `sample_project/src/pricing.py` 的 `total(items, is_member)` |
| 权威依据 | `sample_project/specs/pricing.md`（从 seekdb 检索得到，见 `samples/spec.md`） |
| 验收标准 | `sample_project/tests/test_pricing.py` 中 `test_target_*` 两条目标测试 |
| 不可改动 | 测试文件、规格文件、函数名与签名 `total(items, is_member)` |
| 改动范围 | 只改 `sample_project/src/pricing.py`，只做让规格与测试通过的最小改动 |

## 为什么这个任务需要「上下文」

单看代码会以为「满减 + 会员折扣」自然叠加；单看测试只能看到两个期望值；
只有**同时**拿到规格（不叠加、取优惠大的）、当前实现（错误地叠加）和目标测试
（250→220、1000→900），才能推出正确的改法：

```python
discount = max(full_reduction, member_discount)   # 而不是 +=
```

这三样东西分处三个地方——Markdown 规格、Python 源码、pytest 文件——
所以必须先把它们**索引进 seekdb**，再**按任务检索**出来喂给模型。
这正是本赛题「规有所依」的落点：**上下文有出处、检索可复现、改动可验证。**
