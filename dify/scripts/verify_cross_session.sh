#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 跨会话记忆的"PowerContext 层"验证脚本
#
# 它不依赖 Dify，直接证明一件事：
#   会话 A 写入的记忆，在会话 B（全新 HTTP 请求）能被原样召回，
#   且两者都能在服务端日志里看到带时间戳的写入 / 读取记录。
#
# 完整的端到端验证（Dify 会话 A → Dify 会话 B）见 docs/verification.md。
#
# 用法：
#   ./verify_cross_session.sh
# ---------------------------------------------------------------------------
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pc_common.sh"

WORK="${STATE_DIR}/verify"
mkdir -p "${WORK}"
STAMP="$(date '+%Y-%m-%d %H:%M:%S %z')"

echo "=================================================================="
echo " PowerContext 跨会话记忆验证"
echo " 时间：${STAMP}"
echo " 服务：${POWERCONTEXT_URL}"
echo "=================================================================="

# --- 0. 清场 --------------------------------------------------------------
log "步骤 0：重置存档"
bash "${SCRIPT_DIR}/reset.sh" >/dev/null 2>&1 || true

# --- 1. 会话 A：写入 ------------------------------------------------------
log "步骤 1：会话 A —— 玩家把阿铁的风筝踩进泥里"
remember_resp="$(pc_remember "prank_grudge" \
  "第1回合，玩家把阿铁的风筝踩进泥里，还笑出了声。阿铁记下了这笔账。" \
  "玩家恶作剧")"
entry_id="$(printf '%s' "${remember_resp}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("entry",{}).get("citation",{}).get("entry_id",""))')"
version_id="$(printf '%s' "${remember_resp}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("entry",{}).get("citation",{}).get("entry_version_id",""))')"
log "写入成功：entry_id=${entry_id}"
log "          entry_version_id=${version_id}"

log "步骤 2：会话 A —— 玩家又帮阿铁修了炉子"
pc_write_turn "help" "帮阿铁把烧坏的炉子修好了" "2" "0" "1" >/dev/null

# --- 2. 会话 B：读取 ------------------------------------------------------
log "步骤 3：会话 B —— 全新 HTTP 请求，读取历史"
prepared="$(pc_prepare "玩家对阿铁做过什么" 4000)"
printf '%s' "${prepared}" > "${WORK}/session_b_prepare.json"
pc_render prepare "${prepared}"

# --- 3. 断言 --------------------------------------------------------------
echo
echo "=================================================================="
log "断言：会话 B 召回了会话 A 写入的那条 entry"
PASS=0
if printf '%s' "${prepared}" | grep -q "${entry_id}"; then
  echo "  PASS  召回内容中包含会话A 写入的 entry_id=${entry_id}"; PASS=$((PASS+1))
else
  echo "  FAIL  召回内容里没有该 entry_id"
fi
if printf '%s' "${prepared}" | grep -q "风筝踩进泥里"; then
  echo "  PASS  召回内容包含恶作剧原文"; PASS=$((PASS+1))
else
  echo "  FAIL  没有召回恶作剧原文"
fi
if printf '%s' "${prepared}" | grep -q "烧坏的炉子修好了"; then
  echo "  PASS  召回内容包含帮忙原文"; PASS=$((PASS+1))
else
  echo "  FAIL  没有召回帮忙原文"
fi
echo "=================================================================="

echo
log "当前存档全部条目（可直接作为 evidence 附件）"
pc_render entries "$(pc_list_entries false)"

echo
log "产物已写入：${WORK}/session_b_prepare.json"
[ "${PASS}" -ge 3 ] || die "跨会话验证未通过（通过 ${PASS}/3）"
log "跨会话验证通过（${PASS}/3）"
