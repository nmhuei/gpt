#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RUNTIME_ROOT="${WEBGPT_RUNTIME_ROOT:-${HOME}/.local/share/webgpt}"
MODE="normal"
if [[ "${1:-}" == "--final" ]]; then
  MODE="final"
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--final]" >&2
  exit 2
fi

CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
[[ -n "${CLAUDE_BIN}" ]] || { echo "claude not found" >&2; exit 69; }
RUN_ID="pcap-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="${RUNTIME_ROOT}/benchmarks/pcap/${RUN_ID}"
WORKSPACE="${RUN_DIR}/workspace"
PROMPT_DEBUG="${RUN_DIR}/prompt_debug"
TMP_DIR="${RUNTIME_ROOT}/tmp"
PORT="${WEBGPT_GATEWAY_PORT:-18766}"
GATEWAY_PID=""
CLAUDE_PID=""

mkdir -p "${WORKSPACE}" "${PROMPT_DEBUG}" "${TMP_DIR}" \
  "${RUNTIME_ROOT}/failed-runs" "${RUNTIME_ROOT}/successful-runs"
chmod 700 "${RUNTIME_ROOT}" "${RUN_DIR}" "${WORKSPACE}" "${PROMPT_DEBUG}" "${TMP_DIR}" 2>/dev/null || true
export TMPDIR="${TMP_DIR}"
cp "${REPO_DIR}/tests/benchmark/pcap_spec.md" "${WORKSPACE}/SPEC.md"
[[ "$(find "${WORKSPACE}" -mindepth 1 -maxdepth 1 -printf '%f\n')" == "SPEC.md" ]] || {
  echo "clean-room workspace contains files other than SPEC.md" >&2
  exit 1
}

GATEWAY_LOG="${RUN_DIR}/gateway.log"
CLAUDE_LOG="${RUN_DIR}/claude.log"
CLAUDE_ERR="${RUN_DIR}/claude.err"
TRACE_FILE="${RUN_DIR}/trace.jsonl"
STORE_FILE="${RUN_DIR}/conversations.json"
touch "${GATEWAY_LOG}" "${CLAUDE_LOG}" "${CLAUDE_ERR}"
chmod 600 "${GATEWAY_LOG}" "${CLAUDE_LOG}" "${CLAUDE_ERR}"

stop_gateway() {
  if [[ -n "${GATEWAY_PID}" ]] && kill -0 "${GATEWAY_PID}" 2>/dev/null; then
    kill "${GATEWAY_PID}" 2>/dev/null || true
    wait "${GATEWAY_PID}" 2>/dev/null || true
  fi
  GATEWAY_PID=""
}

port_is_free() {
  python - "${PORT}" <<'PYPORT'
import socket, sys
port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("127.0.0.1", port))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PYPORT
}

start_gateway() {
  local ready_file="$1"
  if ! port_is_free; then
    echo "gateway port ${PORT} is already in use; refusing to kill an unrelated process" >>"${GATEWAY_LOG}"
    return 1
  fi
  WEBGPT_SERVER_CLOSE_TIMEOUT="${WEBGPT_SERVER_CLOSE_TIMEOUT:-5}" uv run python -m gpt.debug api-server \
    --port "${PORT}" --headless --ephemeral --prewarm \
    --max-workers 1 --generation-timeout 120 \
    --conversation-store "${STORE_FILE}" \
    --trace-file "${TRACE_FILE}" \
    --prompt-debug-dir "${PROMPT_DEBUG}" \
    >>"${GATEWAY_LOG}" 2>&1 &
  GATEWAY_PID=$!
  printf '{"run_id":"%s","mode":"%s","account_mode":"free_anonymous","gateway_pid":%s,"workspace":"%s"}\n' \
    "${RUN_ID}" "${MODE}" "${GATEWAY_PID}" "${WORKSPACE}" >"${RUN_DIR}/run.json"
  chmod 600 "${RUN_DIR}/run.json"
  if ! uv run python "${REPO_DIR}/scripts/cert/wait-for-anonymous-ready.py" "${PORT}" \
    --json-out "${ready_file}" >/dev/null; then
    local ready_rc=1
    if [[ -f "${ready_file}" ]] && grep -q '"backend": "RateLimited"' "${ready_file}"; then
      ready_rc=75
    fi
    stop_gateway
    return "${ready_rc}"
  fi
  chmod 600 "${ready_file}"
}

