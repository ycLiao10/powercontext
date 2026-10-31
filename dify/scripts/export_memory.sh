#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 导出 PowerContext 里的跨会话记忆存档（取证用）
#
# 产出 evidence/powercontext-memory-export.txt，内容包含：
#   1. scope（存档）元信息
#   2. /v1/memory/entries/list 的原始 JSON（带 entry_id / entry_version_id / 时间戳）
#   3. 每条记忆的可读渲染
#   4. 新会话读取路径 /v1/context/prepare 的召回结果
#   5. 针对关键词的 /v1/memory/search 检索命中
#
# 用法：
#   bash scripts/export_memory.sh                # 导出到 evidence/
#   PLAYER_ID=player-2 bash scripts/export_memory.sh
# ---------------------------------------------------------------------------
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pc_common.sh"

REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
EVIDENCE_DIR="${REPO_DIR}/evidence"
mkdir -p "${EVIDENCE_DIR}"
OUT="${EVIDENCE_DIR}/powercontext-memory-export.txt"

SCOPE_ID="$(pc_ensure_scope)"
# 查询语与 Dify 工作流里 "读取跨会话记忆" 节点保持完全一致，
# 这样导出的召回结果就是会话 B 当时真正拿到的那份上下文。
PREPARE_QUERY="玩家对阿铁做过什么，最近发生了什么"
RAW_ENTRIES="$(pc_list_entries true)"
PREPARE="$(pc_prepare "${PREPARE_QUERY}" 4000)"
SEARCH="$(pc_search "风筝 泥 恶作剧" 10)"

{
  echo "=================================================================="
  echo " PowerContext 跨会话记忆存档导出"
  echo " 采集时间   : $(date '+%Y-%m-%d %H:%M:%S %z')"
  echo " 服务地址   : ${POWERCONTEXT_URL}"
  echo " 玩家标识   : ${PLAYER_ID}"
  echo " scope_id   : ${SCOPE_ID}"
  echo " 幂等键     : ${SCOPE_IDEMPOTENCY_KEY}"
  echo "=================================================================="
  echo
  echo "--- 1. scope 元信息（POST /v1/scopes，幂等键固定）---"
  echo "  title    : ${SCOPE_TITLE}"
  echo "  summary  : ${SCOPE_SUMMARY}"
  echo "  scope_id : ${SCOPE_ID}"
  echo
  echo "--- 2. entries/list 原始 JSON（POST /v1/memory/entries/list）---"
  printf '%s' "${RAW_ENTRIES}" | python3 -m json.tool --no-ensure-ascii
  echo
  echo "--- 3. 逐条记忆（可读渲染）---"
  pc_render entries "${RAW_ENTRIES}" --inactive
  echo
  echo "--- 4. 新会话读取路径：POST /v1/context/prepare ---"
  echo "  查询语： ${PREPARE_QUERY}（与 Dify 工作流里的 HTTP 节点一致）"
  pc_render prepare "${PREPARE}"
  echo
  echo "--- 5. 关键词检索：POST /v1/memory/search ---"
  echo "  查询语： 风筝 泥 恶作剧"
  pc_render search "${SEARCH}"
  echo
  echo "=================================================================="
  echo " 说明：会话 B 的回答内容全部来自上面第 4 节的召回结果，"
  echo "       而召回结果来自第 2 节中会话 A 写入的条目。"
  echo "=================================================================="
} > "${OUT}"

log "已导出：${OUT}"
cat "${OUT}"
