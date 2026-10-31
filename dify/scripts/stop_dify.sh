#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 停止 Dify + PowerContext 栈（保留数据卷）
#
# 用法：
#   DIFY_DIR=/path/to/dify bash scripts/stop_dify.sh
#   DIFY_DIR=/path/to/dify bash scripts/stop_dify.sh --purge   # 连数据卷一起删
# ---------------------------------------------------------------------------
set -euo pipefail

DIFY_DIR="${DIFY_DIR:-${HOME}/dify-1.18.0}"
cd "${DIFY_DIR}/docker"

PROFILES="--profile postgresql --profile qdrant"
if [ "${1:-}" = "--purge" ]; then
  echo "[dify] 停止并删除数据卷……"
  docker compose ${PROFILES} down -v
else
  echo "[dify] 停止服务（保留数据卷）……"
  docker compose ${PROFILES} down
fi
