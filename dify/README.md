# 记仇的 NPC：铁匠阿铁

> **Dify × OceanBase 赛题作品** —— Just for Fun——用 Dify 做一个好玩的智能应用
>
> 技术选型：**Dify（核心编排） + PowerContext（跨会话记忆）**，按题面说明不引入 seekdb。

一个会记仇的铁匠 NPC。玩家每轮只能选择**恶作剧**或**帮忙**，阿铁会把每一次行为记进 PowerContext 的跨会话记忆；等你下次（**新会话**）再进来，他会主动提起你上次干的坏事并开始报复——或者，若你帮过他，他别扭地道谢。

作品的"好玩"不在对话本身，而在**因果感**：这个 NPC 真的记得你，而且这份记忆不在聊天窗口里，在 PowerContext 里。

---

## 1. 一分钟看懂玩法

```
会话 A                                   会话 B（新会话）
─────────────────────────────            ─────────────────────────────
玩家：我把阿铁的风筝踩进了泥里           玩家：我给你带了串糖葫芦
阿铁：（记住了，记仇 +1）                阿铁：「哟，这不是把我风筝踩泥里
   ↓                                        那位的吗？……那串糖葫芦我收了，
   ↓  写入 PowerContext                       但账我还记着呢。」
   ↓  kind=prank_grudge                       ↑ 从 PowerContext 读到了旧账
   ↓  kind=npc_state（记仇=1）                ↓ 再写入 kind=help_favor
```

体验入口（两个都行）：

- **公开 WebApp**：`http://127.0.0.1/chat/<code>`，其中 `<code>` 在 `bash scripts/import_dsl.sh` 导入后打印（也可以在控制台「访问入口」里复制）。
- **Dify 控制台**：`http://127.0.0.1/app/<app_id>/workflow` 看编排，`http://127.0.0.1/app/<app_id>/logs` 看运行日志。

> Dify 1.18 的应用页路由是 `/app/<app_id>/...`（1.16 及以前是 `/apps/<app_id>/...`），照着 1.18 的控制台地址栏走就行。


---

## 2. 技术分工

| 组件 | 承担什么 | 在哪里能看到 |
| --- | --- | --- |
| **Dify 1.18.0**（Chatflow，14 个节点） | 核心流程编排：输入处理、模型调用、条件判断、5 次 PowerContext HTTP 调用、结果输出 | `dsl/grudge-npc-chatflow.yml`、Dify 运行日志 |
| **PowerContext 1.2.0**（HTTP API） | 跨会话记忆的**唯一真相来源**：存玩家行为、存 NPC 态度、读回给下一轮 | `evidence/` 里的 entry_id / 时间戳 |

Dify 侧的完整链路（详见 `docs/design.md`）：

```
开始
 └─▶ [HTTP] 取得存档 scope          POST /v1/scopes
      └─▶ [Code] 解析 scope_id
           ├─▶ [HTTP] 读取跨会话记忆  POST /v1/context/prepare
           └─▶ [HTTP] 列出记忆条目    POST /v1/memory/entries/list
                └─▶ [Code] 还原 NPC 状态（回合 / 记仇 / 恩情 / 态度 / 历史）
                     └─▶ [If-Else] 有旧账吗？
                          ├─ true  ─▶ [LLM] 阿铁反应（有旧账）──┐
                          └─ false ─▶ [LLM] 阿铁反应（初次见面）─┤
                                                      ▼
                                          [Variable-Aggregator] 合并模型输出
                                                      ▼
                                          [Code] 合并状态（确定性数值 + 要写入的记忆）
                                                      ▼
                                          [HTTP] 写入本轮记忆   POST /v1/memory/remember
                                                      ▼
                                          [HTTP] 写入 NPC 状态   POST /v1/memory/remember
                                                      ▼
                                              回应（台词 + 状态条）
```

**关键设计**：模型只负责"说话"，状态数值由 Code 节点按确定性规则算，状态的真值从 PowerContext 读回。这样"台词里的态度"和"状态条里的数字"永远不会打架。

