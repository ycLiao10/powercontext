#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Dify 控制台 / 应用 API 的公共封装
#
# 环境变量：
#   DIFY_CONSOLE_URL  控制台地址，默认 http://127.0.0.1
#   DIFY_EMAIL        管理员邮箱
#   DIFY_PASSWORD     管理员密码
#   MODEL_PROVIDER    模型 provider id
#   MODEL_NAME        模型名
#   MODEL_BASE_URL    OpenAI 兼容端点
#   MODEL_API_KEY     模型 API Key（从环境变量读取，不写进仓库）
#
# 注意：Dify 1.18 的 /console/api/login 把 access_token / refresh_token / csrf_token
# 放在 **Cookie** 里（不是响应体），所以这里用 cookie jar 来维持会话。
# ---------------------------------------------------------------------------
set -euo pipefail

DIFY_CONSOLE_URL="${DIFY_CONSOLE_URL:-http://127.0.0.1}"
# 管理员凭据只从环境变量读，仓库里不留任何默认值：
# 在 .env 里写好 DIFY_EMAIL / DIFY_PASSWORD，再 `set -a && source .env && set +a`
DIFY_EMAIL="${DIFY_EMAIL:-}"
DIFY_PASSWORD="${DIFY_PASSWORD:-}"
MODEL_PROVIDER="${MODEL_PROVIDER:-langgenius/openai_api_compatible/openai_api_compatible}"
MODEL_NAME="${MODEL_NAME:-MiniMax-M3.1-Flash}"
MODEL_BASE_URL="${MODEL_BASE_URL:-https://api.minimaxi.com/v1}"
MODEL_API_KEY="${MODEL_API_KEY:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${STATE_DIR:-${SCRIPT_DIR}/../.state}"
COOKIE_FILE="${STATE_DIR}/dify_cookies.txt"
mkdir -p "${STATE_DIR}"

log()  { printf '\033[36m[dify]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[dify]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[dify] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Dify 前端对 password 字段做 Base64 编码后再提交（见 web/utils/encryption.ts）
b64() { printf '%s' "$1" | base64 -w0; }

# 没有凭据就直接失败，别用任何内置默认值兜底
dify_require_credentials() {
  [ -n "${DIFY_EMAIL}" ] && [ -n "${DIFY_PASSWORD}" ] && return 0
  die "缺少 Dify 管理员凭据。请在 .env 里设置 DIFY_EMAIL / DIFY_PASSWORD 后
   （set -a && source .env && set +a）再运行本脚本。仓库里不内置任何默认密码。"
}

csrf_token() {
  [ -f "${COOKIE_FILE}" ] || return 0
  awk '$6 == "csrf_token" {print $7}' "${COOKIE_FILE}" | tail -n1
}

# 控制台 API：带 Cookie 会话（必要时带 CSRF 头）
console_curl() {
  local method="$1" path="$2" payload="${3:-}"
  local -a args=(-sS --max-time 120 -X "${method}" "${DIFY_CONSOLE_URL}${path}"
                 -H "Content-Type: application/json"
                 -c "${COOKIE_FILE}" -b "${COOKIE_FILE}" -w $'\n%{http_code}')
  local csrf
  csrf="$(csrf_token)"
  [ -n "${csrf}" ] && args+=(-H "X-CSRF-Token: ${csrf}")
  if [ -n "${payload}" ]; then args+=(--data "${payload}"); fi
  local raw code
  raw="$(curl "${args[@]}")" || return 1
  code="$(printf '%s' "${raw}" | tail -n1)"
  printf '%s' "${raw}" | sed '$d'
  if [ "${code}" -ge 400 ]; then
    die "${method} ${path} -> HTTP ${code}: $(printf '%s' "${raw}" | sed '$d' | head -c 600)"
  fi
  return 0
}

# 创建管理员（幂等：已经初始化过就直接登录）
dify_setup_admin() {
  dify_require_credentials
  local body
  body="$(curl -sS --max-time 30 -X POST "${DIFY_CONSOLE_URL}/console/api/setup" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c 'import json,sys,base64;print(json.dumps({"email":sys.argv[1],"name":sys.argv[2],"password":base64.b64encode(sys.argv[3].encode()).decode()}))' \
        "${DIFY_EMAIL}" "赛题裁判" "${DIFY_PASSWORD}")" || true)"
  if printf '%s' "${body}" | grep -q '"result":"success"'; then
    log "管理员已创建：${DIFY_EMAIL}"
  else
    log "控制台已初始化过（${body:0:120}），直接登录"
  fi
}

# 登录（token 走 Cookie）
dify_login() {
  dify_require_credentials
  local body
  body="$(console_curl POST /console/api/login \
    "$(python3 -c 'import json,sys,base64;print(json.dumps({"email":sys.argv[1],"password":base64.b64encode(sys.argv[2].encode()).decode(),"language":"zh-Hans","remember_me":True}))' \
       "${DIFY_EMAIL}" "${DIFY_PASSWORD}")")"
  if grep -q "access_token" "${COOKIE_FILE}" 2>/dev/null; then
    log "已登录（会话保存在 ${COOKIE_FILE}）"
  else
    die "登录失败：${body:0:300}"
  fi
}

dify_ensure_login() {
  if ! grep -q "access_token" "${COOKIE_FILE}" 2>/dev/null; then
    dify_setup_admin
    dify_login
  fi
}

# 应用 API（用应用自己的 API Key，不走 Cookie）
app_curl() {
  local method="$1" path="$2" payload="${3:-}" key="${4:-}"
  local -a args=(-sS --max-time 300 -X "${method}" "${DIFY_CONSOLE_URL}${path}"
                 -H "Content-Type: application/json" -w $'\n%{http_code}')
  if [ -n "${payload}" ]; then args+=(--data "${payload}"); fi
  if [ -n "${key}" ]; then args+=(-H "Authorization: Bearer ${key}"); fi
  local raw code
  raw="$(curl "${args[@]}")" || return 1
  code="$(printf '%s' "${raw}" | tail -n1)"
  printf '%s' "${raw}" | sed '$d'
  if [ "${code}" -ge 400 ]; then
    die "${method} ${path} -> HTTP ${code}: $(printf '%s' "${raw}" | sed '$d' | head -c 800)"
  fi
  return 0
}
