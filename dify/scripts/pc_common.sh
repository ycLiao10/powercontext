#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# PowerContext 公共配置与调用封装
#
# 本作品（《记仇的 NPC：铁匠阿铁》）把 PowerContext 当作"跨会话记忆库"：
#   - 每个玩家一个 scope（存档），scope_id 由服务端生成，这里用幂等键固定下来
#   - 每轮把玩家行为与 NPC 态度写进 Memory
#   - 新会话开始时用 /v1/context/prepare 把历史读回来，喂给 Dify 里的 LLM 节点
#
# 所有连接参数都能用环境变量覆盖，README 里给出的 .env.example 与此一致。
# ---------------------------------------------------------------------------
set -euo pipefail

POWERCONTEXT_URL="${POWERCONTEXT_URL:-http://127.0.0.1:8000}"
POWERCONTEXT_TOKEN="${POWERCONTEXT_TOKEN:-}"          # 启用鉴权时才需要
PLAYER_ID="${PLAYER_ID:-player-1}"                     # 玩家标识，决定用哪个存档
PLAYER_NAME="${PLAYER_NAME:-旅人}"
NPC_NAME="${NPC_NAME:-阿铁}"
TIMEOUT="${POWERCONTEXT_TIMEOUT:-30}"

SCOPE_TITLE="${SCOPE_TITLE:-记仇的 NPC：铁匠阿铁}"
SCOPE_SUMMARY="${SCOPE_SUMMARY:-Dify 赛题《记仇的 NPC》跨会话记忆存档 · player=${PLAYER_ID}}"
SCOPE_IDEMPOTENCY_KEY="${SCOPE_IDEMPOTENCY_KEY:-dify-npc-${PLAYER_ID}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${STATE_DIR:-${SCRIPT_DIR}/../.state}"
SCOPE_CACHE="${STATE_DIR}/scope_${PLAYER_ID}.txt"
PC_RENDER="${SCRIPT_DIR}/pc_render.py"

mkdir -p "${STATE_DIR}"

