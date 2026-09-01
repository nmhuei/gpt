#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RUNTIME_ROOT="${WEBGPT_RUNTIME_ROOT:-${HOME}/.local/share/webgpt}"
RUN_ID="soak-restart-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="${RUNTIME_ROOT}/runs/smoke/${RUN_ID}"
TMP_DIR="${RUNTIME_ROOT}/tmp"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
GATEWAY_PID=""
ACTIVE_CLIENT_PID=""

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

[[ -n "${CLAUDE_BIN}" ]] || { echo "claude not found" >&2; exit 69; }
mkdir -p "${RUN_DIR}/prompt_debug" "${RUN_DIR}/projects" "${TMP_DIR}"
chmod 700 "${RUNTIME_ROOT}" "${RUN_DIR}" "${RUN_DIR}/prompt_debug" "${RUN_DIR}/projects" "${TMP_DIR}" 2>/dev/null || true
export TMPDIR="${TMP_DIR}"
GATEWAY_LOG="${RUN_DIR}/gateway.log"
TRACE_FILE="${RUN_DIR}/trace.jsonl"
STORE_FILE="${RUN_DIR}/conversations.json"
LOG_FILE="${RUN_DIR}/soak.log"
touch "${GATEWAY_LOG}" "${LOG_FILE}"
chmod 600 "${GATEWAY_LOG}" "${LOG_FILE}"

log() { printf '%s\n' "$*" | tee -a "${LOG_FILE}"; }

cleanup() {
  if [[ -n "${ACTIVE_CLIENT_PID}" ]] && kill -0 "${ACTIVE_CLIENT_PID}" 2>/dev/null; then
    kill "${ACTIVE_CLIENT_PID}" 2>/dev/null || true
    wait "${ACTIVE_CLIENT_PID}" 2>/dev/null || true
  fi
  if [[ -n "${GATEWAY_PID}" ]] && kill -0 "${GATEWAY_PID}" 2>/dev/null; then
    kill "${GATEWAY_PID}" 2>/dev/null || true
    wait "${GATEWAY_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

wait_ready() {
  python "${SCRIPT_DIR}/wait-for-anonymous-ready.py" "${PORT}" \
    --json-out "${RUN_DIR}/ready.json" >/dev/null
  chmod 600 "${RUN_DIR}/ready.json"
}

start_gateway() {
  cd "${REPO_DIR}"
  WEBGPT_SERVER_CLOSE_TIMEOUT="${WEBGPT_SERVER_CLOSE_TIMEOUT:-5}" python -m gpt.debug api-server \
    --port "${PORT}" --headless --ephemeral --prewarm \
    --max-workers 1 --generation-timeout 45 \
    --conversation-store "${STORE_FILE}" \
    --trace-file "${TRACE_FILE}" \
    --prompt-debug-dir "${RUN_DIR}/prompt_debug" \
    >>"${GATEWAY_LOG}" 2>&1 &
  GATEWAY_PID=$!
  wait_ready || return 1
  log "[PASS] gateway ready / account_mode=free_anonymous / pid=${GATEWAY_PID}"
}

stop_gateway() {
  if [[ -n "${GATEWAY_PID}" ]] && kill -0 "${GATEWAY_PID}" 2>/dev/null; then
    kill "${GATEWAY_PID}" 2>/dev/null || true
    wait "${GATEWAY_PID}" 2>/dev/null || true
  fi
  GATEWAY_PID=""
}

printf '{"run_id":"%s","account_mode":"free_anonymous","port":%s,"required":{"text":10,"sequential_tool":10,"multi_turn_tool":5,"tiny_projects":3,"restart":1}}\n' \
  "${RUN_ID}" "${PORT}" >"${RUN_DIR}/run.json"
chmod 600 "${RUN_DIR}/run.json"

start_gateway || { log "[FAIL] free-anonymous gateway did not become ready"; exit 1; }

set +e
python - "${PORT}" "${RUN_DIR}/http-soak-summary.json" <<'PY'
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

port = int(sys.argv[1])
summary_path = Path(sys.argv[2])
base = f"http://127.0.0.1:{port}"
summary: dict[str, Any] = {
    "account_mode": "free_anonymous",
    "text": {"required": 10, "passed": 0},
    "sequential_tool": {"required": 10, "passed": 0},
    "multi_turn_tool": {"required": 5, "passed": 0},
    "blocked": None,
}

def persist() -> None:
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def post(payload: dict[str, Any], session_id: str | None = None) -> tuple[dict[str, Any], str]:
    headers = {"Content-Type": "application/json", "X-WebGPT-Client": "soak-harness"}
    if session_id:
        headers["x-webgpt-session-id"] = session_id
    request = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=75) as response:
            body = json.load(response)
            sid = response.headers.get("x-webgpt-session-id", "")
            if response.status != 200:
                raise RuntimeError(f"unexpected status {response.status}: {body}")
            if not sid:
                raise RuntimeError("missing x-webgpt-session-id")
            return body, sid
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        if exc.code == 429:
            summary["blocked"] = {"code": "RATE_LIMITED", "status": 429, "body": raw[:1000]}
            persist()
            raise SystemExit(75)
        raise RuntimeError(f"HTTP {exc.code}: {raw[:1000]}") from exc


