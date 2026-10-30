# PowerContext 状态复用证据

本文件记录第三阶段「接 PowerContext，实现任务状态保存与重启复用」的完整执行过程。

## 0. 接入方式

| 项 | 值 |
| --- | --- |
| 接入方式 | **HTTP API**（`powercontext server run` 起的 REST 服务） |
| powercontext 版本 | `1.2.0`（PyPI 包 `powercontext[cli,server]==1.2.0`） |
| 后端 | SQLite（`sqlite+aiosqlite:///<abs>/minimax/.powercontext/powercontext.db`） |
| 默认地址 | `http://127.0.0.1:8000`（可用 `POWERCONTEXT_SERVER_URL` 覆盖） |
| 使用的端点 | `GET /v1/capabilities`、`POST /v1/scopes`、`POST /v1/memory/remember`、`POST /v1/memory/search`、`POST /v1/memory/entries/list` |
| scope | 服务端生成，形如 `scp_xxx`；客户端用 `idempotency_key=ctx-<POWERCONTEXT_SCOPE_ID>` 幂等取得 |
| 启动方式 | `python -m ctx serve`（把状态文件路径钉死，见文末「已知易错点」） |

## 1. 启动服务

```
$ python -m ctx serve --host 127.0.0.1 --port 8000
PowerContext 服务：127.0.0.1:8000
状态文件：/home/lyc/powercontext/minimax/.powercontext/powercontext.db

$ curl -s http://127.0.0.1:8000/v1/capabilities | head -c 200
{"prompts":{"memory.extract":{"status":"disabled","reason":"provider_not_configured","definition_version":"powercontext.prompt.memory.extract.v1","builtin_version":"powercontext.memory.extract.v1","bu

$ ls -la .powercontext/
total 668
drwxr-xr-x 2 lyc lyc   4096 Oct 31 00:16 .
drwxr-xr-x 8 lyc lyc   4096 Oct 31 00:16 ..
-rw------- 1 lyc lyc     32 Oct 31 00:16 .powercontext.db.cursor-key
-rw-r--r-- 1 lyc lyc 618496 Oct 31 00:16 powercontext.db
-rw-r--r-- 1 lyc lyc  32768 Oct 31 00:16 powercontext.db-shm
-rw-r--r-- 1 lyc lyc  16512 Oct 31 00:16 powercontext.db-wal
```

## 2. 写入一条任务状态

```
$ python -m ctx save --kind todo --text "证据：重启前写入的一条待办"
已写入 todo: 证据：重启前写入的一条待办
```

## 3. 读回

```
$ python -m ctx status
PowerContext: http://127.0.0.1:8000
scope 键: powercontext-minimax
scope id: scp_6ddfsqf8y59dhjcjxbf682vm3j
当前任务: 让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个
completed (2):
  - 已用 ctx index 把 sample_project 的规格/代码/测试导入 seekdb
  - MiniMax 已生成并落地 sample_project/src/pricing.py 的修改（+1/-1 行）
todo (3):
  - 等 MiniMax 生成 pricing.py 的修改
  - 证据：重启前写入的一条待办
  - 跑目标测试确认 8 条全通过
verification (1):
  - 改后验证：python -m pytest sample_project/tests/test_pricing.py -v -> 退出码 0，8 passed（目标测试 2 条 + 回归 6 条）
```

## 4. 重启模块（停掉服务进程 → 用同一个 SQLite 文件重新起）

