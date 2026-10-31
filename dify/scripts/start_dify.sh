#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 启动 Dify 1.18.0 + PowerContext（同一 Docker 网络）
#
# 启动的服务（都是本作品真正用到的最小集合）：
#   nginx / web / api / worker / worker_beat / plugin_daemon / sandbox /
#   ssrf_proxy / db_postgres / redis / qdrant / agent_backend / powercontext
#
# 用法：
#   DIFY_DIR=/path/to/dify bash scripts/start_dify.sh
# ---------------------------------------------------------------------------
set -euo pipefail

DIFY_DIR="${DIFY_DIR:-${HOME}/dify-1.18.0}"
DOCKER_DIR="${DIFY_DIR}/docker"
cd "${DOCKER_DIR}"

PROFILES="--profile postgresql --profile qdrant"
SERVICES="init_permissions db_postgres redis qdrant plugin_daemon agent_backend local_sandbox agent_ssrf_proxy api worker worker_beat sandbox ssrf_proxy web nginx powercontext"

echo "[dify] 启动中……"
docker compose ${PROFILES} up -d ${SERVICES}

echo
echo "[dify] 等待服务就绪……"
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1/health || true)
  if [ "${code}" = "200" ] || [ "${code}" = "302" ]; then
    echo "[dify] 控制台已就绪（HTTP ${code}），耗时约 $((i * 5)) 秒"
    break
  fi
  sleep 5
done

echo
echo "[dify] 容器状态："
docker compose ${PROFILES} ps --format "table {{.Service}}\t{{.Status}}" | head -25

echo
echo "[dify] PowerContext 自检："
curl -s --max-time 10 http://127.0.0.1:8000/health/live && echo
echo "[dify] 控制台地址：http://127.0.0.1/ （首次访问需要创建管理员账号）"
