#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 重置记忆状态（把存档恢复到"刚认识"的样子）
#
#   ./reset.sh        软重置：retire 掉全部 active 条目，再写入一条初始状态
#                     （PowerContext 服务不中断，适合反复做验证样例）
#   ./reset.sh --hard 硬重置：停服务 + 删除 SQLite 库文件，下次启动是全新存档
#
# 用法：
#   PLAYER_ID=player-1 ./reset.sh
# ---------------------------------------------------------------------------
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pc_common.sh"

HARD=0
[ "${1:-}" = "--hard" ] && HARD=1

if [ "${HARD}" = "1" ]; then
  DB_DIR="${POWERCONTEXT_HOME:-${HOME}/.powercontext}"
  DB_FILE="${DB_DIR}/powercontext.db"
  log "硬重置：删除 ${DB_FILE}（需要手动重启 PowerContext 服务）"
  if [ -f "${DB_FILE}" ]; then
    rm -f "${DB_FILE}" "${DB_FILE}-wal" "${DB_FILE}-shm"
    log "数据库文件已删除"
  else
    log "数据库文件不存在，无需删除"
  fi
  rm -f "${STATE_DIR}"/scope_*.txt
  log "已清除本地 scope 缓存"
  exit 0
fi

log "软重置：清空 ${PLAYER_ID} 的存档"
pc_retire_all
rm -f "${STATE_DIR}/scope_${PLAYER_ID}.txt"
scope_id="$(pc_ensure_scope)"
log "重建后的 scope_id = ${scope_id}"

pc_remember "npc_state" \
  "初始状态：${NPC_NAME}是镇上脾气最硬的铁匠，记仇=0，恩情=0，对${PLAYER_NAME}态度中立，尚未结怨也未受恩。" \
  "初始化存档" >/dev/null
log "已重新写入初始状态记忆"

echo
echo "===== 重置后的存档内容 ====="
pc_render entries "$(pc_list_entries true)" --inactive
