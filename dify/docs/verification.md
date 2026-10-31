# 验证样例与运行证据

本文给出评审可以照着做的**完整验证样例**：初始状态、输入序列、预期表现、重置方法，以及对应的自动化脚本和已采集的证据。

---

## 0. 一次跑完全部验证

```bash
cd powercontext/dify
bash scripts/e2e_cross_session.sh      # 端到端：Dify 会话 A → Dify 会话 B
```

脚本会打印每一步、并把完整输出写入 `evidence/e2e-cross-session.txt`。

---

## 1. 样例 A：跨会话记忆（**核心样例，题面要求的必考项**）

### 初始状态

- PowerContext 服务已启动（`http://127.0.0.1:8000/health/live` 返回 200）
- 玩家存档为空，或只有一条初始状态记忆：
  ```
  [npc_state] 初始状态：阿铁是镇上脾气最硬的铁匠，记仇=0，恩情=0，对旅人态度中立，尚未结怨也未受恩。
  ```
- 执行 `bash scripts/reset.sh` 可回到这个状态。

### 输入序列

| 步骤 | 会话 | 玩家ID | 本轮行动 | 详细描述 |
| --- | --- | --- | --- | --- |
| 1 | **A**（全新对话） | `player-1` | 恶作剧 | 我把阿铁的风筝踩进了泥里，还踩了两脚 |
| 2 | **B**（**新建对话**，不是继续 A） | `player-1` | 帮忙 | 我给你带了串糖葫芦，炉子的事我回头再修 |
| 3 | B（同会话，可选） | `player-1` | 结束冒险 | 不写了 |

### 预期表现

**会话 A**：

- 阿铁的反应要体现"记恨"（记仇 +1），例如放话要报复，但**不会**说"上次"——因为此刻还没有旧账。
- 回复末尾状态条：`第 1 回合 · 记仇 1 · 恩情 0 · 态度 记恨`。

**会话 B**（关键）：

- 阿铁**主动提起**会话 A 里的旧事（应出现"风筝""泥""上次"这类指向旧事的词），而不是泛泛地打招呼。
- 因为这条恶作剧记录在 PowerContext 里，即使 B 是全新会话、没有 A 的任何上下文。
- 状态条：`第 2 回合 · 记仇 0 · 恩情 1 · 态度 中立`（帮忙让恩情 +1、记仇 −1）。

**PowerContext 侧**（`bash scripts/show_memory.sh`）：

```
[npc_state]  初始状态：…记仇=0，恩情=0…
[prank_grudge] 第1回合，玩家对阿铁使坏：把阿铁的风筝踩进了泥里，还踩了两脚。阿铁记下了这笔账。
[npc_state]  第1回合后状态：记仇=1，恩情=0，态度=记恨。
[help_favor]   第2回合，玩家帮了阿铁：给你带了串糖葫芦，炉子的事我回头再修。阿铁记着这份人情。
[npc_state]  第2回合后状态：记仇=0，恩情=1，态度=中立。
```

### 如何证明"不是同一个对话窗口里的上下文"

四份材料互相印证（全部在 `evidence/`）：

1. **`evidence/e2e-cross-session.txt`**：会话 A 与会话 B 的 `conversation_id` **不同**（脚本会断言 `CONV_A != CONV_B`）。Dify 的会话上下文按 conversation 隔离，不同 conversation 之间不共享任何上下文。
2. **`evidence/powercontext-memory-export.txt`**：记忆条目带 `entry_id` / `entry_version_id`，并且是**会话 B 之前**就写入的。
3. **`evidence/powercontext-container.log`**：PowerContext 服务端访问日志里的**写入时间戳**早于**读取时间戳**，可见"先写后读"。
4. **`evidence/dify-node-runs.md`**：会话 B 的工作流运行记录里，第一个 HTTP 节点就是 `POST /v1/context/prepare`，其响应里带着会话 A 写进去的原文。

> 补充说明：Dify Chatflow 的 LLM 节点只吃本轮输入 + 工作流里显式喂进去的变量。如果不接 PowerContext，会话 B 的模型上下文里根本不可能出现会话 A 的内容。

### 本机实测记录（2026-10-31 18:47，Dify 1.18.0 + PowerContext 1.2.0）