---

## 3. 目录结构

```
dify/
├── README.md                       本文件
├── .env.example                    配置占位值（不含真实 Key）
├── .gitignore                      忽略运行时状态目录 .state/
├── dsl/
│   └── grudge-npc-chatflow.yml     Dify DSL（Chatflow，导入用）
├── deploy/
│   ├── Dockerfile.powercontext     PowerContext 的最小镜像（官方没发镜像）
│   └── docker-compose.override.yaml  给 Dify 官方 compose 加一个 powercontext 服务
├── docs/
│   ├── design.md                   玩法设计、状态机、记忆策略
│   └── verification.md             验证样例：初始状态 / 输入序列 / 预期表现 / 重置方法
├── evidence/                       运行证据（见第 6 节）
│   ├── e2e-cross-session.txt       端到端验证完整输出（含 5/5 断言）
│   ├── session-b-answer.txt        会话 B 的原始回答
│   ├── powercontext-memory-export.txt  PowerContext 记忆全量导出
│   ├── powercontext-container.log  PowerContext 服务端访问日志（带时间戳）
│   ├── dify-node-runs.md           Dify 节点执行记录
│   ├── workflow-graph.svg          由 DSL 渲染的编排示意图
│   └── screenshots/                界面截图
└── scripts/
    ├── pc_common.sh                PowerContext 公共封装（scope/记忆读写/状态解析）
    ├── pc_render.py                JSON 响应渲染器
    ├── seed.sh                     初始化记忆状态
    ├── reset.sh                    重置记忆状态（软/硬两种）
    ├── show_memory.sh              查看全部记忆条目（取证用）
    ├── verify_cross_session.sh     PowerContext 层的跨会话验证（不依赖 Dify）
    ├── start_powercontext.sh       启动 PowerContext（宿主机方式）
    ├── setup_dify_env.sh           准备 Dify 的 .env（随机密钥 + 关键开关）
    ├── start_dify.sh / stop_dify.sh 起停 Dify + PowerContext
    ├── dify_api.sh                 Dify 控制台 / 应用 API 封装
    ├── configure_model.sh          安装模型插件并配置 provider 凭据
    ├── import_dsl.sh               导入 DSL、发布、申请 API Key
    ├── chat.sh                     调用已发布应用跑一轮对话
    ├── e2e_cross_session.sh        端到端验证（会话 A → 会话 B）
    ├── export_memory.sh            导出 PowerContext 记忆存档（取证）
    ├── collect_node_runs.sh        采集 Dify 节点执行记录（取证）
    └── render_workflow_svg.py      由 DSL 渲染编排示意图
```

---

## 4. 运行环境

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Dify | **1.18.0**（2026-10-23 发布） | 官方 Docker Compose 栈，镜像 `langgenius/dify-api:1.18.0` 等 |
| PowerContext | **1.2.0** | PyPI 包 `powercontext[server]`，本作品把它打进一个最小镜像 |
| Docker | ≥ 24.0，Compose ≥ 2.24.0 | 本机实测 29.1.3 / 2.40.3 |
| Python | ≥ 3.11 | 仅用于跑 PowerContext 与脚本 |
| 模型 | 任意 OpenAI 兼容端点 | 本机实测 `MiniMax-M3.1-Flash` @ `https://api.minimaxi.com/v1` |
| 内存 | ≥ 4 GB（实测 7 GB 足够） | Dify 是多服务栈 |

### 网络注意事项（实测踩过的坑）

1. **Docker Hub 拉不动**：本机 `registry-1.docker.io` 直连不通。给 Docker 配镜像源即可：
   ```json
   // /etc/docker/daemon.json
   { "registry-mirrors": ["https://docker.m.daocloud.io"] }
   ```
   然后 `systemctl restart docker`。
2. **PyPI 官方源慢/不通**：`files.pythonhosted.org` 在本机不可达，构建 PowerContext 镜像时用构建参数切镜像源：
   ```bash
   export PYPI_INDEX=https://mirrors.aliyun.com/pypi/simple/
   export PYPI_TRUSTED_HOST=mirrors.aliyun.com
   ```