log()  { printf '\033[36m[pc]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[pc]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[pc] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 底层 HTTP ------------------------------------------------------------
_pc_curl() {
  local method="$1" path="$2" payload="${3:-}"
  local -a args=(-sS --max-time "${TIMEOUT}" -X "${method}" "${POWERCONTEXT_URL}${path}"
                 -H "Content-Type: application/json" -w $'\n%{http_code}')
  if [ -n "${POWERCONTEXT_TOKEN}" ]; then
    args+=(-H "Authorization: Bearer ${POWERCONTEXT_TOKEN}")
  fi
  if [ -n "${payload}" ]; then
    args+=(--data "${payload}")
  fi
  local raw code
  raw="$(curl "${args[@]}")" || return 1
  code="$(printf '%s' "${raw}" | tail -n1)"
  printf '%s' "${raw}" | sed '$d'
  if [ "${code}" -ge 400 ]; then
    die "${method} ${path} -> HTTP ${code}: $(printf '%s' "${raw}" | sed '$d' | head -c 400)"
  fi
  return 0
}

# --- 基础能力 -------------------------------------------------------------
pc_health() {
  curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "${POWERCONTEXT_URL}/health/live"
}

pc_capabilities() { _pc_curl GET /v1/capabilities; }

# --- scope（存档）---------------------------------------------------------
# scope_id 由服务端生成（形如 scp_xxx），请求里不能指定。
# 幂等：相同 title + summary + idempotency_key 重复调用返回同一个 scope。
pc_ensure_scope() {
  if [ -f "${SCOPE_CACHE}" ]; then
    local cached
    cached="$(cat "${SCOPE_CACHE}")"
    if [ -n "${cached}" ]; then
      printf '%s' "${cached}"
      return 0
    fi
  fi
  local body scope_id
  body="$(_pc_curl POST /v1/scopes "$(python3 -c 'import json,sys;print(json.dumps({"title":sys.argv[1],"summary":sys.argv[2],"idempotency_key":sys.argv[3]},ensure_ascii=False))' \
        "${SCOPE_TITLE}" "${SCOPE_SUMMARY}" "${SCOPE_IDEMPOTENCY_KEY}")")"
  scope_id="$(printf '%s' "${body}" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("scope_id",""))')"
  [ -n "${scope_id}" ] || die "创建 scope 失败：${body}"
  printf '%s' "${scope_id}" > "${SCOPE_CACHE}"
  printf '%s' "${scope_id}"
}

# --- Memory 读写 ----------------------------------------------------------
# kind 是自由字符串（<=128 字符），本作品用它区分用途：
#   prank_grudge —— 玩家的一次恶作剧
#   help_favor   —— 玩家的一次帮忙
#   npc_state    —— NPC 当前态度与数值状态
#   encounter    —— 一次值得记住的互动细节
pc_remember() {
  local kind="$1" text="$2" reason="${3:-}"
  local scope_id payload
  scope_id="$(pc_ensure_scope)"
  payload="$(python3 -c 'import json,sys;print(json.dumps({"scope_id":sys.argv[1],"kind":sys.argv[2],"text":sys.argv[3],"reason":sys.argv[4] or None},ensure_ascii=False))' \
            "${scope_id}" "${kind}" "${text}" "${reason}")"
  _pc_curl POST /v1/memory/remember "${payload}"
}

pc_search() {
  local query="$1" limit="${2:-10}" scope_id
  scope_id="$(pc_ensure_scope)"
  _pc_curl POST /v1/memory/search \
    "$(python3 -c 'import json,sys;print(json.dumps({"scope_id":sys.argv[1],"query":sys.argv[2],"limit":int(sys.argv[3])},ensure_ascii=False))' \
       "${scope_id}" "${query}" "${limit}")"
}

# 新会话开头用它取"历史上下文"（返回带信任边界的 PreparedContext）
pc_prepare() {
  local query="$1" max_bytes="${2:-4000}" scope_id
  scope_id="$(pc_ensure_scope)"
  _pc_curl POST /v1/context/prepare \
    "$(python3 -c 'import json,sys;print(json.dumps({"scope_id":sys.argv[1],"query":sys.argv[2],"max_bytes":int(sys.argv[3])},ensure_ascii=False))' \
       "${scope_id}" "${query}" "${max_bytes}")"
}

pc_list_entries() {
  local include_inactive="${1:-false}" scope_id
  scope_id="$(pc_ensure_scope)"
  _pc_curl POST /v1/memory/entries/list \
    "$(python3 -c 'import json,sys;print(json.dumps({"scope_id":sys.argv[1],"include_inactive":sys.argv[2].lower()=="true"},ensure_ascii=False))' \
       "${scope_id}" "${include_inactive}")"
}

pc_retire_entry() {
  local citation_json="$1" reason="${2:-}" scope_id
  scope_id="$(pc_ensure_scope)"
  _pc_curl POST /v1/memory/entries/retire \
    "$(python3 -c 'import json,sys;print(json.dumps({"scope_id":sys.argv[1],"citation":json.loads(sys.argv[2]),"reason":sys.argv[3] or None},ensure_ascii=False))' \
       "${scope_id}" "${citation_json}" "${reason}")"
}

# 从存档里解析当前状态（最新一条 npc_state 记忆里的数值）
# Dify 工作流里用同样的规则读状态，保证"对话里看到的数值"和"存进 PowerContext 的数值"一致。
pc_current_state() {
  pc_list_entries false | python3 -c '
import json, re, sys
d = json.load(sys.stdin)
entries = [e for e in d.get("entries", []) if e.get("kind") == "npc_state" and e.get("state") == "active"]
if not entries:
    print(json.dumps({"grudge": 0, "favor": 0, "turn": 0, "attitude": "中立"}, ensure_ascii=False))
    raise SystemExit(0)

def parse(text):
    grudge = int(m.group(1)) if (m := re.search(r"记仇=(-?\d+)", text)) else 0
    favor = int(m.group(1)) if (m := re.search(r"恩情=(-?\d+)", text)) else 0
    turn = int(m.group(1)) if (m := re.search(r"第\s*(\d+)\s*回合", text)) else 0
    return grudge, favor, turn

def attitude_of(grudge):
    if grudge >= 3: return "死敌"
    if grudge >= 1: return "记恨"
    if grudge <= -3: return "死忠"
    if grudge <= -1: return "亲近"
    return "中立"

parsed = [parse(e.get("text", "")) for e in entries]
# 取回合号最大的那条（列表不保证按时间排序，按回合号选才稳）
grudge, favor, turn = max(parsed, key=lambda x: x[2])
print(json.dumps({"grudge": grudge, "favor": favor, "turn": turn, "attitude": attitude_of(grudge)}, ensure_ascii=False))
'
}

# 把当前 scope 下所有 active 条目全部 retire（软重置，服务不用重启）
# 注意：每 retire 一条，Memory Artifact 的 revision 就会 +1，
# 所以必须每次都重新 list 一遍拿到最新 citation，否则会撞 409 revision_conflict。
pc_retire_all() {
  local count=0
  while :; do
    local list citation
    list="$(pc_list_entries false)"
    citation="$(printf '%s' "${list}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for e in d.get("entries", []):
    if e.get("state") == "active":
        print(json.dumps(e.get("citation"), ensure_ascii=False))
        break
')"
    [ -z "${citation}" ] && break
    pc_retire_entry "${citation}" "重置脚本：清空上一轮验证状态" >/dev/null
    count=$((count+1))
    [ "${count}" -gt 200 ] && die "retire 次数异常，停止重置"
  done
  if [ "${count}" -eq 0 ]; then
    log "当前没有 active 条目，无需重置"
  else
    log "已 retire ${count} 条记忆条目"
  fi
}

# 把 PowerContext 的 JSON 响应渲染成可读文本（走临时文件，避开 heredoc 抢 stdin 的坑）
pc_render() {
  local command="$1"; shift
  local payload="$1"; shift
  local tmp
  tmp="$(mktemp)"
  printf '%s' "${payload}" > "${tmp}"
  python3 "${PC_RENDER}" "${command}" "${tmp}" "$@"
  local rc=$?
  rm -f "${tmp}"
  return ${rc}
}

# --- 记忆写入的统一格式 ----------------------------------------------------
# 与 Dify 工作流里 HTTP Request 节点写入的文本格式保持一致，
# 这样评审在 PowerContext 里看到的条目和 NPC 在对话里说的能对得上。
pc_write_turn() {
  local action="$1" detail="$2" turn="$3" grudge="$4" favor="$5"
  local kind text reason
  case "${action}" in
    prank) kind="prank_grudge"; text="第${turn}回合，玩家对${NPC_NAME}使坏：${detail}。${NPC_NAME}记下了这笔账。"; reason="玩家恶作剧" ;;
    help)  kind="help_favor";   text="第${turn}回合，玩家帮了${NPC_NAME}：${detail}。${NPC_NAME}记着这份人情。"; reason="玩家帮忙" ;;
    *)     kind="encounter";    text="第${turn}回合，玩家与${NPC_NAME}：${detail}。"; reason="普通互动" ;;
  esac
  pc_remember "${kind}" "${text}" "${reason}" >/dev/null
  pc_remember "npc_state" "第${turn}回合后状态：记仇=${grudge}，恩情=${favor}，对${PLAYER_NAME}的态度由记仇值判定。" "NPC 状态快照" >/dev/null
  log "已写入第${turn}回合记忆（${kind}，记仇=${grudge} 恩情=${favor}）"
}
