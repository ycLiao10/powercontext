#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 把 DSL 导入 Dify，发布应用、申请 API Key，并把 Dify 导出的 DSL 回存到仓库
#
# 产出（写到 dify/.state/）：
#   app_id.txt        应用 ID
#   app_api_key.txt   应用 API Key（调用 /v1/chat-messages 用）
#
# 另外会把 Dify 自己导出的 DSL 覆盖回 dsl/grudge-npc-chatflow.yml，
# 保证交付的 DSL 与实际运行的版本完全一致。
#
# 用法：
#   bash scripts/import_dsl.sh [dsl文件]
# ---------------------------------------------------------------------------
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dify_api.sh"

REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DSL_FILE="${1:-${REPO_DIR}/dsl/grudge-npc-chatflow.yml}"
[ -f "${DSL_FILE}" ] || die "找不到 DSL 文件：${DSL_FILE}"

dify_ensure_login

# --- 1. 导入 ----------------------------------------------------------------
log "导入 DSL：${DSL_FILE}"
python3 - "$DSL_FILE" > /tmp/dify_import_payload.json <<'PY'
import json, sys
content = open(sys.argv[1], encoding='utf-8').read()
print(json.dumps({"mode": "yaml-content", "yaml_content": content}, ensure_ascii=False))
PY

result="$(console_curl POST /console/api/apps/imports "$(cat /tmp/dify_import_payload.json)")"
app_id="$(printf '%s' "${result}" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("app_id") or "")')"
status="$(printf '%s' "${result}" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("status") or "")')"
[ -n "${app_id}" ] || die "导入失败：${result}"
log "导入完成：app_id=${app_id} status=${status}"
printf '%s' "${app_id}" > "${STATE_DIR}/app_id.txt"

log "检查缺失依赖："
console_curl GET "/console/api/apps/imports/${app_id}/check-dependencies" 2>/dev/null | head -c 400 || true
echo

# --- 2. 发布 ----------------------------------------------------------------
log "发布应用……"
publish_result="$(console_curl POST "/console/api/apps/${app_id}/workflows/publish" '{"publish_model_config":true}' 2>/dev/null || true)"
echo "${publish_result}" | head -c 200; echo

# --- 3. 申请 API Key ---------------------------------------------------------
log "申请应用 API Key……"
key_result="$(console_curl POST "/console/api/apps/${app_id}/api-keys" '{}')"
api_key="$(printf '%s' "${key_result}" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(d.get("token") or d.get("api_key") or "")')"
if [ -n "${api_key}" ]; then
  printf '%s' "${api_key}" > "${STATE_DIR}/app_api_key.txt"
  log "API Key 已保存到 ${STATE_DIR}/app_api_key.txt"
else
  warn "自动申请 API Key 失败，请到控制台手动创建：${key_result:0:300}"
fi

# --- 4. 回存 Dify 导出的 DSL -------------------------------------------------
log "导出 Dify 的 DSL 并回存……"
exported="$(console_curl GET "/console/api/apps/${app_id}/export?format=yaml")"
printf '%s' "${exported}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
data = d.get("data")
if not data:
    raise SystemExit("导出结果里没有 data 字段")
open("/tmp/dify_exported_dsl.yml", "w", encoding="utf-8").write(data)
print("exported bytes:", len(data))
'
if [ -s /tmp/dify_exported_dsl.yml ]; then
  cp /tmp/dify_exported_dsl.yml "${REPO_DIR}/dsl/grudge-npc-chatflow.yml"
  log "已回存到 ${REPO_DIR}/dsl/grudge-npc-chatflow.yml"
fi

echo
log "完成。应用 ID=${app_id}"
log "体验入口：http://127.0.0.1/apps/${app_id}"