def assistant_message(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"missing choices: {body}")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError(f"missing assistant message: {body}")
    return message


def content_text(message: dict[str, Any]) -> str:
    value = message.get("content")
    return value if isinstance(value, str) else ""


def execute_echo(message: dict[str, Any], expected: str) -> tuple[dict[str, Any], str]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        raise RuntimeError(f"expected exactly one tool call, got: {calls!r}")
    call = calls[0]
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict) or function.get("name") != "Echo":
        raise RuntimeError(f"unexpected tool call: {call!r}")
    args = json.loads(function.get("arguments", "{}"))
    if args != {"text": expected}:
        raise RuntimeError(f"Echo args mismatch: expected {expected!r}, got {args!r}")
    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id:
        raise RuntimeError("tool call missing id")
    return call, call_id


tool = {
    "type": "function",
    "function": {
        "name": "Echo",
        "description": "Return the exact text supplied. Use only when the user explicitly requests Echo.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}

for index in range(1, 11):
    marker = f"SOAK_TEXT_{index:02d}"
    body, _ = post(
        {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": f"Reply exactly: {marker}"}],
            "max_completion_tokens": 64,
        }
    )
    text = content_text(assistant_message(body)).strip()
    if marker not in text:
        raise RuntimeError(f"text request {index} mismatch: {text!r}")
    summary["text"]["passed"] += 1
    persist()

for index in range(1, 11):
    marker = f"SOAK_TOOL_{index:02d}"
    done = marker + "_DONE"
    prompt = f'Use Echo exactly once with text "{marker}". After the tool result, reply exactly {done}.'
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    body, sid = post({"model": "chatgpt-web", "messages": messages, "tools": [tool], "tool_choice": "auto"})
    message = assistant_message(body)
    _call, call_id = execute_echo(message, marker)
    messages.append(message)
    messages.append({"role": "tool", "tool_call_id": call_id, "content": marker})
    body, _ = post(
        {"model": "chatgpt-web", "messages": messages, "tools": [tool], "tool_choice": "auto"},
        sid,
    )
    final = content_text(assistant_message(body)).strip()
    if done not in final:
        raise RuntimeError(f"tool request {index} final mismatch: {final!r}")
    summary["sequential_tool"]["passed"] += 1
    persist()

for index in range(1, 6):
    first_marker = f"SOAK_WF_{index:02d}_A"
    second_marker = f"SOAK_WF_{index:02d}_B"
    done = f"SOAK_WF_{index:02d}_DONE"
    prompt = (
        f'Use Echo first with text "{first_marker}". After that tool result, use Echo with text '
        f'"{second_marker}". After the second tool result, reply exactly {done}. Use one tool call per turn.'
    )
    messages = [{"role": "user", "content": prompt}]
    body, sid = post({"model": "chatgpt-web", "messages": messages, "tools": [tool], "tool_choice": "auto"})
    message = assistant_message(body)
    _, call_id = execute_echo(message, first_marker)
    messages.append(message)
    messages.append({"role": "tool", "tool_call_id": call_id, "content": first_marker})

    body, _ = post(
        {"model": "chatgpt-web", "messages": messages, "tools": [tool], "tool_choice": "auto"},
        sid,
    )
    message = assistant_message(body)
    _, call_id = execute_echo(message, second_marker)
    messages.append(message)
    messages.append({"role": "tool", "tool_call_id": call_id, "content": second_marker})

    body, _ = post(
        {"model": "chatgpt-web", "messages": messages, "tools": [tool], "tool_choice": "auto"},
        sid,
    )
    final = content_text(assistant_message(body)).strip()
    if done not in final:
        raise RuntimeError(f"workflow {index} final mismatch: {final!r}")
    summary["multi_turn_tool"]["passed"] += 1
    persist()

persist()
PY
HTTP_STATUS=$?
set -e
if [[ "${HTTP_STATUS}" == "75" ]]; then
  printf 'BLOCKED_QUOTA\n' >"${RUN_DIR}/STATUS.txt"
  chmod 600 "${RUN_DIR}/STATUS.txt"
  log "[BLOCKED] anonymous rate limit reached; stopped without session/profile churn"
  exit 75
fi
[[ "${HTTP_STATUS}" == "0" ]] || { log "[FAIL] HTTP soak failed"; exit "${HTTP_STATUS}"; }
log "[PASS] 10 text + 10 sequential tool + 5 multi-turn tool workflows"

run_project() {
  local index="$1" prompt="$2"
  local project_dir="${RUN_DIR}/projects/project-${index}"
  local workspace="${project_dir}/workspace"
  mkdir -p "${workspace}" "${project_dir}/home" "${project_dir}/config" "${project_dir}/claude-config"
  (
    cd "${workspace}"
    exec env \
      HOME="${project_dir}/home" \
      XDG_CONFIG_HOME="${project_dir}/config" \
      CLAUDE_CONFIG_DIR="${project_dir}/claude-config" \
      ANTHROPIC_BASE_URL="http://127.0.0.1:${PORT}" \
      ANTHROPIC_AUTH_TOKEN="local-webgpt-placeholder" \
      ANTHROPIC_API_KEY="local-webgpt-placeholder" \
      ANTHROPIC_MODEL="claude-fable-5" \
      ANTHROPIC_SMALL_FAST_MODEL="claude-fable-5" \
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
      DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 DISABLE_BUG_COMMAND=1 \
      timeout --kill-after=5 480 "${CLAUDE_BIN}" --bare --print --no-session-persistence \
        --dangerously-skip-permissions --tools "Bash,Read,Edit,Write,Glob,Grep" \
        --output-format json "${prompt}"
  ) >"${project_dir}/claude.json" 2>"${project_dir}/claude.err" &
  ACTIVE_CLIENT_PID=$!
  if ! wait "${ACTIVE_CLIENT_PID}"; then
    ACTIVE_CLIENT_PID=""
    if grep -Eqi '429|rate.?limit|quota' "${project_dir}/claude.err" "${project_dir}/claude.json" 2>/dev/null; then
      printf 'BLOCKED_QUOTA\n' >"${RUN_DIR}/STATUS.txt"
      chmod 600 "${RUN_DIR}/STATUS.txt"
      return 75
    fi
    return 1
  fi
  ACTIVE_CLIENT_PID=""
}

run_project 1 'Create app.py that prints exactly PROJECT_1_OK when run. Use tools, run python app.py yourself, and only finish after it succeeds.' || PROJECT_STATUS=$?
PROJECT_STATUS="${PROJECT_STATUS:-0}"
if [[ "${PROJECT_STATUS}" == "75" ]]; then log "[BLOCKED] quota during tiny project 1"; exit 75; fi
[[ "${PROJECT_STATUS}" == "0" ]] || { log "[FAIL] tiny project 1"; exit 1; }
[[ "$(cd "${RUN_DIR}/projects/project-1/workspace" && python app.py)" == "PROJECT_1_OK" ]] || { log "[FAIL] project 1 behavior"; exit 1; }
log "[PASS] tiny coding project 1"
unset PROJECT_STATUS

run_project 2 'Create math_mod.py with add(a, b) and a test_math_mod.py pytest test proving add(20,22)==42. Run pytest and fix anything necessary until it passes.' || PROJECT_STATUS=$?
PROJECT_STATUS="${PROJECT_STATUS:-0}"
if [[ "${PROJECT_STATUS}" == "75" ]]; then log "[BLOCKED] quota during tiny project 2"; exit 75; fi
[[ "${PROJECT_STATUS}" == "0" ]] || { log "[FAIL] tiny project 2"; exit 1; }
(cd "${RUN_DIR}/projects/project-2/workspace" && pytest -q) >"${RUN_DIR}/projects/project-2/pytest-final.log" 2>&1 || { log "[FAIL] project 2 pytest"; exit 1; }
log "[PASS] tiny coding project 2"
unset PROJECT_STATUS

run_project 3 'Create tiny_cli.py. Running python tiny_cli.py hello must print HELLO. Use tools to implement it and execute that exact command before finishing.' || PROJECT_STATUS=$?
PROJECT_STATUS="${PROJECT_STATUS:-0}"
if [[ "${PROJECT_STATUS}" == "75" ]]; then log "[BLOCKED] quota during tiny project 3"; exit 75; fi
[[ "${PROJECT_STATUS}" == "0" ]] || { log "[FAIL] tiny project 3"; exit 1; }
[[ "$(cd "${RUN_DIR}/projects/project-3/workspace" && python tiny_cli.py hello)" == "HELLO" ]] || { log "[FAIL] project 3 behavior"; exit 1; }
log "[PASS] tiny coding project 3"

stop_gateway
log "[PASS] first gateway stopped cleanly; restarting from a fresh browser/service start"
start_gateway || { log "[FAIL] gateway restart did not become anonymous-ready"; exit 1; }

set +e
python - "${PORT}" <<'PY'
import json, sys, urllib.error, urllib.request
port = int(sys.argv[1])
payload = {"model":"chatgpt-web","messages":[{"role":"user","content":"Reply exactly: RESTART_OK"}]}
request = urllib.request.Request(
    f"http://127.0.0.1:{port}/v1/chat/completions",
    data=json.dumps(payload).encode(),
    headers={"Content-Type":"application/json","X-WebGPT-Client":"restart-harness"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=75) as response:
        body=json.load(response)
    text=body["choices"][0]["message"].get("content") or ""
    raise SystemExit(0 if response.status==200 and "RESTART_OK" in text else 1)
except urllib.error.HTTPError as exc:
    if exc.code == 429:
        raise SystemExit(75)
    raise
PY
RESTART_STATUS=$?
set -e
if [[ "${RESTART_STATUS}" == "75" ]]; then
  printf 'BLOCKED_QUOTA\n' >"${RUN_DIR}/STATUS.txt"
  chmod 600 "${RUN_DIR}/STATUS.txt"
  log "[BLOCKED] quota reached during restart verification"
  exit 75
fi
[[ "${RESTART_STATUS}" == "0" ]] || { log "[FAIL] restart request failed"; exit 1; }
log "[PASS] restart -> new conversation works"
stop_gateway

if grep -Eqi 'INVALID_AUTHENTICATED_SESSION|anonymous_session_unavailable' "${GATEWAY_LOG}"; then
  log "[FAIL] authenticated/unavailable anonymous-session evidence in gateway log"
  exit 1
fi

python - "${TRACE_FILE}" "${RUN_DIR}/request-summary.json" <<'PY'
from __future__ import annotations
import json, sys
from collections import Counter
from pathlib import Path
source, target = map(Path, sys.argv[1:])
events=[]
if source.is_file():
    for line in source.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
kinds=Counter(f"{e.get('component')}/{e.get('kind')}" for e in events)
errors={k:v for k,v in kinds.items() if any(token in k for token in ("error","failed","commit_unknown"))}
summary={"trace_events":len(events),"event_kinds":dict(kinds),"error_kinds":errors}
target.write_text(json.dumps(summary,indent=2,sort_keys=True)+"\n",encoding="utf-8")
PY
chmod 600 "${RUN_DIR}/request-summary.json" "${RUN_DIR}/http-soak-summary.json"
printf 'SOAK_RESTART_PASS\n' >"${RUN_DIR}/STATUS.txt"
chmod 600 "${RUN_DIR}/STATUS.txt"
log "=== SOAK + RESTART PASS ==="
log "Artifacts: ${RUN_DIR}"
