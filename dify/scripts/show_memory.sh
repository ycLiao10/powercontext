#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 查看当前存档的完整记忆条目（取证用）
#
#   ./show_memory.sh              列出 active 条目
#   ./show_memory.sh --all        连同已 retire 的条目一起列出
#
# 输出的 entry_id / entry_version_id 可以直接粘到 evidence 里，
# 用来证明"会话B 读到的就是会话A 写进去的那一条"。
# ---------------------------------------------------------------------------
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pc_common.sh"

INCLUDE=false
[ "${1:-}" = "--all" ] && INCLUDE=true

log "PowerContext=${POWERCONTEXT_URL}  玩家=${PLAYER_ID}"
pc_render entries "$(pc_list_entries "${INCLUDE}")" $([ "${INCLUDE}" = "true" ] && echo --inactive)
