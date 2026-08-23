#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNTIME_ROOT="${WEBGPT_RUNTIME_ROOT:-${HOME}/Downloads/webgpt}"
RUN_ID="opencode-micro-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="${RUNTIME_ROOT}/runs/opencode/${RUN_ID}"
TMP_DIR="${RUNTIME_ROOT}/tmp"
GATEWAY_PORT="${WEBGPT_GATEWAY_PORT:-18766}"
OPENCODE_BIN="${OPENCODE_BIN:-/home/light/.opencode/bin/opencode}"
GATEWAY_PID=""
ACTIVE_PID=""

if [[ ! -x "${OPENCODE_BIN}" ]]; then
  echo "OpenCode CLI not found at ${OPENCODE_BIN}" >&2
  exit 69
fi

mkdir -p "${RUN_DIR}/prompt_debug" "${TMP_DIR}"
chmod 700 "${RUNTIME_ROOT}" "${RUN_DIR}" "${RUN_DIR}/prompt_debug" "${TMP_DIR}" 2>/dev/null || true
export TMPDIR="${TMP_DIR}"
LOG_FILE="${RUN_DIR}/microgates.log"
GATEWAY_LOG="${RUN_DIR}/gateway.log"
TRACE_FILE="${RUN_DIR}/trace.jsonl"
STORE_FILE="${RUN_DIR}/conversations.json"
touch "${LOG_FILE}" "${GATEWAY_LOG}"
chmod 600 "${LOG_FILE}" "${GATEWAY_LOG}"

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

log() { printf '%s\n' "$*" | tee -a "${LOG_FILE}"; }
fail() { log "[FAIL] $*"; exit 1; }

log "=== OpenCode OC1-OC3 / account_mode=free_anonymous / run=${RUN_ID} ==="

# Refuse to disturb processes outside this run.
ensure_port_free() {
  python3 - "${GATEWAY_PORT}" <<'PYPORT'
import socket, sys
port = int(sys.argv[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.2)
    in_use = sock.connect_ex(("127.0.0.1", port)) == 0
raise SystemExit(1 if in_use else 0)
PYPORT
}
ensure_port_free || fail "Gateway port ${GATEWAY_PORT} is already in use; refusing to kill an unrelated process"


cd "${REPO_DIR}"
WEBGPT_SERVER_CLOSE_TIMEOUT="${WEBGPT_SERVER_CLOSE_TIMEOUT:-5}" uv run python -m gpt.debug api-server \
  --port "${GATEWAY_PORT}" \
  --headless --ephemeral --prewarm \
  --max-workers 1 \
  --generation-timeout 120 \
  --conversation-store "${STORE_FILE}" \
  --trace-file "${TRACE_FILE}" \
  --prompt-debug-dir "${RUN_DIR}/prompt_debug" \
  >>"${GATEWAY_LOG}" 2>&1 &
GATEWAY_PID=$!

if ! python3 "${SCRIPT_DIR}/wait-for-anonymous-ready.py" "${GATEWAY_PORT}" \
  --json-out "${RUN_DIR}/ready.json" >/dev/null; then
  fail "Gateway never reached ready anonymous state"
fi
chmod 600 "${RUN_DIR}/ready.json"
log "[PASS] gateway ready with auth_status=anonymous"

run_opencode() {
  local gate="$1" prompt="$2" workspace="$3"
  local gate_dir="${RUN_DIR}/${gate}"
  mkdir -p "${workspace}" "${gate_dir}/home" "${gate_dir}/config"
  local out="${gate_dir}/opencode.json" err="${gate_dir}/opencode.err"
  (
    cd "${workspace}"
    exec env \
      HOME="${gate_dir}/home" \
      XDG_CONFIG_HOME="${gate_dir}/config" \
      OPENAI_BASE_URL="http://127.0.0.1:${GATEWAY_PORT}/v1" \
      OPENAI_API_KEY="local-webgpt-placeholder" \
      timeout --kill-after=5 300 "${OPENCODE_BIN}" run \
        --model "gpt-4o" \
        "${prompt}"
  ) >"${out}" 2>"${err}" &
  ACTIVE_PID=$!
  if ! wait "${ACTIVE_PID}"; then
    ACTIVE_PID=""
    return 1
  fi
  ACTIVE_PID=""
}

new_workspace() {
  local gate="$1"
  local workspace="${RUN_DIR}/${gate}/workspace"
  rm -rf "${workspace}"
  mkdir -p "${workspace}"
  printf '%s' "${workspace}"
}

# OC1: Text only
W="$(new_workspace OC1)"
run_opencode OC1 "Reply exactly OPENCODE_C1_PASS. Do not call any tools." "${W}" || fail "OC1 failed"
grep -q 'OPENCODE_C1_PASS' "${RUN_DIR}/OC1/opencode.json" || fail "OC1 marker missing"
log "[PASS] OC1 text only"

# OC2: Read SPEC.md
W="$(new_workspace OC2)"
printf '# Gate OC2\nTOKEN=OPENCODE_OC2_TOKEN_7741\n' >"${W}/SPEC.md"
run_opencode OC2 "Read SPEC.md and print the TOKEN value." "${W}" || fail "OC2 failed"
grep -q 'OPENCODE_OC2_TOKEN_7741' "${RUN_DIR}/OC2/opencode.json" || fail "OC2 token missing"
log "[PASS] OC2 Read SPEC.md"

# OC3: Write math_module.py and verify
W="$(new_workspace OC3)"
run_opencode OC3 "Create math_module.py with add(a,b) returning a+b and verify." "${W}" || fail "OC3 failed"
python3 - "${W}" <<'PY' || fail "OC3 verification failed"
import sys
sys.path.insert(0, sys.argv[1])
import math_module
assert math_module.add(10, 20) == 30
PY
log "[PASS] OC3 Write and verify"

log "=== ALL OPENCODE MICRO-GATES PASSED OC1-OC3 ==="
log "Artifacts: ${RUN_DIR}"