```
$ pkill -f "powercontext server run"
（服务进程已全部退出）

$ python -m ctx serve --host 127.0.0.1 --port 8000 &
新服务 pid=5486
   5489 /home/lyc/pcvenv/bin/python /home/lyc/pcvenv/bin/powercontext server run --host 127.0.0.1 --port 8000 --no-env-file

$ python -m ctx status      # 重启之后读回
PowerContext: http://127.0.0.1:8000
scope 键: powercontext-minimax
scope id: scp_6ddfsqf8y59dhjcjxbf682vm3j
当前任务: 让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个
completed (2):
  - 已用 ctx index 把 sample_project 的规格/代码/测试导入 seekdb
  - MiniMax 已生成并落地 sample_project/src/pricing.py 的修改（+1/-1 行）
todo (3):
  - 等 MiniMax 生成 pricing.py 的修改
  - 证据：重启前写入的一条待办
  - 跑目标测试确认 8 条全通过
verification (1):
  - 改后验证：python -m pytest sample_project/tests/test_pricing.py -v -> 退出码 0，8 passed（目标测试 2 条 + 回归 6 条）
```

**重启后状态完整读回**——因为 PowerContext 的后端是磁盘上的 SQLite，
重启的只是服务进程，数据不随进程消失。

## 5. 状态如何进入上下文（ctx build）

```
$ python -m ctx build --task "让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个" --out ctx.json
已写出上下文: ctx.json
检索模式: fulltext（seekdb root@127.0.0.1:2881/powercontext_minimax，解析器 ngram）
规格 4 条 / 代码 2 条 / 测试 8 条 / 整文件 1 份
任务状态: completed=2 todo=3 verification=1（PowerContext 可用）

$ python -c "import json;print(json.dumps(json.load(open(\"ctx.json\"))[\"state\"], ensure_ascii=False, indent=2))"
{
  "completed": [
    "已用 ctx index 把 sample_project 的规格/代码/测试导入 seekdb",
    "MiniMax 已生成并落地 sample_project/src/pricing.py 的修改（+1/-1 行）"
  ],
  "todo": [
    "等 MiniMax 生成 pricing.py 的修改",
    "证据：重启前写入的一条待办",
    "跑目标测试确认 8 条全通过"
  ],
  "verification": [
    "改后验证：python -m pytest sample_project/tests/test_pricing.py -v -> 退出码 0，8 passed（目标测试 2 条 + 回归 6 条）"
  ],
  "task": "让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个"
}
```

## 6. 记忆条目的原始数据（数据库视角）

```
$ curl -s -X POST http://127.0.0.1:8000/v1/memory/entries/list -H "Content-Type: application/json" -d "{\"scope_id\":\"...\"}"
scope_id: scp_6ddfsqf8y59dhjcjxbf682vm3j
  kind=completed     version=1 text=已用 ctx index 把 sample_project 的规格/代码/测试导入 seekdb
  kind=verification  version=1 text=改后验证：python -m pytest sample_project/tests/test_pricing.py -v -> 退出码 0，8 passed（目标测试 2 条 + 回归 6 条）
  kind=todo          version=1 text=等 MiniMax 生成 pricing.py 的修改
  kind=todo          version=1 text=证据：重启前写入的一条待办
  kind=todo          version=1 text=跑目标测试确认 8 条全通过
  kind=task          version=1 text=让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个
  kind=completed     version=1 text=MiniMax 已生成并落地 sample_project/src/pricing.py 的修改（+1/-1 行）
```

## 7. 重置方法

```
清空任务状态：
  rm -rf .powercontext/ && python -m ctx serve
完全重来：
  rm -rf .powercontext/            # 删掉本地 SQLite 状态
  python init_db.py --reset        # 重建 seekdb 索引
```

## 8. 已知易错点：状态文件路径必须钉死

`powercontext server run` 的数据库路径取自 `POWERCONTEXT_SERVER_DATABASE_URL`，
**不设置时走服务端默认路径**。于是「带环境变量起」和「不带起」指向两个不同的 SQLite 文件，
表现为**重启之后状态突然没了**——不是状态丢了，是连到了另一个库。

因此本项目提供 `python -m ctx serve`，把 
`POWERCONTEXT_SERVER_DATABASE_URL` 固定到 `minimax/.powercontext/powercontext.db` 再拉起服务。
