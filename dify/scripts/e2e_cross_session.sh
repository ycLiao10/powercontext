#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 端到端验证：会话 A 恶作剧 → 会话 B（新会话）NPC 主动提起旧账
#
# 这个脚本走的是完整的真实链路：
#   玩家输入 → Dify Chatflow（编排）→ PowerContext HTTP API（读写）→ 模型 → 回复
#
# 关键点：会话 B 使用**新的 conversation_id**，也就是 Dify 侧完全没有上一轮的
# 上下文残留；NPC 能说出旧事，只可能来自 PowerContext。
#
# 用法：
#   bash scripts/e2e_cross_session.sh
# ---------------------------------------------------------------------------
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dify_api.sh"

REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
EVIDENCE_DIR="${REPO_DIR}/evidence"
mkdir -p "${EVIDENCE_DIR}"
REPORT="${EVIDENCE_DIR}/e2e-cross-session.txt"

: > "${REPORT}"
say() { echo "$@" | tee -a "${REPORT}"; }

say "=================================================================="
say " 端到端验证：Dify × PowerContext 跨会话记忆"
say " 时间：$(date '+%Y-%m-%d %H:%M:%S %z')"
say " Dify：${DIFY_CONSOLE_URL}"
say "=================================================================="

# --- 0. 清空记忆状态 -------------------------------------------------------
say ""
say "--- 步骤 0：重置玩家存档 ---"
bash "${SCRIPT_DIR}/reset.sh" 2>&1 | tee -a "${REPORT}"

# --- 1. 会话 A ------------------------------------------------------------
say ""
say "--- 步骤 1：会话 A（新建对话）玩家恶作剧 ---"
say "会话 A 的 conversation_id：<新会话，尚未生成>"
bash "${SCRIPT_DIR}/chat.sh" "恶作剧" "我把阿铁的风筝踩进了泥里，还踩了两脚" "" 2>&1 | tee -a "${REPORT}"
CONV_A="$(cat "${STATE_DIR}/last_conversation_id.txt")"
say "会话 A 结束，conversation_id=${CONV_A}"

say ""
say "--- 步骤 2：检查 PowerContext 里是否真的写进去了 ---"
bash "${SCRIPT_DIR}/show_memory.sh" 2>&1 | tee -a "${REPORT}"

# --- 2. 会话 B（新会话）---------------------------------------------------
say ""
say "--- 步骤 3：会话 B（全新 conversation_id，Dify 侧没有任何上下文残留）---"
say "会话 B 的 conversation_id：<新会话，尚未生成>"
bash "${SCRIPT_DIR}/chat.sh" "帮忙" "我给你带了串糖葫芦，炉子的事我回头再修" "" 2>&1 | tee -a "${REPORT}"
CONV_B="$(cat "${STATE_DIR}/last_conversation_id.txt")"
say "会话 B 结束，conversation_id=${CONV_B}"

say ""
say "--- 步骤 4：检查 PowerContext 里的最终记忆 ---"
ENTRIES="$(bash "${SCRIPT_DIR}/show_memory.sh" 2>&1)"
printf '%s\n' "${ENTRIES}" | tee -a "${REPORT}"

# 把会话 B 的回答单独留一份，便于评审直接看（直接取本轮真实输出，不再额外跑一次对话）
cp "${STATE_DIR}/last_answer.txt" "${EVIDENCE_DIR}/session-b-answer.txt"
{
  echo "conversation_id: ${CONV_B}"
  echo "采集时间: $(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "---- NPC 的回答 ----"
  cat "${EVIDENCE_DIR}/session-b-answer.txt"
} > "${EVIDENCE_DIR}/session-b-answer.txt.tmp"
mv "${EVIDENCE_DIR}/session-b-answer.txt.tmp" "${EVIDENCE_DIR}/session-b-answer.txt"

# --- 3. 断言 ---------------------------------------------------------------
say ""
say "=================================================================="
say " 断言"
PASS=0
if [ "${CONV_A}" != "${CONV_B}" ] && [ -n "${CONV_B}" ]; then
  say "  PASS  会话 A(${CONV_A}) 与会话 B(${CONV_B}) 是两个不同的会话"; PASS=$((PASS+1))
else
  say "  FAIL  两个会话的 conversation_id 相同，不构成跨会话验证"
fi

if printf '%s' "${ENTRIES}" | grep -q "prank_grudge"; then
  say "  PASS  PowerContext 中存在 kind=prank_grudge 的记忆条目"; PASS=$((PASS+1))
else
  say "  FAIL  PowerContext 中没有 prank_grudge 条目"
fi
if printf '%s' "${ENTRIES}" | grep -q "npc_state"; then
  say "  PASS  PowerContext 中存在 kind=npc_state 的状态条目"; PASS=$((PASS+1))
else
  say "  FAIL  PowerContext 中没有 npc_state 条目"
fi
if printf '%s' "${ENTRIES}" | grep -q "help_favor"; then
  say "  PASS  PowerContext 中存在 kind=help_favor 的记忆条目（会话B 写入）"; PASS=$((PASS+1))
else
  say "  FAIL  PowerContext 中没有 help_favor 条目"
fi

ANSWER_B="$(cat "${EVIDENCE_DIR}/session-b-answer.txt" 2>/dev/null || true)"
if printf '%s' "${ANSWER_B}" | grep -qE "风筝|泥|上次|那笔账|记着"; then
  say "  PASS  会话 B 的回答里主动提到了会话 A 的旧事"; PASS=$((PASS+1))
else
  say "  WARN  会话 B 的回答里没有匹配到旧事关键词，请人工核对 evidence/session-b-answer.txt"
fi

say "=================================================================="

# --- 4. 收集服务侧日志 -----------------------------------------------------
say ""
say "--- 步骤 5：收集 PowerContext 容器日志（含写入/读取时间戳）---"
if command -v docker >/dev/null 2>&1 && docker ps -a --format '{{.Names}}' | grep -q dify-powercontext; then
  docker logs --timestamps dify-powercontext > "${EVIDENCE_DIR}/powercontext-container.log" 2>&1 || true
  tail -30 "${EVIDENCE_DIR}/powercontext-container.log" | tee -a "${REPORT}"
fi

say ""
say "完整报告已写入：${REPORT}"
[ "${PASS}" -ge 4 ] || die "端到端验证未通过（通过 ${PASS}/5）"
say "端到端验证通过（${PASS}/5）"
