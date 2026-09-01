#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RUNTIME_ROOT="${WEBGPT_RUNTIME_ROOT:-${HOME}/.local/share/webgpt}"
RUN_ID="opencode-live-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="${RUNTIME_ROOT}/runs/opencode/${RUN_ID}"
TMP_DIR="${RUNTIME_ROOT}/tmp"
PORT="${WEBGPT_GATEWAY_PORT:-18767}"
OPENCODE_BIN="${OPENCODE_BIN:-$(command -v opencode || true)}"
GATEWAY_PID=""
ACTIVE_PID=""

[[ -n "${OPENCODE_BIN}" ]] || { echo "opencode not found" >&2; exit 69; }
mkdir -p "${RUN_DIR}/prompt_debug" "${TMP_DIR}"
chmod 700 "${RUNTIME_ROOT}" "${RUN_DIR}" "${RUN_DIR}/prompt_debug" "${TMP_DIR}" 2>/dev/null || true
export TMPDIR="${TMP_DIR}"
LOG="${RUN_DIR}/run.log"
GATEWAY_LOG="${RUN_DIR}/gateway.log"
TRACE="${RUN_DIR}/trace.jsonl"
STORE="${RUN_DIR}/conversations.json"
touch "${LOG}" "${GATEWAY_LOG}"; chmod 600 "${LOG}" "${GATEWAY_LOG}"

cleanup() {
  if [[ -n "${ACTIVE_PID}" ]] && kill -0 "${ACTIVE_PID}" 2>/dev/null; then
    kill "${ACTIVE_PID}" 2>/dev/null || true
    wait "${ACTIVE_PID}" 2>/dev/null || true
  fi
  if [[ -n "${GATEWAY_PID}" ]] && kill -0 "${GATEWAY_PID}" 2>/dev/null; then
    kill "${GATEWAY_PID}" 2>/dev/null || true
    wait "${GATEWAY_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM
log(){ printf '%s\n' "$*" | tee -a "${LOG}"; }
fail(){ log "[FAIL] $*"; exit 1; }

cd "${REPO_DIR}"
WEBGPT_SERVER_CLOSE_TIMEOUT="${WEBGPT_SERVER_CLOSE_TIMEOUT:-5}" python -m gpt.debug api-server \
  --port "${PORT}" --headless --ephemeral --prewarm \
  --max-workers 1 --generation-timeout 45 \
  --conversation-store "${STORE}" --trace-file "${TRACE}" \
  --prompt-debug-dir "${RUN_DIR}/prompt_debug" \
  >>"${GATEWAY_LOG}" 2>&1 &
GATEWAY_PID=$!
printf '{"run_id":"%s","account_mode":"free_anonymous","gateway_pid":%s,"client":"opencode"}\n' \
  "${RUN_ID}" "${GATEWAY_PID}" >"${RUN_DIR}/run.json"
chmod 600 "${RUN_DIR}/run.json"

if ! python "${SCRIPT_DIR}/wait-for-anonymous-ready.py" "${PORT}" \
  --json-out "${RUN_DIR}/ready.json" >/dev/null; then
  fail "gateway never became free-anonymous ready"
fi
chmod 600 "${RUN_DIR}/ready.json"
log "[PASS] gateway ready / anonymous"

run_gate() {
  local gate="$1" prompt="$2"
  local gate_dir="${RUN_DIR}/${gate}"
  local workspace="${gate_dir}/workspace"
  local home="${gate_dir}/home" config="${gate_dir}/config"
  mkdir -p "${workspace}" "${home}" "${config}/opencode"
  cat >"${config}/opencode/opencode.jsonc" <<JSON
{
  "\$schema": "https://opencode.ai/config.json",
  "provider": {
    "webgpt": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "WebGPT Free Anonymous",
      "options": {"baseURL": "http://127.0.0.1:${PORT}/v1", "apiKey": "local-placeholder"},
      "models": {"chatgpt-web": {"name": "ChatGPT Web Gateway"}}
    }
  },
  "model": "webgpt/chatgpt-web",
  "small_model": "webgpt/chatgpt-web"
}
JSON
  (
    cd "${workspace}"
    exec env HOME="${home}" XDG_CONFIG_HOME="${config}" OPENCODE_DISABLE_AUTOUPDATE=1 \
      timeout --kill-after=5 360 "${OPENCODE_BIN}" run --pure \
        --model webgpt/chatgpt-web --format json "${prompt}"
  ) >"${gate_dir}/client.out" 2>"${gate_dir}/client.err" &
  ACTIVE_PID=$!
  if ! wait "${ACTIVE_PID}"; then ACTIVE_PID=""; return 1; fi
  ACTIVE_PID=""
}

run_gate OC1 "Reply exactly OC_FREE_OK and do not use tools." || fail "OC1 client exited non-zero"
grep -q 'OC_FREE_OK' "${RUN_DIR}/OC1/client.out" || fail "OC1 marker missing"
log "[PASS] OC1 text"

run_gate OC2 "Use the bash tool to run pwd and return the exact working directory." || fail "OC2 client exited non-zero"
grep -Fq "${RUN_DIR}/OC2/workspace" "${RUN_DIR}/OC2/client.out" || fail "OC2 pwd mismatch"
log "[PASS] OC2 bash pwd"

run_gate OC3 "Create tiny.py containing a main function that returns 7, then use bash to run python tiny.py or compile it and verify it is valid." || fail "OC3 client exited non-zero"
python -m py_compile "${RUN_DIR}/OC3/workspace/tiny.py" || fail "OC3 tiny.py missing/invalid"
python - "${RUN_DIR}/OC3/workspace" <<'PY' || fail "OC3 behavior mismatch"
import sys
sys.path.insert(0,sys.argv[1])
import tiny
assert tiny.main()==7
PY
log "[PASS] OC3 create/run Python"

if grep -Eqi 'anonymous_session_unavailable|web_ui_changed|browser_disconnected|internal_error|conversation_conflict|malformed_model_tool_call' "${GATEWAY_LOG}"; then
  fail "unexpected gateway-layer error present in log"
fi
log "=== OPENCODE LIVE GATES PASS ==="
log "Artifacts: ${RUN_DIR}"
