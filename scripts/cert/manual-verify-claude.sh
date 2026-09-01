#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RUNTIME_ROOT="${WEBGPT_RUNTIME_ROOT:-${HOME}/.local/share/webgpt}"
RUN_ID="manual-claude-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="${RUNTIME_ROOT}/runs/claude/${RUN_ID}"
WORKSPACE="${RUN_DIR}/workspace"
TMP_DIR="${RUNTIME_ROOT}/tmp"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
GATEWAY_PID=""
CLAUDE_PID=""

if [[ -n "${WEBGPT_GATEWAY_PORT:-}" ]]; then
  PORT="${WEBGPT_GATEWAY_PORT}"
else
  PORT="$(python - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
fi

[[ -n "${CLAUDE_BIN}" ]] || { echo "Claude Code CLI not found" >&2; exit 69; }
mkdir -p "${WORKSPACE}" "${RUN_DIR}/home" "${RUN_DIR}/config" "${RUN_DIR}/claude-config" \
  "${RUN_DIR}/prompt_debug" "${TMP_DIR}"
chmod 700 "${RUNTIME_ROOT}" "${RUN_DIR}" "${WORKSPACE}" "${RUN_DIR}/prompt_debug" "${TMP_DIR}" 2>/dev/null || true
export TMPDIR="${TMP_DIR}"
GATEWAY_LOG="${RUN_DIR}/gateway.log"
CLAUDE_LOG="${RUN_DIR}/claude.json"
CLAUDE_ERR="${RUN_DIR}/claude.err"
TRACE_FILE="${RUN_DIR}/trace.jsonl"
STORE_FILE="${RUN_DIR}/conversations.json"
touch "${GATEWAY_LOG}" "${CLAUDE_LOG}" "${CLAUDE_ERR}"
chmod 600 "${GATEWAY_LOG}" "${CLAUDE_LOG}" "${CLAUDE_ERR}"

cleanup() {
  if [[ -n "${CLAUDE_PID}" ]] && kill -0 "${CLAUDE_PID}" 2>/dev/null; then
    kill "${CLAUDE_PID}" 2>/dev/null || true
    wait "${CLAUDE_PID}" 2>/dev/null || true
  fi
  if [[ -n "${GATEWAY_PID}" ]] && kill -0 "${GATEWAY_PID}" 2>/dev/null; then
    kill "${GATEWAY_PID}" 2>/dev/null || true
    wait "${GATEWAY_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

cd "${REPO_DIR}"
WEBGPT_SERVER_CLOSE_TIMEOUT="${WEBGPT_SERVER_CLOSE_TIMEOUT:-5}" python -m gpt.debug api-server \
  --port "${PORT}" --headless --ephemeral --prewarm \
  --max-workers 1 --generation-timeout 45 \
  --conversation-store "${STORE_FILE}" \
  --trace-file "${TRACE_FILE}" \
  --prompt-debug-dir "${RUN_DIR}/prompt_debug" \
  >>"${GATEWAY_LOG}" 2>&1 &
GATEWAY_PID=$!
printf '{"run_id":"%s","account_mode":"free_anonymous","gateway_pid":%s,"port":%s,"workspace":"%s"}\n' \
  "${RUN_ID}" "${GATEWAY_PID}" "${PORT}" "${WORKSPACE}" >"${RUN_DIR}/run.json"
chmod 600 "${RUN_DIR}/run.json"

if ! python "${SCRIPT_DIR}/wait-for-anonymous-ready.py" "${PORT}" \
  --json-out "${RUN_DIR}/ready.json" >/dev/null; then
  echo "gateway did not reach free-anonymous ready state" >&2
  exit 1
fi
chmod 600 "${RUN_DIR}/ready.json"

PROMPT='Create manual_probe.py with a main() function that prints exactly MANUAL_WEBGPT_OK. Use Write/Edit/Bash as needed. Run python manual_probe.py yourself and do not finish until stdout is exactly MANUAL_WEBGPT_OK.'
(
  cd "${WORKSPACE}"
  exec env \
    HOME="${RUN_DIR}/home" \
    XDG_CONFIG_HOME="${RUN_DIR}/config" \
    CLAUDE_CONFIG_DIR="${RUN_DIR}/claude-config" \
    ANTHROPIC_BASE_URL="http://127.0.0.1:${PORT}" \
    ANTHROPIC_AUTH_TOKEN="local-webgpt-placeholder" \
    ANTHROPIC_API_KEY="local-webgpt-placeholder" \
    ANTHROPIC_MODEL="claude-fable-5" \
    ANTHROPIC_SMALL_FAST_MODEL="claude-fable-5" \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 DISABLE_BUG_COMMAND=1 \
    timeout --kill-after=5 480 "${CLAUDE_BIN}" --bare --print --no-session-persistence \
      --dangerously-skip-permissions --tools "Bash,Read,Edit,Write,Glob,Grep" \
      --output-format json "${PROMPT}"
) >"${CLAUDE_LOG}" 2>"${CLAUDE_ERR}" &
CLAUDE_PID=$!
if ! wait "${CLAUDE_PID}"; then
  CLAUDE_PID=""
  if grep -Eqi '429|rate.?limit|quota' "${CLAUDE_LOG}" "${CLAUDE_ERR}" 2>/dev/null; then
    printf 'BLOCKED_QUOTA\n' >"${RUN_DIR}/STATUS.txt"
    chmod 600 "${RUN_DIR}/STATUS.txt"
    echo "BLOCKED_QUOTA"
    echo "Artifacts: ${RUN_DIR}"
    exit 75
  fi
  echo "Claude Code exited non-zero" >&2
  exit 1
fi
CLAUDE_PID=""

[[ -f "${WORKSPACE}/manual_probe.py" ]] || { echo "manual_probe.py was not created" >&2; exit 1; }
python -m py_compile "${WORKSPACE}/manual_probe.py"
printf 'MANUAL_REVIEW_REQUIRED\n' >"${RUN_DIR}/STATUS.txt"
printf '%s\n' "${WORKSPACE}/manual_probe.py" >"${RUN_DIR}/MANUAL_CHECK_PATH.txt"
chmod 600 "${RUN_DIR}/STATUS.txt" "${RUN_DIR}/MANUAL_CHECK_PATH.txt"

echo "MANUAL_REVIEW_REQUIRED"
echo "Run directory: ${RUN_DIR}"
echo "Manual file check: ${WORKSPACE}/manual_probe.py"
echo "Required direct checks: open file, run it, inspect stdout, inspect trace, confirm ready.json auth_status=anonymous, then write MANUAL_PASS evidence."