3. **Dify 的 SSRF 代理会拦内网地址**：Dify 的 HTTP Request 节点出站要经过 `ssrf_proxy`（squid），默认规则 `http_access deny to_private_networks` 会拦掉容器内网段。所以 `.env` 里必须配：
   ```
   SSRF_PROXY_ALLOW_PRIVATE_IPS=172.16.0.0/12,10.0.0.0/8,192.168.0.0/16
   SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=powercontext,host.docker.internal
   ```
   （`scripts/setup_dify_env.sh` 已经写好这一项。）
4. **随机化 Redis 密码后必须同步 broker URL**（本作品踩过最隐蔽的一个坑）：Dify 官方 `.env.example` 里 `CELERY_BROKER_URL` 把密码写死成 `difyai123456`。如果只把 `REDIS_PASSWORD` 换成随机值，`celery` worker 会连不上 Redis：
   ```
   kombu.exceptions.OperationalError: invalid username-password pair or user is disabled.
   ```
   表现很有迷惑性——**Service API 的 blocking 调用完全正常**（工作流在 API 进程内跑），但 **WebApp / 流式回复 / 控制台调试全部卡在"正在思考"**，因为它们要经 celery 派发。
   `scripts/setup_dify_env.sh` 现在会自动把 `CELERY_BROKER_URL` 改成与 `REDIS_PASSWORD` 一致：
   ```
   CELERY_BROKER_URL=redis://:<与 REDIS_PASSWORD 相同>@redis:6379/1
   ```
   如果你已经改过密码又没同步，就手动改这一行，然后 `docker compose up -d api worker worker_beat`。

---

## 5. 从零复现（评审可按此逐步执行）

> 全程约 15–25 分钟，主要时间在拉镜像。命令以 Linux/WSL 为例。

### 步骤 0：准备

```bash
# 0.1 下载 Dify 1.18.0
curl -L -o dify.tar.gz https://codeload.github.com/langgenius/dify/tar.gz/refs/tags/1.18.0
tar -xzf dify.tar.gz && mv dify-1.18.0 ~/dify
export DIFY_DIR=~/dify

# 0.2 本仓库
cd <本仓库> && git clone -b 2026-competition-ecosystem https://github.com/ycLiao10/powercontext.git
cd powercontext/dify

# 0.3 配置（把 .env.example 复制成 .env 并填模型 Key）
cp .env.example .env && vi .env      # 至少填 MODEL_API_KEY、DIFY_PASSWORD、DIFY_DIR
set -a && source .env && set +a
```

### 步骤 1：准备 Dify 的 .env（随机生成密钥，不需要手填）

```bash
bash scripts/setup_dify_env.sh
```

它会做三件事：生成随机 `SECRET_KEY`/数据库密码、打开 SSRF 内网放行、换用 qdrant 作为向量库（weaviate 镜像在国内不好拉），并把 `deploy/` 里的两个文件复制到 `~/dify/docker/`。

### 步骤 2：起栈（Dify + PowerContext 同一网络）

```bash
bash scripts/start_dify.sh
```

- 控制台：`http://127.0.0.1`
- PowerContext：`http://127.0.0.1:8000`（`/v1/capabilities`、`/health/live` 可探活）
- 想单独用宿主机跑 PowerContext：`bash scripts/start_powercontext.sh --daemon`（此时把 Dify 环境变量 `POWERCONTEXT_BASE_URL` 改成宿主机可达地址即可）

### 步骤 3：配置模型

```bash
bash scripts/configure_model.sh
```

装 OpenAI-API-compatible 插件 → 写入 `MODEL_BASE_URL` / `MODEL_API_KEY` → 校验。
也可以在控制台 `设置 → 模型供应商 → OpenAI-API-compatible` 手动配，效果等价。

### 步骤 4：导入 DSL 并发布

```bash
bash scripts/import_dsl.sh
```

脚本会打印 `app_id`，并把 `app_id`、应用 API Key 写到 `dify/.state/`。

