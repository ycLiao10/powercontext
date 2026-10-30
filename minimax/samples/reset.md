# 重置方法

把系统恢复到「全新环境」的状态。评审从干净环境开始时，按下面的顺序执行即可。

## 一键重建（推荐）

```bash
cd minimax

# 1. 依赖
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 2. 检索底座：建库建表 + 灌数据（幂等，可反复执行）
python init_db.py --reset

# 3. 另开终端启动 PowerContext
python -m ctx serve

# 4. 重建上下文
python -m ctx build \
  --task "让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个" \
  --out ctx.json

# 5. 看状态
python -m ctx status
```

以上步骤**不依赖任何本地残留**：seekdb 的表会自己建，PowerContext 的 scope
会按幂等键自己建（`idempotency_key=ctx-<POWERCONTEXT_SCOPE_ID>`）。

## 分项重置

| 想清掉什么 | 怎么做 |
| --- | --- |
| seekdb 里的工程资料 | `python init_db.py --reset`（清空 `eng_docs` 后按当前文件重导） |
| 连库一起删 | `DROP DATABASE powercontext_minimax;` 然后 `python init_db.py` |
| PowerContext 的任务状态 | `rm -rf .powercontext/` 并重启服务（`python -m ctx serve`） |
| 只想清记忆、留服务 | 停服务 → 删 `.powercontext/powercontext.db*` → 重启服务 |
| 重新生成上下文 | `python -m ctx build --task "..." --out ctx.json`（覆盖写） |
| 把代码退回「改前」状态 | 见下 |

> 重启 PowerContext 时**务必用 `python -m ctx serve`**（或手工带上
> `POWERCONTEXT_SERVER_DATABASE_URL`）。少了这个变量，服务会用它的默认库路径，
> 表现为「重启之后状态突然没了」——这不是状态丢了，是连到了另一个库。

## 把代码退回「改前」状态

第四阶段之后 `sample_project/src/pricing.py` 会被 MiniMax 的修改覆盖。
要复现「改前失败」，把叠加实现写回去即可：

```python
def total(items, is_member):
    amount = sum(float(item["price"]) for item in items)
    discount = 0.0
    if amount >= 200:
        discount = 30.0
    if is_member:
        discount += amount * 0.1
    return round(amount - discount, 2)
```

然后重新验证：

```bash
python -m ctx verify --root sample_project --label "改前（before）" --out logs/before.txt
```

**改了代码就要重新导入索引**，否则 seekdb 里还是旧内容：

```bash
python -m ctx index --root sample_project
```

## 改了规格之后

```bash
# 1. 改 sample_project/specs/pricing.md
# 2. 重新导入（否则库和上下文都还是旧的）
python -m ctx index --root sample_project
# 3. 重新构建
python -m ctx build --task "..." --out ctx.json
```

验证「输出跟着变」的做法（`logs/seekdb.md` 第 6、7 节有完整记录）：

```bash
# A. 只改数据库，观察 ctx build 的输出跟着变 —— 证明检索源是库不是文件
mysql -h127.0.0.1 -P2881 -uroot -e "
  use powercontext_minimax;
  update eng_docs set content = replace(content, '满 200 减 30', '满 999 减 77')
   where kind='spec' and symbol='计价规则';"
python -m ctx build --task "..." --out /tmp/ctx_B.json
# → 输出里出现「满 999 减 77」，且不再出现「满 200 减 30」

# B. 改规格文件，重新导入，观察输出跟着变 —— 证明不是固定答案
sed -i 's/满 200 减 30/满 200 减 50/g' sample_project/specs/pricing.md
python -m ctx index --root sample_project
python -m ctx build --task "..." --out /tmp/ctx_C.json
# → 输出含「满 200 减 50」，不含「满 200 减 30」
```

## 清理本地残留

```bash
rm -rf .powercontext/ .pytest_cache/ ctx/__pycache__/ sample_project/__pycache__/
```

这些目录都已在 `minimax/.gitignore` 中，不会进入提交。