| 项目 | 实测值 |
| --- | --- |
| 玩家存档 | `scope_id = scp_23kagjzeq34hbbxg7pbjhwf47r`（幂等键 `dify-npc-player-1`） |
| 会话 A 的 conversation_id | `f4349dcd-8be5-40e2-b810-a18b01a22925` |
| 会话 B 的 conversation_id | `a1366cbe-b9dc-4e26-9006-0c6228c6eb53`（**与 A 不同**） |
| 会话 A 的回答 | 「第一次来就敢糟蹋我的东西？规矩记住：风筝坏了，你就拿工钱赔；再敢耍花招，我把你脑袋夹进铁钳里！」<br>`第 1 回合 · 记仇 1 · 恩情 0 · 态度 记恨` |
| 会话 B 的回答 | 「糖葫芦？拿来吧。**上次你把我风筝踩进泥里，还踩了两脚**，我可还记着呢；炉子敢再拖，我就把你焊在炉边。」<br>`第 2 回合 · 记仇 0 · 恩情 1 · 态度 中立` |
| 会话 A 写入的条目 | `prank_grudge` → `mem_ent_fbeaea9893b348f7892a8d0764d38d48`<br>`npc_state`（第 1 回合后状态）→ `mem_ent_f8a59c8d84664e09ab16fb9d7d6e98ae` |
| 断言结果 | 端到端 **5/5 PASS**；PowerContext 层独立复核 **3/3 PASS** |

### 样例 A 的可视证据（截图）

`evidence/screenshots/02-webapp-cross-session.png`：在一个**全新的浏览器会话**里打开 WebApp、新建对话、
只填了"帮忙 + 糖葫芦"，阿铁的回答是：

> 糖葫芦我收下了，算你有一点良心。**上次你把我风筝踩进泥里、还补了两脚的事，我可没忘**；炉子你记着修，别想拿串糖葫芦就赖账。
> 第 3 回合 · 记仇 -1 · 恩情 2 · 态度 亲近

这个浏览器窗口此前从未打开过本应用，Dify 侧没有任何上一轮上下文；"风筝"这件事只存在于 PowerContext 里。

### 重置

```bash
bash scripts/reset.sh            # 软重置：retire 全部条目 + 重写初始状态（服务不中断）
bash scripts/reset.sh --hard     # 硬重置：删除 SQLite 库文件（需要重启 PowerContext）
```

---

## 2. 样例 B：多玩家隔离

### 初始状态

同上。

### 输入序列

| 步骤 | 会话 | 玩家ID | 本轮行动 | 详细描述 |
| --- | --- | --- | --- | --- |
| 1 | A | `player-1` | 恶作剧 | 我把阿铁的风筝踩进了泥里 |
| 2 | B | `player-2` | 帮忙 | 我来给你帮忙 |

### 预期表现

- `player-2` 的第一次交互**不应该**提到风筝——那是 `player-1` 的账。
- `bash scripts/show_memory.sh`（先 `PLAYER_ID=player-2 bash scripts/show_memory.sh`）显示 `player-2` 是另一个 `scope_id`，条目独立。

### 重置

```bash
PLAYER_ID=player-2 bash scripts/reset.sh
```

---

## 3. 样例 C：结局分支

### 输入序列

1. 会话 A：玩家连续 3 次「恶作剧」（每次新建对话）→ 记仇值到 3，态度变「死敌」
2. 会话 B：玩家选「结束冒险」

### 预期表现

- 状态条：`态度 死敌`
- 阿铁的结局台词带报复意味（脚本里对应「这辈子别再让我看见你。」）
- 把恶作剧换成「帮忙」3 次，则结局变为报恩（「……下回再来，提前说一声。」）

### 重置

```bash
bash scripts/reset.sh
```

---

## 4. 样例 D：PowerContext 层验证（不依赖 Dify，5 分钟内可独立复核）

```bash
bash scripts/verify_cross_session.sh
```

它做的事：

1. 重置存档
2. 写入一条恶作剧记忆 + 一条帮忙记忆（模拟会话 A）
3. 用**全新的 HTTP 请求**调用 `POST /v1/context/prepare`（模拟会话 B）
4. 断言召回内容里：
   - 包含会话 A 写入的 `entry_id` ✅
   - 包含恶作剧原文 ✅
   - 包含帮忙原文 ✅

输出示例：

```
[pc] 步骤 3：会话 B —— 全新 HTTP 请求，读取历史
  status = ready
  ...
  PASS  召回内容中包含会话A 写入的 entry_id=mem_ent_a1b94b8a22ad4a6386e147317f2284ce
  PASS  召回内容包含恶作剧原文
  PASS  召回内容包含帮忙原文
[pc] 跨会话验证通过（3/3）
```

