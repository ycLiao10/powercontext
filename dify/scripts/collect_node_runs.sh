#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 采集 Dify 侧的节点执行记录（取证用）
#
# Dify 会把每一次工作流运行拆成"节点执行"记录存到数据库里，控制台 API:
#   GET /console/api/apps/{app_id}/workflow-runs                    运行列表
#   GET /console/api/apps/{app_id}/workflow-runs/{run_id}/node-executions
#                                                                某次运行的每个节点
#
# 产出 evidence/dify-node-runs.md：把会话 A / 会话 B 两次运行的关键节点
# （HTTP 请求节点打到了 PowerContext 的哪个 URL、Code 节点算出的状态）
# 连同时间戳一起列出来，证明"记忆来自 PowerContext 而不是模型上下文残留"。
#
# 用法：
#   bash scripts/collect_node_runs.sh
# ---------------------------------------------------------------------------
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dify_api.sh"

REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
EVIDENCE_DIR="${REPO_DIR}/evidence"
mkdir -p "${EVIDENCE_DIR}"
OUT="${EVIDENCE_DIR}/dify-node-runs.md"

APP_ID_FILE="${STATE_DIR}/app_id.txt"
[ -s "${APP_ID_FILE}" ] || die "找不到 app_id（${APP_ID_FILE}），请先跑 scripts/import_dsl.sh"
APP_ID="$(cat "${APP_ID_FILE}")"
MAX_RUNS="${MAX_RUNS:-5}"

dify_ensure_login

RUNS="$(console_curl GET "/console/api/apps/${APP_ID}/workflow-runs?limit=20&triggered_from=app-run")"

# 取最近若干次运行的 id
RUN_IDS="$(printf '%s' "${RUNS}" | python3 -c '
import json, sys, time
d = json.load(sys.stdin)
runs = d.get("data") if isinstance(d, dict) else d
ids = []
for r in (runs or []):
    rid = r.get("id")
    if rid:
        ts = r.get("created_at")
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""
        ids.append((when, rid, r.get("status", ""),
                    r.get("total_steps", ""), r.get("total_tokens", "")))
for c, i, s, st, tk in ids:
    print("{}\t{}\t{}\t{}\t{}".format(c, i, s, st, tk))
')"

{
  echo "# Dify 节点执行记录"
  echo ""
  echo "> 采集时间：$(date '+%Y-%m-%d %H:%M:%S %z')  "
  echo "> 应用 ID：\`${APP_ID}\`  "
  echo "> 数据来源：Dify 控制台 API \`GET /console/api/apps/{app_id}/workflow-runs?triggered_from=app-run\`"
  echo "> 与 \`GET /console/api/apps/{app_id}/workflow-runs/{run_id}/node-executions\`  "
  echo "> 说明：\`triggered_from=app-run\` 表示这些运行来自 WebApp / Service API（也就是真实的玩家对话），而不是控制台里的调试运行。"
  echo ""
  echo "## 一、工作流运行列表（最近 ${MAX_RUNS:-5} 次）"
  echo ""
  echo "| 创建时间 | 运行 ID | 状态 | 节点步数 | Token |"
  echo "| --- | --- | --- | --- | --- |"
  while IFS=$'\t' read -r created rid status steps tokens; do
    [ -z "${rid}" ] && continue
    printf '| %s | `%s` | %s | %s | %s |\n' "${created}" "${rid}" "${status}" "${steps}" "${tokens}"
  done <<< "${RUN_IDS}"
  echo ""

  # 逐个运行展开节点
  index=0
  while IFS=$'\t' read -r created rid status steps tokens; do
    [ -z "${rid}" ] && continue
    index=$((index+1))
    [ "${index}" -gt "${MAX_RUNS:-5}" ] && break
    EXEC="$(console_curl GET "/console/api/apps/${APP_ID}/workflow-runs/${rid}/node-executions?limit=100")"
    echo "## 二、运行 #${index} 的节点明细"
    echo ""
    echo "- 运行 ID：\`${rid}\`"
    echo "- 创建时间：${created}   状态：${status}   节点步数：${steps}   Token：${tokens}"
    echo ""
    printf '%s' "${EXEC}" | python3 -c '
import json, sys

data = json.load(sys.stdin)
items = data.get("data") if isinstance(data, dict) else data
items = items or []
items = sorted(items, key=lambda x: x.get("created_at") or "")

print("| # | 节点类型 | 节点 ID | 标题 | 状态 | 耗时 | 输入摘要 | 输出摘要 |")
print("| --- | --- | --- | --- | --- | --- | --- | --- |")
for i, n in enumerate(items, 1):
    inputs = n.get("inputs") or {}
    outputs = n.get("outputs") or {}
    def brief(x):
        if isinstance(x, dict):
            x = {k: v for k, v in x.items() if k not in ("files",)}
        s = json.dumps(x, ensure_ascii=False)
        s = " ".join(str(s).split())
        return s[:220] + ("…" if len(s) > 220 else "")
    elapsed = n.get("elapsed_time")
    print("| {} | {} | `{}` | {} | {} | {} | {} | {} |".format(
        i, n.get("node_type"), n.get("node_id"), n.get("title"),
        n.get("status"), ("{}s".format(elapsed) if elapsed is not None else "-"),
        brief(inputs), brief(outputs)))
print("")
for n in items:
    node_type = n.get("node_type")
    if node_type in ("http-request", "code", "if-else"):
        print("<details><summary>节点 <code>{}</code>（{} / {}）完整输入输出</summary>".format(
            n.get("title"), n.get("node_type"), n.get("node_id")))
        print("")
        print("```json")
        print(json.dumps({"inputs": n.get("inputs"), "outputs": n.get("outputs")}, ensure_ascii=False, indent=2))
        print("```")
        print("")
        print("</details>")
        print("")
'
  done <<< "${RUN_IDS}"
} > "${OUT}"

# --- 三、工作流定义里的 PowerContext 调用点 --------------------------------
GRAPH="$(console_curl GET "/console/api/apps/${APP_ID}/workflows")"
{
  echo ""
  echo "## 三、工作流里所有指向 PowerContext 的 HTTP 节点（来自已发布的工作流定义）"
  echo ""
  echo "> 来源：\`GET /console/api/apps/${APP_ID}/workflows\`（已发布版本）"
  echo ""
  printf '%s' "${GRAPH}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
items = data.get("items") if isinstance(data, dict) else data
items = items or []
if not items:
    raise SystemExit(0)
graph = items[0].get("graph") or {}
nodes = graph.get("nodes") or []
rows = []
for n in nodes:
    data = n.get("data") or {}
    if data.get("type") != "http-request":
        continue
    body = ((data.get("body") or {}).get("data") or [])
    body_text = " ".join(str(b.get("value", "")) for b in body)
    rows.append((data.get("title", ""), n.get("id", ""),
                 (data.get("method") or "").upper(), data.get("url", ""),
                 data.get("desc", ""), body_text))
rows.sort(key=lambda r: r[0])
print("| 节点标题 | 节点 ID | 方法 | URL | 用途 | 请求体 |")
print("| --- | --- | --- | --- | --- | --- |")
for t, i, m, u, d, b in rows:
    b = " ".join(str(b).split())
    print("| {} | `{}` | {} | `{}` | {} | `{}` |".format(
        t, i, m, u, d, b if len(b) <= 300 else b[:300] + "…"))
'
} >> "${OUT}"

log "已写入：${OUT}"
