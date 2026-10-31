#!/usr/bin/env bash
#
# 启动本地 seekdb server（题目 4.1 允许随应用提供本地 server 的启动脚本与连接配置）。
#
# 用法：
#   bash server/start-seekdb.sh                 # 用默认路径启动
#   SEEKDB_BIN=/path/to/seekdb bash server/start-seekdb.sh
#   SEEKDB_BASE_DIR=./.seekdb SEEKDB_PORT=2881 bash server/start-seekdb.sh
#
# 环境变量：
#   SEEKDB_BIN       seekdb 可执行文件路径
#   SEEKDB_BASE_DIR  数据目录（默认 ./server/.seekdb）
#   SEEKDB_PORT      监听端口（默认 2881）
#   SEEKDB_LOG_LEVEL 日志级别（默认 WARN）
#
# 说明：脚本只负责「准备数据目录 + 拉起进程」，不做任何数据搬运或向量预计算。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SEEKDB_BIN="${SEEKDB_BIN:-/home/lyc/seekdb/build_release/src/observer/seekdb}"
SEEKDB_BASE_DIR="${SEEKDB_BASE_DIR:-$SCRIPT_DIR/.seekdb}"
SEEKDB_PORT="${SEEKDB_PORT:-2881}"
SEEKDB_LOG_LEVEL="${SEEKDB_LOG_LEVEL:-WARN}"

if [[ ! -x "$SEEKDB_BIN" ]]; then
  echo "错误：找不到 seekdb 可执行文件：$SEEKDB_BIN" >&2
  echo "请设置 SEEKDB_BIN 指向 seekdb 二进制，或先编译 seekdb。" >&2
  exit 1
fi

mkdir -p "$SEEKDB_BASE_DIR"/{etc,log,run,store}

echo "seekdb 可执行文件 : $SEEKDB_BIN"
echo "数据目录          : $SEEKDB_BASE_DIR"
echo "监听端口          : $SEEKDB_PORT"

# 已在监听则不重复拉起
if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${SEEKDB_PORT} "; then
  echo "端口 ${SEEKDB_PORT} 已有服务在监听，跳过启动。"
  exit 0
fi

echo "正在启动 seekdb ..."
"$SEEKDB_BIN" \
  --nodaemon \
  --base-dir="$SEEKDB_BASE_DIR" \
  --port="$SEEKDB_PORT" \
  --log-level="$SEEKDB_LOG_LEVEL" \
  >"$SEEKDB_BASE_DIR/log/stdout.log" 2>&1 &

SEEKDB_PID=$!
echo "seekdb 已启动，PID=$SEEKDB_PID"

# 等待端口就绪
for _ in $(seq 1 60); do
  if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${SEEKDB_PORT} "; then
    echo "seekdb 已就绪：127.0.0.1:${SEEKDB_PORT}"
    echo "连接参数：host=127.0.0.1 port=${SEEKDB_PORT} user=root password=(空)"
    exit 0
  fi
  sleep 1
done

echo "错误：seekdb 在 60 秒内没有就绪，请查看 $SEEKDB_BASE_DIR/log/stdout.log" >&2
exit 1
