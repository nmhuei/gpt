#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RUNTIME_ROOT="${WEBGPT_RUNTIME_ROOT:-${HOME}/.local/share/webgpt}"
RUN_ID="claude-micro-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="${RUNTIME_ROOT}/runs/claude/${RUN_ID}"
TMP_DIR="${RUNTIME_ROOT}/tmp"
GATEWAY_PORT="${WEBGPT_GATEWAY_PORT:-18765}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
GATEWAY_PID=""
ACTIVE_CLAUDE_PID=""

if [[ -z "${CLAUDE_BIN}" ]]; then
  echo "Claude Code CLI not found" >&2
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
  if [[ -n "${ACTIVE_CLAUDE_PID}" ]] && kill -0 "${ACTIVE_CLAUDE_PID}" 2>/dev/null; then
    kill "${ACTIVE_CLAUDE_PID}" 2>/dev/null || true
    wait "${ACTIVE_CLAUDE_PID}" 2>/dev/null || true
  fi
  if [[ -n "${GATEWAY_PID}" ]] && kill -0 "${GATEWAY_PID}" 2>/dev/null; then
    kill "${GATEWAY_PID}" 2>/dev/null || true
    wait "${GATEWAY_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

log() { printf '%s\n' "$*" | tee -a "${LOG_FILE}"; }
fail() { log "[FAIL] $*"; exit 1; }

log "=== Claude Code C1-C8 / account_mode=free_anonymous / run=${RUN_ID} ==="

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
printf '{"run_id":"%s","account_mode":"free_anonymous","gateway_pid":%s,"claude_bin":"%s"}\n' \
  "${RUN_ID}" "${GATEWAY_PID}" "${CLAUDE_BIN}" >"${RUN_DIR}/run.json"
chmod 600 "${RUN_DIR}/run.json"

if ! python3 "${SCRIPT_DIR}/wait-for-anonymous-ready.py" "${GATEWAY_PORT}" \
  --json-out "${RUN_DIR}/ready.json" >/dev/null; then
  fail "Gateway never reached ready anonymous state"
fi
chmod 600 "${RUN_DIR}/ready.json"
log "[PASS] gateway ready with auth_status=anonymous"

run_claude() {
  local gate="$1" prompt="$2" workspace="$3"
  local gate_dir="${RUN_DIR}/${gate}"
  mkdir -p "${workspace}" "${gate_dir}/home" "${gate_dir}/config" "${gate_dir}/claude-config"
  local out="${gate_dir}/claude.json" err="${gate_dir}/claude.err"
  (
    cd "${workspace}"
    exec env \
      HOME="${gate_dir}/home" \
      XDG_CONFIG_HOME="${gate_dir}/config" \
      CLAUDE_CONFIG_DIR="${gate_dir}/claude-config" \
      ANTHROPIC_BASE_URL="http://127.0.0.1:${GATEWAY_PORT}" \
      ANTHROPIC_AUTH_TOKEN="local-webgpt-placeholder" \
      ANTHROPIC_API_KEY="local-webgpt-placeholder" \
      ANTHROPIC_MODEL="claude-3-5-sonnet-20241022" \
      ANTHROPIC_SMALL_FAST_MODEL="claude-3-5-sonnet-20241022" \
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
      DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 DISABLE_BUG_COMMAND=1 \
      timeout --kill-after=5 360 "${CLAUDE_BIN}" --bare --print --no-session-persistence \
        --dangerously-skip-permissions \
        --output-format json "${prompt}"
  ) >"${out}" 2>"${err}" &
  ACTIVE_CLAUDE_PID=$!
  if ! wait "${ACTIVE_CLAUDE_PID}"; then
    ACTIVE_CLAUDE_PID=""
    return 1
  fi
  ACTIVE_CLAUDE_PID=""
}