start_gateway_with_rate_limit_restarts() {
  local ready_prefix="$1"
  local max_restarts="${WEBGPT_ANON_BROWSER_RESTARTS:-3}"
  [[ "${max_restarts}" =~ ^[0-9]+$ ]] || return 2
  local restart=0
  while true; do
    local ready_file="${ready_prefix}-${restart}.json"
    local rc=0
    if start_gateway "${ready_file}"; then
      cp "${ready_file}" "${RUN_DIR}/ready.json"
      chmod 600 "${RUN_DIR}/ready.json"
      return 0
    else
      rc=$?
    fi
    if [[ "${rc}" != "75" ]]; then
      return "${rc}"
    fi
    if [[ "${restart}" -ge "${max_restarts}" ]]; then
      return 75
    fi
    restart=$((restart + 1))
    echo "Anonymous readiness hit 429; fully reopening gateway/browser (${restart}/${max_restarts})." >>"${GATEWAY_LOG}"
    sleep 1
  done
}

archive_attempt_workspace() {
  local attempt="$1"
  local attempt_root="${RUN_DIR}/attempts/attempt-${attempt}"
  mkdir -p "${attempt_root}"
  if [[ -d "${WORKSPACE}" ]]; then
    mv "${WORKSPACE}" "${attempt_root}/workspace"
  fi
  mkdir -p "${WORKSPACE}"
  cp "${REPO_DIR}/tests/benchmark/pcap_spec.md" "${WORKSPACE}/SPEC.md"
}

cleanup() {
  if [[ -n "${CLAUDE_PID}" ]] && kill -0 "${CLAUDE_PID}" 2>/dev/null; then
    kill "${CLAUDE_PID}" 2>/dev/null || true
    wait "${CLAUDE_PID}" 2>/dev/null || true
  fi
  stop_gateway
}

archive_failure() {
  local reason="$1"
  cleanup
  trap - EXIT INT TERM
  printf '%s\n' "${reason}" >"${RUN_DIR}/FAILURE_REASON.txt"
  local destination="${RUNTIME_ROOT}/failed-runs/${RUN_ID}"
  [[ ! -e "${destination}" ]] || { echo "failure archive already exists: ${destination}" >&2; exit 1; }
  mv "${RUN_DIR}" "${destination}"
  echo "CERTIFICATION_FAILED: ${reason}" >&2
  echo "Artifacts: ${destination}" >&2
  exit 1
}

cd "${REPO_DIR}"
if [[ "${MODE}" == "final" ]]; then
  MANUAL_RECORDS="${RUNTIME_ROOT}/manual-verification.jsonl"
  [[ -f "${MANUAL_RECORDS}" ]] || archive_failure "FINAL_MODE_REQUIRES_MANUAL_VERIFICATION_RECORDS"
  if ! uv run python -m gpt.debug manual-status \
    --input "${MANUAL_RECORDS}" --require-pass >"${RUN_DIR}/manual-status.json"; then
    archive_failure "FINAL_MODE_REQUIRES_COMPLETE_MANUAL_VERIFICATION"
  fi
  cp "${MANUAL_RECORDS}" "${RUN_DIR}/manual-verification.jsonl"
  chmod 600 "${RUN_DIR}/manual-status.json" "${RUN_DIR}/manual-verification.jsonl"
  printf 'MANUAL_PASS\nsource=manual-verification.jsonl\n' >"${RUN_DIR}/MANUAL_PASS.txt"
  chmod 600 "${RUN_DIR}/MANUAL_PASS.txt"
fi


if start_gateway_with_rate_limit_restarts "${RUN_DIR}/ready-initial"; then
  :
else
  rc=$?
  if [[ "${rc}" == "75" ]]; then
    archive_failure "FREE_ANONYMOUS_RESTART_BUDGET_EXHAUSTED_AT_READINESS"
  fi
  archive_failure "FREE_ANONYMOUS_BASELINE_NOT_READY_OR_PORT_BUSY"
fi

