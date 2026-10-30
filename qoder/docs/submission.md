# 提测材料 · 文献阅读工作台

> 本文件是提测表单的填写底稿和判题证据索引。最后核对：2026-10-30。
> 每次重新提测前，先跑一遍第 4 节的 `preflight.py`，再把第 1 节字段原样填进提测表单。

## 1. 提测表单（可直接粘贴）

| 字段 | 内容 |
| --- | --- |
| 赛题 | 2026 OceanBase 数据库大赛 · Qoder 赛道 |
| 作品名 | 文献阅读工作台（Literature Desk） |
| 一句话介绍 | 把散落的 `.md`/`.txt` 资料导入后做「词法 + 本地向量」混合检索，每条结果给出文献名、第几段、字符区间和原文；PowerContext 负责记忆文献与阅读偏好，并在下次检索时参与排序。 |
| 仓库地址 | <https://github.com/ycLiao10/powercontext> |
| 分支 | `2026-competition-ecosystem` |
| Commit SHA | `<push 后执行 git rev-parse HEAD，或看 artifacts/preflight.json 的 submission.commit>` |
| 作品目录 | `qoder/` |
| 项目网站 | <https://literature-desk-olizvin1uxl.qoder.zone> |
| 指定技术 | PowerContext 1.2.0（官方包，未改源码），通过 HTTP `/v1/scopes`、`/v1/sources/content`、`/v1/memory/remember`、`/v1/memory/search` 调用 |
| 嵌入模型 | 本地 `BAAI/bge-small-zh-v1.5`（fastembed + onnxruntime，512 维，无需 GPU、无需云端 Key） |
| 运行环境 | Python 3.11+，Windows / Linux；应用本体只用标准库 |
| 验证入口 | `qoder/tools/verify_powercontext.py`、`qoder/tools/verify_literature.py`、`qoder/tools/preflight.py` |

## 2. 功能宣称与判题证据对照

| # | 宣称 | 代码位置 | 复现方式 | 证据字段 | 站点对应 |
| --- | --- | --- | --- | --- | --- |
| 1 | 混合检索：SQLite FTS5 词法通道 + 本地向量通道，按 RRF（k=60）融合名次 | `qoder/app/search.py`、`qoder/app/store.py` | `python tools/verify_literature.py` | `steps[].channels_used` 同时含 `lexical` 与 `vector` | 演示 / 架构 |
| 2 | 换一种说法也能召回：语义案例的命中只来自向量通道 | 同上 | 同上 | 语义案例 `results[0].channels == ["vector"]` | 演示「换说法」 |
| 3 | 每条结果可定位出处：文献名 + 第 N 段 + 字符区间 + 原文片段 | `qoder/app/store.py` 的检索返回、`qoder/app/index.html` 渲染 | 同上 | `results[].title / ordinal / offset` | 功能 03 |
| 4 | PowerContext 真实参与排序：导入写来源与记忆，检索读记忆，命中文献 1.15× 加权 | `qoder/app/ingest.py`、`qoder/app/pc_client.py`、`qoder/app/search.py` | 同上 | `memory_reused=true`、`results[].memory_boost=true` | 架构、演示（记忆角标） |
| 5 | PowerContext 自身用上了本地嵌入：检索模式从 `auto, fts` 扩展到 `auto, fts, vector, hybrid` | `qoder/.env.example`、`qoder/tools/embed_server.py` | `python tools/verify_powercontext.py` | `search_mode=hybrid`、`hits[].matched_by=["vector"]`、`capabilities.search_modes` 含 `vector`/`hybrid` | 验证 |
| 6 | 嵌入服务是 OpenAI 兼容接口，工作台与 PowerContext 共用 | `qoder/tools/embed_server.py`、`qoder/app/embedding.py` | `curl 127.0.0.1:8081/health` | `dimension=512`；`/api/health` 的 `embedding.available=true` | 验证 facts |
| 7 | 站点展示的是真实运行数据，不是手写文案 | `qoder/tools/build_site_data.py` 从 `qoder/artifacts/*.json` 导出 | 重新运行导出脚本后对比 | `qoder/site/data/demo.json` 的查询、分数、通道与证据一致 | 全部 |

## 3. 评委复现路径（约 10 分钟）

```bash
# 终端 A：本地嵌入服务
pip install "fastembed==0.9.1" truststore
cd qoder && python tools/embed_server.py --port 8081
curl -s http://127.0.0.1:8081/health            # dimension=512

# 终端 B：PowerContext（示例 .env 已含 embedding 段，改成注释里的实际行即可）
uv tool install --force "powercontext[cli,server]==1.2.0"
cd qoder && cp .env.example .env && powercontext config validate --env-file .env
powercontext server run --env-file .env
curl -s http://127.0.0.1:8000/v1/capabilities    # search_modes 含 vector、hybrid

# 终端 C：工作台与验证
cd qoder && python -m app --port 8080
python tools/verify_powercontext.py             # PASS: 记忆写入与召回均生效
python tools/verify_literature.py               # passed=true，语义案例仅向量通道命中
```

