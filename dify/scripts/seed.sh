#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 初始化记忆状态（幂等，可重复执行）
#
# 做什么：
#   1. 连接 PowerContext，确认服务可用
#   2. 按幂等键取得（或创建）玩家的存档 scope
#   3. 写入一条"初始状态"记忆，让 NPC 在全新存档里也有开场白可说
#
# 用法：
#   PLAYER_ID=player-1 ./seed.sh
# ---------------------------------------------------------------------------
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pc_common.sh"

log "PowerContext 地址：${POWERCONTEXT_URL}"
code="$(pc_health || true)"
[ "${code}" = "200" ] || die "PowerContext 服务未就绪（/health/live 返回 ${code:-连接失败}）。
请先启动：bash scripts/start_powercontext.sh
或按 README 的说明用 docker compose 起 PowerContext。"
log "服务就绪（/health/live = 200）"

scope_id="$(pc_ensure_scope)"
log "玩家 ${PLAYER_ID} 的存档 scope_id = ${scope_id}"

existing="$(pc_list_entries false | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(1 for e in d.get("entries",[]) if e.get("kind")=="npc_state"))')"
if [ "${existing}" -gt 0 ]; then
  log "存档里已经有 ${existing} 条 npc_state 初始状态，跳过写入（需要干净状态请先跑 reset.sh）"
else
  pc_remember "npc_state" \
    "初始状态：${NPC_NAME}是镇上脾气最硬的铁匠，记仇=0，恩情=0，对${PLAYER_NAME}态度中立，尚未结怨也未受恩。" \
    "初始化存档" >/dev/null
  log "已写入初始状态记忆"
fi

echo
echo "===== 当前存档内容 ====="
pc_render entries "$(pc_list_entries false)"