new_workspace() {
  local gate="$1"
  local workspace="${RUN_DIR}/${gate}/workspace"
  [[ "${workspace}" == "${RUN_DIR}"/* ]] || fail "workspace escaped run root"
  rm -rf "${workspace}"
  mkdir -p "${workspace}"
  printf '%s' "${workspace}"
}

W="$(new_workspace C1)"
run_claude C1 "Reply exactly CLAUDE_C1_PASS. Do not call tools." "${W}" || fail "C1 Claude exited non-zero"
grep -q 'CLAUDE_C1_PASS' "${RUN_DIR}/C1/claude.json" || fail "C1 marker missing"
log "[PASS] C1 text only"

W="$(new_workspace C2)"
printf '# Gate C2\nTOKEN=CLAUDE_C2_TOKEN_9182\n' >"${W}/SPEC.md"
run_claude C2 "Use Read to read SPEC.md and return the TOKEN value." "${W}" || fail "C2 Claude exited non-zero"
grep -q 'CLAUDE_C2_TOKEN_9182' "${RUN_DIR}/C2/claude.json" || fail "C2 token missing"
log "[PASS] C2 Read SPEC.md"

W="$(new_workspace C3)"
run_claude C3 "Use Bash to run pwd and return the exact output." "${W}" || fail "C3 Claude exited non-zero"
grep -Fq "${W}" "${RUN_DIR}/C3/claude.json" || fail "C3 pwd mismatch"
log "[PASS] C3 Bash pwd"

W="$(new_workspace C4)"
run_claude C4 "Create math_helper.py defining add(a,b) returning a+b, then run it or compile it to verify." "${W}" || fail "C4 Claude exited non-zero"
python3 -m py_compile "${W}/math_helper.py" || fail "C4 syntax invalid"
python3 - "${W}" <<'PY' || fail "C4 add() verification failed"
import sys
sys.path.insert(0, sys.argv[1])
import math_helper
assert math_helper.add(2, 3) == 5
PY
log "[PASS] C4 Write Python"

W="$(new_workspace C5)"
printf 'def multiply(a, b):\n    return a + b\n' >"${W}/calc.py"
run_claude C5 "Use Edit to fix calc.py so multiply(a,b) returns a*b. Verify the file." "${W}" || fail "C5 Claude exited non-zero"
python3 - "${W}" <<'PY' || fail "C5 edit verification failed"
import sys
sys.path.insert(0, sys.argv[1])
import calc
assert calc.multiply(3, 4) == 12
PY
log "[PASS] C5 Edit Python"

W="$(new_workspace C6)"
run_claude C6 "Create string_util.py with reverse_str(s) returning reversed string, then use Bash to run python3 -m compileall -q string_util.py before finishing." "${W}" || fail "C6 Claude exited non-zero"
python3 -m compileall -q "${W}" || fail "C6 compileall failed"
python3 - "${W}" <<'PY' || fail "C6 behavior failed"
import sys
sys.path.insert(0, sys.argv[1])
import string_util
assert string_util.reverse_str('hello') == 'olleh'
PY
log "[PASS] C6 Write -> Bash compile"

W="$(new_workspace C7)"
run_claude C7 "Sequentially create file1.txt containing ONE, file2.txt containing TWO, and file3.txt containing THREE. Use one tool action at a time." "${W}" || fail "C7 Claude exited non-zero"
[[ "$(cat "${W}/file1.txt")" == "ONE" ]] || fail "C7 file1 mismatch"
[[ "$(cat "${W}/file2.txt")" == "TWO" ]] || fail "C7 file2 mismatch"
[[ "$(cat "${W}/file3.txt")" == "THREE" ]] || fail "C7 file3 mismatch"
log "[PASS] C7 three sequential files"

W="$(new_workspace C8)"
printf 'def is_even(n: int) -> bool:\n    return n %% 2 == 1\n' >"${W}/logic.py"
cat >"${W}/test_logic.py" <<'PY'
from logic import is_even

def test_is_even():
    assert is_even(4) is True
    assert is_even(5) is False
    assert is_even(0) is True
PY
run_claude C8 "Run pytest, diagnose the failing test, fix only logic.py, and rerun pytest until it passes." "${W}" || fail "C8 Claude exited non-zero"
(cd "${W}" && pytest -q) >"${RUN_DIR}/C8/pytest-final.log" 2>&1 || fail "C8 pytest still failing"
log "[PASS] C8 failing test -> self-fix"

if grep -Eq 'authenticated|plus|pro account' "${GATEWAY_LOG}"; then
  fail "Authenticated-session evidence appeared in gateway log"
fi
log "=== ALL CLAUDE CODE MICRO-GATES PASSED C1-C8 ==="
printf 'MANUAL_REVIEW_REQUIRED\n' >"${RUN_DIR}/STATUS.txt"
chmod 600 "${RUN_DIR}/STATUS.txt"
log "Artifacts: ${RUN_DIR}"
