#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 启动 PowerContext 服务（本地 SQLite 后端，数据落盘，跨重启可复用）
#
# 依赖：pip install "powercontext[cli,server]==1.2.0"
#       （官方文档：uv tool install "powercontext[cli,server] @ git+https://github.com/oceanbase/powercontext.git@master"）
#
# 用法：
#   ./start_powercontext.sh              # 前台启动
#   ./start_powercontext.sh --daemon     # 后台启动，日志写到 .state/powercontext.log
# ---------------------------------------------------------------------------
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pc_common.sh"

HOST="${POWERCONTEXT_HOST:-127.0.0.1}"
PORT="${POWERCONTEXT_PORT:-8000}"
STATE_HOME="${POWERCONTEXT_HOME:-${STATE_DIR}/powercontext-home}"
LOG_FILE="${STATE_DIR}/powercontext.log"

mkdir -p "${STATE_HOME}" "${STATE_DIR}"

if ! command -v powercontext >/dev/null 2>&1; then
  die "找不到 powercontext 命令。请先安装：
  python3 -m venv .venv && . .venv/bin/activate && pip install \"powercontext[cli,server]==1.2.0\""
fi

export POWERCONTEXT_SERVER_DATABASE_URL="sqlite+aiosqlite:///${STATE_HOME}/powercontext.db"
export POWERCONTEXT_SERVER_HTTP_HOST="${HOST}"
export POWERCONTEXT_SERVER_HTTP_PORT="${PORT}"
export POWERCONTEXT_HOME="${STATE_HOME}"

log "数据目录：${STATE_HOME}"
log "监听：${HOST}:${PORT}"

if [ "${1:-}" = "--daemon" ]; then
  nohup powercontext server run --host "${HOST}" --port "${PORT}" --no-env-file > "${LOG_FILE}" 2>&1 &
  log "后台 PID=$!，日志：${LOG_FILE}"
  for _ in $(seq 1 40); do
    sleep 2
    if [ "$(pc_health || true)" = "200" ]; then
      log "服务就绪"
      exit 0
    fi
  done
  die "服务未在 80 秒内就绪，请查看 ${LOG_FILE}"
else
  exec powercontext server run --host "${HOST}" --port "${PORT}" --no-env-file
fi
