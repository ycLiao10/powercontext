#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 调用已发布的 Dify 应用（service API），跑一轮对话
#
# 用法：
#   bash scripts/chat.sh "恶作剧" "我把阿铁的风筝踩进了泥里" [conversation_id]
#
# 说明：
#   - conversation_id 为空时表示开启一个"新会话"（Dify 的会话上下文互不影响）
#   - 脚本会把回答、conversation_id 以及每次调用的时间戳打印出来，便于做验证记录
# ---------------------------------------------------------------------------
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dify_api.sh"

ACTION="${1:-恶作剧}"
DETAIL="${2:-（没细说）}"
PLAYER_ID="${PLAYER_ID:-player-1}"
CONVERSATION_ID="${3:-}"
API_KEY_FILE="${STATE_DIR}/app_api_key.txt"

[ -s "${API_KEY_FILE}" ] || die "找不到应用 API Key（${API_KEY_FILE}），请先跑 scripts/import_dsl.sh"
API_KEY="$(cat "${API_KEY_FILE}")"

payload="$(python3 -c '
import json, sys
action, detail, player, conv = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
body = {
    "inputs": {"player_id": player, "action": action, "detail": detail},
    "query": f"这一轮我选择{action}：{detail}",
    "response_mode": "blocking",
    "user": "judge",
}
if conv:
    body["conversation_id"] = conv
print(json.dumps(body, ensure_ascii=False))
' "${ACTION}" "${DETAIL}" "${PLAYER_ID}" "${CONVERSATION_ID}")"

echo "=== [$(date '+%Y-%m-%d %H:%M:%S %z')] action=${ACTION} detail=${DETAIL} conversation_id=${CONVERSATION_ID:-<新会话>} ==="
response="$(app_curl POST /v1/chat-messages "${payload}" "${API_KEY}")"
printf '%s' "${response}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("conversation_id:", d.get("conversation_id"))
print("task_id        :", d.get("task_id"))
print("answer         :")
print(d.get("answer", ""))
'
printf '%s' "${response}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d.get("conversation_id", ""))
' > "${STATE_DIR}/last_conversation_id.txt"

# 单独留一份本轮回答，方便验证脚本直接引用（不必再跑一次对话）
printf '%s' "${response}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
sys.stdout.write(d.get("answer", ""))
' > "${STATE_DIR}/last_answer.txt"
