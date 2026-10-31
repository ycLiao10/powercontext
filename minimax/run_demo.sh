#!/usr/bin/env bash
# 一键复现：把赛题六项必做项里「可机器复现」的部分全部跑一遍。
#
#   1. 建库建表 + 灌数据（seekdb）
#   2. 按任务检索并输出上下文（seekdb 全文检索）
#   3. 写入任务状态（PowerContext）
#   4. **重启 PowerContext 进程**，再读回状态（状态在磁盘 SQLite 里）
#   5. 跑目标测试 + 回归测试
#
# 每一步都打印退出码；全部为 0 才算通过。
#
# 用法（必须在 minimax/ 目录下）：
#   bash run_demo.sh
#
# 前置条件（见 README 第 3 节）：seekdb 在 127.0.0.1:2881 上跑着。
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
PORT="${POWERCONTEXT_PORT:-8000}"
STATE_DIR="${POWERCONTEXT_STATE_DIR:-.powercontext}"
SERVER_LOG="${STATE_DIR}/server.log"

# 客户端地址必须和端口一致，否则 status/save 会连到别的实例上
export POWERCONTEXT_SERVER_URL="${POWERCONTEXT_SERVER_URL:-http://127.0.0.1:$PORT}"

FAILED=0
step() { echo; echo "=================================================="; echo ">>> $*"; echo "=================================================="; }
record() {
  local name="$1" rc="$2"
  if [ "$rc" -eq 0 ]; then echo "PASS  $name (exit=$rc)"; else echo "FAIL  $name (exit=$rc)"; FAILED=1; fi
}

mkdir -p "$STATE_DIR"

stop_server() {
  if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
  fi
  SERVER_PID=""
}
trap stop_server EXIT

start_server() {
  stop_server
  "$PY" -m ctx serve --host 127.0.0.1 --port "$PORT" --state-dir "$STATE_DIR" >>"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 60); do
    if "$PY" - <<'PYCHK' >/dev/null 2>&1
import os, urllib.request
url = os.environ.get("POWERCONTEXT_SERVER_URL", "http://127.0.0.1:8000") + "/v1/capabilities"
urllib.request.urlopen(url, timeout=2).read()
PYCHK
    then
      echo "PowerContext 就绪：http://127.0.0.1:$PORT (pid=$SERVER_PID)"
      return 0
    fi
    sleep 1
  done
  echo "PowerContext 启动失败，日志见 $SERVER_LOG" >&2
  return 1
}

echo "### 环境"
echo "python : $("$PY" --version 2>&1)"
echo "workdir: $(pwd)"

step "1/6 建库建表 + 灌数据（seekdb）"
"$PY" init_db.py --reset
record "init_db.py" $?

step "2/6 导入工程资料（seekdb）"
"$PY" -m ctx index --root sample_project
record "ctx index" $?

step "3/6 启动 PowerContext"
start_server
record "ctx serve" $?

step "4/6 写入任务状态（PowerContext）"
"$PY" -m ctx save --kind task \
  --text "让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个"
record "ctx save (task)" $?
"$PY" -m ctx save --kind completed --text "已用 ctx index 把 sample_project 的规格/代码/测试导入 seekdb"
record "ctx save (completed)" $?
"$PY" -m ctx save --kind todo --text "跑目标测试确认 8 条全通过"
record "ctx save (todo)" $?
"$PY" -m ctx save --kind verification \
  --text "改后验证：python -m pytest sample_project/tests/test_pricing.py -v -> 退出码 0，8 passed"
record "ctx save (verification)" $?

step "5/6 按任务检索并输出上下文（seekdb 全文检索 -> ctx.json，含任务状态）"
"$PY" -m ctx build \
  --task "让 total 按规格实现满减与会员折扣不叠加，取优惠金额更大的那个" \
  --out ctx.json
record "ctx build" $?

step "6/6 重启 PowerContext 进程 -> 读回状态 -> 跑测试"
echo
echo "--- 写入后（同一进程内）读回 ---"
"$PY" -m ctx status
record "ctx status (before restart)" $?

echo
echo "--- 重启 PowerContext 进程 ---"
stop_server
echo "进程已停止，等待 2 秒"
sleep 2
start_server
record "ctx serve (restart)" $?

echo
echo "--- 重启后读回（状态必须仍在） ---"
"$PY" -m ctx status
record "ctx status (after restart)" $?

echo
echo "--- 目标测试 + 回归测试 ---"
"$PY" -m pytest sample_project/tests/test_pricing.py -v
record "pytest" $?

echo
echo "=================================================="
if [ "$FAILED" -eq 0 ]; then
  echo "全部通过 ✅"
else
  echo "存在失败步骤 ❌"
fi
echo "=================================================="
exit "$FAILED"
