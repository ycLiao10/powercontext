#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 准备 Dify 的 .env（幂等）
#
# 做三件事：
#   1. 用官方 .env.example 复制出 .env（只在第一次时生成随机密钥与密码）
#   2. 打开本作品需要的几个开关：
#        - SSRF_PROXY_ALLOW_PRIVATE_IPS：让 Dify 的 HTTP Request 节点能访问内网里的 PowerContext
#        - VECTOR_STORE=qdrant：默认的 weaviate 镜像在国内拉取不稳定，换成 qdrant
#        - DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN：agent_backend 与 local_sandbox 之间的共享 token
#   3. 把本作品的增量文件复制到 Dify 的 docker/ 目录
#
# 注意：DB_PASSWORD / REDIS_PASSWORD / SECRET_KEY 只在**首次生成 .env 时**写入。
# 数据库密码改了之后，已经初始化过的 Postgres 数据卷里的密码不会跟着变，
# 需要 `docker compose down -v` 重建数据卷才能生效（此时工作区数据会清空）。
#
# 用法：
#   DIFY_DIR=/path/to/dify bash scripts/setup_dify_env.sh
# ---------------------------------------------------------------------------
set -euo pipefail

DIFY_DIR="${DIFY_DIR:-${HOME}/dify-1.18.0}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -f "${DIFY_DIR}/docker/docker-compose.yaml" ]; then
  echo "找不到 ${DIFY_DIR}/docker/docker-compose.yaml" >&2
  echo "请先下载 Dify 1.18.0：https://github.com/langgenius/dify/archive/refs/tags/1.18.0.tar.gz" >&2
  exit 1
fi

cd "${DIFY_DIR}/docker"

FRESH=0
if [ ! -f .env ]; then
  cp .env.example .env
  FRESH=1
  echo "[setup] 已从 .env.example 生成 .env"
fi

SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(42))")
DBPASS=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
REDPASS=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")

python3 - "$SECRET" "$DBPASS" "$REDPASS" "$FRESH" <<'PY'
import os
import re
import secrets
import sys
import pathlib

secret, dbpass, redpass, fresh = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
p = pathlib.Path('.env')
s = p.read_text(encoding='utf-8')


def setkv(text, key, value):
    pat = re.compile(r'(?m)^%s=.*$' % re.escape(key))
    if pat.search(text):
        return pat.sub(f'{key}={value}', text)
    return text + f'\n{key}={value}\n'


if fresh:
    # 只有第一次生成 .env 时才写密码：数据库一旦初始化，密码就不能再改
    s = setkv(s, 'SECRET_KEY', secret)
    s = setkv(s, 'DB_PASSWORD', dbpass)
    s = setkv(s, 'REDIS_PASSWORD', redpass)

# 关键坑：官方 .env.example 里 CELERY_BROKER_URL 把密码写死成 difyai123456。
# 一旦把 REDIS_PASSWORD 换成随机值，就必须同步改 broker URL，
# 否则 celery worker 连不上 Redis（报 invalid username-password pair），
# 表现是：Service API 的 blocking 调用正常，但 WebApp / 流式回复 / 控制台调试全部卡住不动。
m = re.search(r'(?m)^REDIS_PASSWORD=(.*)$', s)
if m and m.group(1).strip():
    s = setkv(s, 'CELERY_BROKER_URL', 'redis://:{}@redis:6379/1'.format(m.group(1).strip()))

s = setkv(s, 'EXPOSE_NGINX_PORT', '80')
s = setkv(s, 'VECTOR_STORE', 'qdrant')
if 'QDRANT_URL' not in s:
    s += '\nQDRANT_URL=http://qdrant:6333\n'
# 关键：放行内网段，Dify 的 HTTP Request 节点才能访问同一网络里的 PowerContext
s = setkv(s, 'SSRF_PROXY_ALLOW_PRIVATE_IPS', '172.16.0.0/12,10.0.0.0/8,192.168.0.0/16')
s = setkv(s, 'SSRF_PROXY_ALLOW_PRIVATE_DOMAINS', 'powercontext,host.docker.internal')
# PyPI 源：默认官方源；国内网络不通时改成镜像源（见 README「网络注意事项」）
s = setkv(s, 'PYPI_INDEX', os.environ.get('PYPI_INDEX', 'https://pypi.org/simple'))
s = setkv(s, 'PYPI_TRUSTED_HOST', os.environ.get('PYPI_TRUSTED_HOST', 'pypi.org'))
# agent_backend 走 local 运行时，必须和 local_sandbox 对上同一个 token
s = setkv(s, 'DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN', secrets.token_urlsafe(24))
p.write_text(s, encoding='utf-8')
print('[setup] .env 已配置（密码与密钥随机生成，未写入仓库）')
PY

# 复制本作品给 Dify 栈用的增量文件
cp "${REPO_DIR}/deploy/Dockerfile.powercontext" ./Dockerfile.powercontext
cp "${REPO_DIR}/deploy/docker-compose.override.yaml" ./docker-compose.override.yaml
echo "[setup] 已复制 Dockerfile.powercontext 与 docker-compose.override.yaml 到 ${DIFY_DIR}/docker"

echo "[setup] 关键配置："
grep -E "^(VECTOR_STORE|SSRF_PROXY_ALLOW_PRIVATE_IPS|EXPOSE_NGINX_PORT|PYPI_INDEX)=" .env
