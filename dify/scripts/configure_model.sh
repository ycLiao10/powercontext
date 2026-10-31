#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 配置 Dify 使用的模型 provider（OpenAI 兼容端点）
#
# 做三件事：
#   1. 从插件市场查询并安装 openai_api_compatible 模型插件
#   2. 为模型创建凭据（api_key / endpoint_url / 模式 / 上下文长度等）
#   3. 校验凭据可用
#
# 凭据从环境变量读取，不写进仓库：
#   export MODEL_API_KEY='sk-xxx'
#   export MODEL_BASE_URL='https://api.minimaxi.com/v1'
#   export MODEL_NAME='MiniMax-M3.1-Flash'
#
# 用法：
#   bash scripts/configure_model.sh
# ---------------------------------------------------------------------------
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dify_api.sh"

MARKETPLACE_API_URL="${MARKETPLACE_API_URL:-https://marketplace.dify.ai}"
# provider id（插件 id 是 langgenius/openai_api_compatible，provider id 是 openai_api_compatible）
PROVIDER_ID="${PROVIDER_ID:-openai_api_compatible}"
PLUGIN_ID="${PLUGIN_ID:-}"

dify_ensure_login

# --- 1. 安装模型插件 --------------------------------------------------------
if [ -z "${PLUGIN_ID}" ]; then
  log "从插件市场查找 openai_api_compatible……"
  # Dify 的 marketplace 搜索接口是 POST + JSON（见 api/core/helper/marketplace.py）
  PLUGIN_ID="$(curl -sS --max-time 40 -X POST "${MARKETPLACE_API_URL}/api/v1/plugins/search/advanced" \
    -H "Content-Type: application/json" -H "X-Dify-Version: 1.18.0" \
    -d '{"page":1,"page_size":30,"query":"openai_api_compatible","category":"model","sort_by":"install_count","sort_order":"DESC","tags":[],"exclude":[],"type":"plugin"}' \
    | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
plugins = (d.get("data") or {}).get("plugins") or []
for p in plugins:
    if (p.get("plugin_id") or "") == "langgenius/openai_api_compatible":
        print(p.get("latest_package_identifier") or "")
        break
')"
fi
[ -n "${PLUGIN_ID}" ] || die "没找到 openai_api_compatible 插件。可手动在控制台安装后再运行本脚本。"
log "插件标识：${PLUGIN_ID}"

log "安装插件……"
console_curl POST /console/api/workspaces/current/plugin/install/marketplace \
  "$(python3 -c 'import json,sys;print(json.dumps({"plugin_unique_identifiers":[sys.argv[1]]}))' "${PLUGIN_ID}")" | head -c 200
echo
sleep 8

# --- 2. 为模型创建凭据 -----------------------------------------------------
[ -n "${MODEL_API_KEY}" ] || die "请先设置环境变量 MODEL_API_KEY（OpenAI 兼容端点的 API Key）"

log "配置模型凭据：${MODEL_NAME} @ ${MODEL_BASE_URL}"
cred_result="$(console_curl POST "/console/api/workspaces/current/model-providers/${PROVIDER_ID}/models/credentials" \
  "$(python3 -c '
import json, sys
model, base_url, api_key = sys.argv[1:4]
creds = {
    "api_key": api_key,
    "endpoint_url": base_url,
    "mode": "chat",
    "context_size": "32768",
    "max_chunks": "1",
    "max_tokens_to_sample": "4096",
    "api_type": "chat_completions",
    "compatibility_mode": "strict",
    "stream_mode_auth": "not_use",
}
print(json.dumps({
    "model": model,
    "model_type": "llm",
    "name": "dify-grudge-npc",
    "credentials": creds,
}, ensure_ascii=False))
' "${MODEL_NAME}" "${MODEL_BASE_URL}" "${MODEL_API_KEY}")")"
echo "${cred_result}" | head -c 300; echo

# --- 3. 校验 ----------------------------------------------------------------
log "校验凭据……"
validate_result="$(console_curl POST "/console/api/workspaces/current/model-providers/${PROVIDER_ID}/models/credentials/validate" \
  "$(python3 -c '
import json, sys
model, base_url, api_key = sys.argv[1:4]
creds = {"api_key": api_key, "endpoint_url": base_url, "mode": "chat", "context_size": "32768", "max_chunks": "1"}
print(json.dumps({"model": model, "model_type": "llm", "credentials": creds}, ensure_ascii=False))
' "${MODEL_NAME}" "${MODEL_BASE_URL}" "${MODEL_API_KEY}")")"
echo "${validate_result}" | head -c 500; echo

log "完成。模型：${MODEL_NAME}（${MODEL_BASE_URL}）"
log "在 DSL 里对应的 provider 字段应为：langgenius/openai_api_compatible/openai_api_compatible"