浏览器打开 <http://127.0.0.1:8080>，导入页面内置示例，再点「换说法」类查询即可看到仅向量通道命中的结果。

## 4. 提交前一键自检

```bash
cd qoder
python tools/preflight.py --live
```

检查项：工作区是否干净、提测字段（仓库/分支/SHA/站点）、必需文件、`.env` 是否被跟踪、凭据与本机绝对路径泄漏、公开站点（首页/数据/样式/脚本）可达且内容匹配、两份验证证据是否 `passed=true`、本机三个服务是否在线。结果写入 `qoder/artifacts/preflight.json`，退出码 0 才提测。

## 5. 提交前自检表

- [ ] 提测表单的仓库、分支、Commit SHA 与 `preflight.json` 的 `submission` 完全一致，SHA 是已推送到远端的那个
- [ ] 工作区干净（`git status --porcelain -- qoder` 无输出），提测内容与该 SHA 一一对应
- [ ] 站点 <https://literature-desk-olizvin1uxl.qoder.zone> 匿名可访问（未登录 200），且内容与当前 SHA 对应
- [ ] 若改动了检索/演示数据：重跑两份验证 → `python tools/build_site_data.py` → 重新发布站点
- [ ] `qoder/.env` 未提交，`.env.example` 全部是占位值（`replace-with-your-key`、`<user>`）
- [ ] 仓库内无 GitHub PAT / API Key / 密码 / 本机绝对路径（`preflight.py` 的 `secrets.findings` 为空）
- [ ] PowerContext 是官方 `powercontext==1.2.0`，不是自制替代品；`qoder/app` 只通过 `/v1` 调用它
- [ ] 两份证据文件 `passed=true`，且 `qoder/docs/qoder-dev-log.md` 记录了当天验证时间
- [ ] 遵守提测频率限制：每题每天最多 10 次、两次之间不少于 10 分钟（按赛题规则）

## 6. 常见拒收原因与本作品的对应处理

| 风险 | 本作品的处理 |
| --- | --- |
| 站点打不开 / 需要登录 | 站点访问策略已设为公开，`preflight.py` 用无凭据请求校验首页、数据、样式、脚本 |
| 代码与站点对不上 | 站点数据由 `build_site_data.py` 从验证证据导出，页面上的分数、通道、记忆角标均来自 JSON |
| 声称用指定技术但代码里没调用 | `qoder/app/pc_client.py` 是唯一的 PowerContext 客户端；`verify_powercontext.py` 输出 `search_mode=hybrid`、`matched_by=["vector"]` |
| 提交凭据 | `.env` 在 `.gitignore` 内；`preflight.py` 扫描将入库文件；`.env.example` 只保留占位值 |
| 分支 / SHA 与表单不一致 | `preflight.py` 直接读取当前 HEAD 打印到 `submission`，提交时复制粘贴即可 |

> 安全提醒：此前对话中出现过的 GitHub PAT 属于已暴露凭据，请先在 GitHub 设置中吊销并重新生成；后续 push 使用本机凭据管理器或 SSH，不要把 token 写进命令、文件或提测表单。

## 7. 提测流程

1. `git add qoder && git commit -m "..." && git push origin 2026-competition-ecosystem`（用你自己的凭据）
2. `cd qoder && python tools/preflight.py` 确认通过，记录 `submission.commit`
3. 按第 1 节字段填写提测表单，粘贴站点 URL 与 SHA
4. 提交后保留 `qoder/artifacts/preflight.json` 与两份 `verify-*.json` 作为当日验证记录；次日重新验证后再提测

## 8. 使用效果（站点公开访问）

Qoder Sites 自带的访问统计，取 7 天窗口（2026-10-23 ~ 2026-10-30，统计时间 2026-10-30 10:53 +08:00）：

| 指标 | 数值 | 说明 |
| --- | --- | --- |
| PV | 26 | 页面访问量，站点上线首日 24 次、次日 2 次 |
| UV | 11 | 估算独立访客 |

数据来源为站点 `01a187c5-0d04-77ac-883b-d533a990c852` 的 `get_analytics`，`coverage=partial`（`ingestion_watermark_unavailable`），即统计覆盖不完整、实际访问量不低于上表。提交时若需要更新的数字，重新读取同一接口并替换本节表格即可。