GATE_PROMPT='Read SPEC.md first. Implement the entire PCAP analysis automation project in this workspace from that specification. Work autonomously and sequentially. Use controller tools for all filesystem/shell actions. Run compileall, pytest, CLI help, successful fixture analysis, and bad-input checks yourself. Diagnose and fix any failures you create before finishing. Do not modify SPEC.md. Do not claim completion until every mandatory specification item and all of your tests pass.'
MAX_ATTEMPTS="${WEBGPT_PCAP_MAX_ATTEMPTS:-6}"
[[ "${MAX_ATTEMPTS}" =~ ^[1-9][0-9]*$ ]] || archive_failure "INVALID_PCAP_MAX_ATTEMPTS"
attempt=1
claude_success=false
while [ "${attempt}" -le "${MAX_ATTEMPTS}" ]; do
  ATTEMPT_ROOT="${RUN_DIR}/attempts/attempt-${attempt}"
  ATTEMPT_LOG="${ATTEMPT_ROOT}/claude.log"
  ATTEMPT_ERR="${ATTEMPT_ROOT}/claude.err"
  ATTEMPT_GATEWAY="${ATTEMPT_ROOT}/gateway.log"
  mkdir -p "${ATTEMPT_ROOT}/home" "${ATTEMPT_ROOT}/config" "${ATTEMPT_ROOT}/claude-config"
  touch "${ATTEMPT_LOG}" "${ATTEMPT_ERR}" "${ATTEMPT_GATEWAY}"
  chmod 600 "${ATTEMPT_LOG}" "${ATTEMPT_ERR}" "${ATTEMPT_GATEWAY}"
  gateway_line_before="$(wc -l <"${GATEWAY_LOG}")"
  echo "=== Claude Code Attempt ${attempt}/${MAX_ATTEMPTS} at $(date -u +'%Y-%m-%dT%H:%M:%SZ') ===" | tee -a "${CLAUDE_LOG}" "${ATTEMPT_LOG}" >/dev/null
  (
    cd "${WORKSPACE}"
    exec env \
      HOME="${ATTEMPT_ROOT}/home" \
      XDG_CONFIG_HOME="${ATTEMPT_ROOT}/config" \
      CLAUDE_CONFIG_DIR="${ATTEMPT_ROOT}/claude-config" \
      ANTHROPIC_BASE_URL="http://127.0.0.1:${PORT}" \
      ANTHROPIC_AUTH_TOKEN="local-webgpt-placeholder" \
      ANTHROPIC_API_KEY="local-webgpt-placeholder" \
      ANTHROPIC_MODEL="claude-3-5-sonnet-20241022" \
      ANTHROPIC_SMALL_FAST_MODEL="claude-3-5-sonnet-20241022" \
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
      DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 DISABLE_BUG_COMMAND=1 \
      timeout --kill-after=10 3600 "${CLAUDE_BIN}" --bare --print --no-session-persistence \
        --dangerously-skip-permissions \
        --output-format json "${GATE_PROMPT}"
  ) >>"${ATTEMPT_LOG}" 2>>"${ATTEMPT_ERR}" &
  CLAUDE_PID=$!
  if wait "${CLAUDE_PID}"; then
    CLAUDE_PID=""
    cat "${ATTEMPT_LOG}" >>"${CLAUDE_LOG}"
    cat "${ATTEMPT_ERR}" >>"${CLAUDE_ERR}"
    claude_success=true
    break
  fi
  CLAUDE_PID=""
  cat "${ATTEMPT_LOG}" >>"${CLAUDE_LOG}"
  cat "${ATTEMPT_ERR}" >>"${CLAUDE_ERR}"
  tail -n "+$((gateway_line_before + 1))" "${GATEWAY_LOG}" >"${ATTEMPT_GATEWAY}" || true

  if grep -Eqi '"api_error_status"[[:space:]]*:[[:space:]]*429|Request rejected \(429\)|429 Too Many Requests|rate[_ -]?limit' \
      "${ATTEMPT_LOG}" "${ATTEMPT_ERR}" "${ATTEMPT_GATEWAY}"; then
    echo "Claude Code attempt ${attempt} hit anonymous 429; closing the whole gateway/browser and restarting from a clean workflow." >>"${CLAUDE_LOG}"
    archive_attempt_workspace "${attempt}"
    stop_gateway
    sleep 1
    if start_gateway_with_rate_limit_restarts "${RUN_DIR}/ready-restart-${attempt}"; then
      :
    else
      rc=$?
      if [[ "${rc}" == "75" ]]; then
        archive_failure "FREE_ANONYMOUS_RESTART_BUDGET_EXHAUSTED_AFTER_429"
      fi
      archive_failure "FREE_ANONYMOUS_RESTART_FAILED_AFTER_429"
    fi
    attempt=$((attempt + 1))
    continue
  fi

  archive_failure "CLAUDE_CODE_EXITED_NONZERO_WITHOUT_429"
done

if [ "${claude_success}" != "true" ]; then
  archive_failure "CLAUDE_CODE_RATE_LIMIT_RESTART_BUDGET_EXHAUSTED"
fi