**手动导入也可以**：控制台 → `工作室` → `创建应用` → `导入 DSL` → 选 `dsl/grudge-npc-chatflow.yml`。

### 步骤 5：初始化记忆状态

```bash
bash scripts/seed.sh
```

给默认玩家 `player-1` 建存档并写入一条初始状态记忆。

### 步骤 6：体验 & 验证

```bash
bash scripts/e2e_cross_session.sh      # 自动跑完"会话 A → 会话 B"
```

或手动（推荐评审也手动来一次）：

1. 打开 WebApp `http://127.0.0.1/chat/<code>`（`<code>` 见 `scripts/import_dsl.sh` 的输出）
2. **新建对话**，选「恶作剧」，写"我把阿铁的风筝踩进了泥里"，发送 → 看到阿铁的反应和状态条 `记仇 1`
3. 点 **新建对话**（关键：不是继续上一个对话）
4. 选「帮忙」，写"我给你带了串糖葫芦"，发送 → **阿铁主动提起风筝那件事**
5. 另开一个终端确认记忆确实落库：
   ```bash
   bash scripts/show_memory.sh
   ```

> WebApp 走的是 Dify 的异步链路（celery worker + Redis）。如果第 4 步卡在"正在思考"不动，
> 多半是 `CELERY_BROKER_URL` 与 `REDIS_PASSWORD` 不一致（见第 4 节的网络注意事项）。
> 只想看"跨会话记忆"是否成立，用 Service API 也一样：`bash scripts/chat.sh "帮忙" "我给你带了串糖葫芦" ""`。

### 步骤 7：清理

```bash
bash scripts/stop_dify.sh          # 停服务（保留数据）
bash scripts/stop_dify.sh --purge  # 连数据卷一起删
bash scripts/reset.sh              # 只清记忆，保留环境
```

---

## 6. 交付物与证据索引

| 文件 | 内容 |
| --- | --- |
| `dsl/grudge-npc-chatflow.yml` | Dify DSL（Chatflow，14 节点 / 15 连线）。从 Dify 1.18.0 导出，可直接导入 |
| `docs/design.md` | 玩法、状态机、记忆策略、节点职责 |
| `docs/verification.md` | 验证样例：初始状态 / 输入序列 / 预期表现 / 重置方法 / 自动化脚本 |
| `evidence/e2e-cross-session.txt` | 端到端验证的完整输出（含时间戳、两个不同的 conversation_id、**5/5 断言**） |
| `evidence/session-b-answer.txt` | 会话 B 的原始回答（阿铁主动提起旧账的那句台词 + 状态条） |
| `evidence/powercontext-memory-export.txt` | PowerContext 记忆条目全量导出（含 entry_id / entry_version_id）+ prepare 召回 + search 命中 |
| `evidence/powercontext-container.log` | PowerContext 服务端访问日志（写入/读取的时间戳） |
| `evidence/dify-node-runs.md` | Dify 节点执行记录（每次运行 13 个节点的输入/输出摘要 + 工作流里 5 个 PowerContext HTTP 调用点清单） |
| `evidence/workflow-graph.svg` | 由 DSL 渲染的编排示意图（不装 Dify 也能看懂流程） |
| `evidence/screenshots/01-webapp-setup.png` | WebApp 的开局设置（玩家ID / 本轮行动 / 详细描述） |
| `evidence/screenshots/02-webapp-cross-session.png` | **关键图**：全新浏览器会话里，阿铁主动提起"上次你把我风筝踩进泥里" |
| `evidence/screenshots/03-dify-console-logs.png` | Dify 控制台「日志」页，6 次运行全部 SUCCESS |
| `evidence/screenshots/04-dify-workflow-canvas.png` | Dify 控制台的工作流画布（真实界面） |
| `evidence/screenshots/05-workflow-graph.png` | `workflow-graph.svg` 的位图版，方便直接看 |

### 本机实测结果（2026-10-31，Dify 1.18.0 + PowerContext 1.2.0）