这个样例的价值：**即使评审不装 Dify**，也能独立确认"PowerContext 真的跨请求持久化了记忆"。

---

## 5. 样例 E：PowerContext 重启后记忆仍在（持久化）

```bash
# 1. 写一条记忆
bash scripts/seed.sh
# 2. 重启 PowerContext 容器
docker restart dify-powercontext
# 3. 等待就绪后查看
sleep 10 && bash scripts/show_memory.sh
```

### 预期表现

- 重启后 `scope_id` 不变、条目仍在 → 记忆是**落盘持久化**的，不依赖进程内存。
- （因为 `scope_id` 由幂等键决定，重启后重新调用 `POST /v1/scopes` 仍会拿到同一个 id。）

---

## 6. 自动化脚本一览

| 脚本 | 用途 | 依赖 |
| --- | --- | --- |
| `scripts/seed.sh` | 初始化记忆状态 | PowerContext |
| `scripts/reset.sh` | 重置记忆状态（软/硬） | PowerContext |
| `scripts/show_memory.sh` | 列出全部记忆条目（取证） | PowerContext |
| `scripts/verify_cross_session.sh` | PowerContext 层跨会话验证 | PowerContext |
| `scripts/export_memory.sh` | 导出记忆存档（scope + 条目 + prepare 召回 + search） | PowerContext |
| `scripts/e2e_cross_session.sh` | 端到端跨会话验证 | PowerContext + Dify |
| `scripts/chat.sh` | 调一次已发布的应用 | Dify 应用 API Key |
| `scripts/import_dsl.sh` | 导入 DSL / 发布 / 申请 API Key | Dify 控制台 |
| `scripts/collect_node_runs.sh` | 采集 Dify 节点执行记录（取证） | Dify 控制台 |
| `scripts/render_workflow_svg.py` | 由 DSL 渲染编排示意图 | Python + PyYAML |

---

## 7. 已采集的证据

| 文件 | 说明 |
| --- | --- |
| `evidence/e2e-cross-session.txt` | 样例 A 的完整执行输出（含时间戳、两个不同的 conversation_id、**5/5 断言结果**） |
| `evidence/session-b-answer.txt` | 会话 B 的原始回答（阿铁主动提起旧账的那句台词 + 状态条） |
| `evidence/powercontext-memory-export.txt` | PowerContext 记忆条目全量导出（entry_id / entry_version_id / state / 原文）+ `prepare` 召回结果 + `search` 命中 |
| `evidence/powercontext-container.log` | PowerContext 容器访问日志（含写入与读取的时间戳顺序） |
| `evidence/dify-node-runs.md` | 会话 A / 会话 B / WebApp 那次的 Dify 节点执行记录（每个 HTTP 节点的请求与响应摘要），并附工作流里 5 个 PowerContext 调用点的清单 |
| `evidence/workflow-graph.svg` | 由 `dsl/grudge-npc-chatflow.yml` 渲染的编排示意图（14 节点 / 15 连线） |
| `evidence/screenshots/01-webapp-setup.png` | WebApp 开局设置页（玩家ID / 本轮行动 / 详细描述） |
| `evidence/screenshots/02-webapp-cross-session.png` | 全新浏览器会话里阿铁主动提起旧账（关键图） |
| `evidence/screenshots/03-dify-console-logs.png` | Dify 控制台「日志」页，6 次运行全部 SUCCESS |
| `evidence/screenshots/04-dify-workflow-canvas.png` | Dify 控制台工作流画布（真实界面） |
| `evidence/screenshots/05-workflow-graph.png` | 编排示意图的位图版 |

---

## 8. 复现时可能踩到的坑

1. **WebApp 卡在"正在思考"**：Dify 的 WebApp / 流式回复走 celery worker。随机化 `REDIS_PASSWORD` 后如果没同步 `CELERY_BROKER_URL`，worker 会报 `invalid username-password pair`，WebApp 就一直没反应（但 Service API 正常）。检查 `docker logs docker-worker-1` 里有没有 `Connected to redis://...`。
2. **`entries/list` 返回 400**：这个接口**没有** `limit` 参数，多传会被 API 契约校验拒绝。
3. **`retire` 返回 409 revision_conflict**：每 retire 一条，Memory Artifact 的 revision +1，必须每次都重新 `list` 拿到最新 citation 再 retire。
4. **WSL 里跑批时容器被杀**：如果 WSL 发行版被关掉，dockerd 也会停，容器随之重启。长任务前先用 `wsl -d Ubuntu-24.04 --exec sleep infinity` 把发行版保活。