(
  cd "${WORKSPACE}"
  python -m compileall -q .
) >"${RUN_DIR}/compile.log" 2>&1 || archive_failure "PROJECT_COMPILE_FAILED"
(
  cd "${WORKSPACE}"
  python -m pytest -q
) >"${RUN_DIR}/pytest.log" 2>&1 || archive_failure "PROJECT_PYTEST_FAILED"
(
  cd "${WORKSPACE}"
  python -m pcap_analysis_automation --help
) >"${RUN_DIR}/cli-smoke.log" 2>&1 || archive_failure "PROJECT_CLI_HELP_FAILED"

python "${REPO_DIR}/tests/benchmark/generate_pcap_fixture.py" "${RUN_DIR}/fixture.pcap"
(
  cd "${WORKSPACE}"
  python -m pcap_analysis_automation \
    --input "${RUN_DIR}/fixture.pcap" --out "${RUN_DIR}" --format both
) >>"${RUN_DIR}/cli-smoke.log" 2>&1 || archive_failure "PROJECT_FIXTURE_ANALYSIS_FAILED"

if ! python "${REPO_DIR}/tests/benchmark/pcap_grader.py" \
  "${WORKSPACE}" --json-out "${RUN_DIR}/score.json" \
  >"${RUN_DIR}/grader.log" 2>&1; then
  archive_failure "PCAP_SCORE_BELOW_100"
fi

python - "${TRACE_FILE}" "${RUN_DIR}/request-summary.json" <<'PY'
import json, sys
from collections import Counter
from pathlib import Path
source, target = map(Path, sys.argv[1:])
events=[]
if source.is_file():
    for line in source.read_text(encoding='utf-8').splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
kinds=Counter(f"{e.get('component')}/{e.get('kind')}" for e in events)
requests=[
    e for e in events
    if e.get('component')=='api' and e.get('kind')=='request_completed'
]
request_meta=[e.get('metadata') or {} for e in requests]
errors=[
    {
        'request_id':m.get('request_id'),
        'protocol':m.get('protocol'),
        'http_status':m.get('http_status'),
        'error':m.get('error'),
    }
    for m in request_meta if m.get('status')!='ok'
]
latencies=[int(m.get('duration_ms') or 0) for m in request_meta]
actual_tool_calls=sum(
    int((e.get('metadata') or {}).get('tool_calls') or 0)
    for e in events
    if e.get('component')=='assistantturn' and e.get('kind')=='parsed'
)
timeouts=sum(
    1
    for e in events
    if e.get('component')=='completionruntime'
    and e.get('kind')=='submit_failed_before_commit_unknown'
    and (e.get('metadata') or {}).get('error_type')=='GenerationTimeout'
)
summary={
    'api_requests':len(requests),
    'successful_requests':sum(1 for m in request_meta if m.get('status')=='ok'),
    'tool_calls':actual_tool_calls,
    'corrections':kinds.get('completionruntime/tool_correction',0),
    'timeouts':timeouts,
    'errors':errors,
    'average_latency_ms':round(sum(latencies)/len(latencies),2) if latencies else 0,
    'trace_events':len(events),
    'commit_unknown':kinds.get('completionruntime/commit_unknown',0),
    'submit_failures':kinds.get('completionruntime/submit_failed_before_commit_unknown',0),
    'prompt_compactions':kinds.get('promptcompat/prompt_compacted',0),
    'error_kinds':{k:v for k,v in kinds.items() if any(word in k for word in ('failed','error','commit_unknown'))},
}
target.write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n',encoding='utf-8')
PY

if grep -Eqi 'anonymous_session_unavailable|web_ui_changed|browser_disconnected|internal_error|malformed_model_tool_call|conversation_conflict' "${GATEWAY_LOG}"; then
  archive_failure "UNEXPECTED_GATEWAY_LAYER_ERROR_IN_CERTIFICATION"
fi

cleanup
trap - EXIT INT TERM
printf 'AUTOMATED_PCAP_CERTIFICATION_PASS\n' >"${RUN_DIR}/AUTOMATED_PASS.txt"
chmod 600 "${RUN_DIR}/AUTOMATED_PASS.txt"

if [[ "${MODE}" == "final" ]]; then
  FINAL_DIR="${RUNTIME_ROOT}/successful-runs/pcap-final"
  [[ ! -e "${FINAL_DIR}" ]] || archive_failure "FINAL_ARTIFACT_DIRECTORY_ALREADY_EXISTS"
  mv "${RUN_DIR}" "${FINAL_DIR}"
  echo "FINAL_PCAP_CERTIFICATION_PASS"
  echo "Artifacts: ${FINAL_DIR}"
else
  echo "PCAP_CERTIFICATION_PASS"
  echo "Artifacts: ${RUN_DIR}"
fi