| 项目 | 实测值 |
| --- | --- |
| 容器状态 | 15 个容器全部 healthy |
| 玩家存档 scope | `scp_23kagjzeq34hbbxg7pbjhwf47r`（幂等键 `dify-npc-player-1`） |
| 会话 A | `conversation_id = f4349dcd-8be5-40e2-b810-a18b01a22925`（恶作剧 → `第 1 回合 · 记仇 1 · 恩情 0 · 态度 记恨`） |
| 会话 B | `conversation_id = a1366cbe-b9dc-4e26-9006-0c6228c6eb53`（**不同 conversation**，帮忙 → `第 2 回合 · 记仇 0 · 恩情 1 · 态度 中立`） |
| 会话 B 的台词 | 「糖葫芦？拿来吧。**上次你把我风筝踩进泥里，还踩了两脚**，我可还记着呢；炉子敢再拖，我就把你焊在炉边。」 |
| 写入的恶作剧条目 | `entry_id = mem_ent_fbeaea9893b348f7892a8d0764d38d48` |
| 写入的状态条目 | `entry_id = mem_ent_f8a59c8d84664e09ab16fb9d7d6e98ae`（第 1 回合后状态） |
| 断言结果 | 端到端 **5/5 PASS**；PowerContext 层独立复核 **3/3 PASS** |
| WebApp 再开一局 | `第 3 回合 · 记仇 -1 · 恩情 2 · 态度 亲近`，台词仍提到风筝旧账 |

---

## 7. 导入 DSL 时需要重新绑定的配置

DSL 里**不含任何密钥**。导入到新环境后，需要处理这几项：

| 项 | 怎么处理 | 说明 |
| --- | --- | --- |
| 模型 provider 凭据 | `设置 → 模型供应商 → OpenAI-API-compatible` 填 base_url + API Key | 插件缺失时导入页面会提示"一键安装" |
| 应用环境变量 | `dsl` 里已声明三个：`POWERCONTEXT_BASE_URL` / `POWERCONTEXT_TOKEN` / `POWERCONTEXT_DEFAULT_PLAYER`，导入后在应用的环境变量里确认即可 | `POWERCONTEXT_BASE_URL` 必须改成**评审环境里 Dify 容器能访问到的地址** |
| 插件依赖 | `openai_api_compatible` 模型插件 | 导入时可一键安装 |
| 知识库 | 本作品**不使用**知识库 | 无需处理 |

---

## 8. 已知限制

1. PowerContext 最小服务用全文检索（`mode: fts`），未配向量模型时召回依赖词面重合；玩家换一种说法描述同一件事时，`prepare` 可能召回不全（`entries/list` 那条路是全量的，不受影响）。
2. `entries/list` 没有分页参数，条目极多时响应会变大；本作每次验证十几条以内，没有问题。
3. 状态只有一个主轴（记仇值）与辅助轴（恩情值），没有做多属性角色养成。
4. 依赖 Dify 的 Code 节点（跑在 `dify-sandbox` 容器里），如果评审环境禁用了 sandbox，状态合并节点会失败。
5. PowerContext 官方没有发布 Docker 镜像，本作品自建了 `deploy/Dockerfile.powercontext`（等价于官方文档的 `pip install "powercontext[cli,server]"`）。
6. `scripts/dify_api.sh` 里内置了一个本地演示用的默认管理员密码（`Dify-Local-2026!`），只适用于本机自建栈；部署到共享环境前请在 `.env` 里改成自己的密码（`.env` 已被 `.gitignore` 忽略）。

---

## 9. 复用与来源说明

- Dify：Apache-2.0，<https://github.com/langgenius/dify>（1.18.0）
- PowerContext：Apache-2.0，<https://github.com/oceanbase/powercontext>（1.2.0）
- 题面参考的公开工作流（`dzh-gitcode/dify-workflows`、`wwwzhouhui/dify-for-dsl`）**仅作设计参考**，本作品未复制其 DSL。
- 本作品新增内容：DSL 编排、PowerContext 集成脚本、玩法与状态机设计、验证脚本。
