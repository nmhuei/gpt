#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RUNTIME_ROOT="${WEBGPT_RUNTIME_ROOT:-${HOME}/.local/share/webgpt}"
RUN_ID="process-lifecycle-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="${RUNTIME_ROOT}/runs/smoke/${RUN_ID}"
TMP_DIR="${RUNTIME_ROOT}/tmp"
GATEWAY_PID=""

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

mkdir -p "${RUN_DIR}/prompt_debug" "${TMP_DIR}"
chmod 700 "${RUNTIME_ROOT}" "${RUN_DIR}" "${RUN_DIR}/prompt_debug" "${TMP_DIR}" 2>/dev/null || true
export TMPDIR="${TMP_DIR}"
GATEWAY_LOG="${RUN_DIR}/gateway.log"
TRACE="${RUN_DIR}/trace.jsonl"
STORE="${RUN_DIR}/conversations.json"
touch "${GATEWAY_LOG}"
chmod 600 "${GATEWAY_LOG}"

cleanup() {
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
  --conversation-store "${STORE}" --trace-file "${TRACE}" \
  --prompt-debug-dir "${RUN_DIR}/prompt_debug" \
  >>"${GATEWAY_LOG}" 2>&1 &
GATEWAY_PID=$!

if ! python "${SCRIPT_DIR}/wait-for-anonymous-ready.py" "${PORT}" \
  --json-out "${RUN_DIR}/ready.json" >/dev/null; then
  echo "gateway did not become anonymous-ready" >&2
  exit 1
fi
chmod 600 "${RUN_DIR}/ready.json"

python - "${GATEWAY_PID}" "${RUN_DIR}/processes-before.json" <<'PY'
from __future__ import annotations
import json, sys
from pathlib import Path

root = int(sys.argv[1])
out = Path(sys.argv[2])

def stat(pid: int):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    right = raw.rsplit(") ", 1)[1].split()
    ppid = int(right[1])
    starttime = int(right[19])
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        cmd = ""
    return {"pid": pid, "ppid": ppid, "starttime": starttime, "cmd": cmd}

items = {}
for proc in Path("/proc").iterdir():
    if proc.name.isdigit():
        value = stat(int(proc.name))
        if value:
            items[value["pid"]] = value
selected = {root}
changed = True
while changed:
    changed = False
    for pid, value in items.items():
        if value["ppid"] in selected and pid not in selected:
            selected.add(pid)
            changed = True
records = [items[pid] for pid in sorted(selected) if pid in items]
out.write_text(json.dumps(records, indent=2) + "\n")
PY

printf '{"run_id":"%s","account_mode":"free_anonymous","gateway_pid":%s,"port":%s}\n' \
  "${RUN_ID}" "${GATEWAY_PID}" "${PORT}" >"${RUN_DIR}/run.json"
chmod 600 "${RUN_DIR}/run.json" "${RUN_DIR}/processes-before.json"

kill "${GATEWAY_PID}"
wait "${GATEWAY_PID}" || true
GATEWAY_PID=""
sleep 2

python - "${RUN_DIR}/processes-before.json" "${RUN_DIR}/processes-after.json" <<'PY'
from __future__ import annotations
import json, sys
from pathlib import Path

before_path, after_path = map(Path, sys.argv[1:])
before = json.loads(before_path.read_text())
alive = []
for item in before:
    pid = item["pid"]
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        continue
    right = raw.rsplit(") ", 1)[1].split()
    starttime = int(right[19])
    if starttime == item["starttime"]:
        alive.append(item)
after_path.write_text(json.dumps(alive, indent=2) + "\n")
if alive:
    print(json.dumps(alive, indent=2), file=sys.stderr)
    raise SystemExit("orphan process(es) remained after gateway shutdown")
PY
chmod 600 "${RUN_DIR}/processes-after.json"
printf 'PROCESS_LIFECYCLE_PASS\n' >"${RUN_DIR}/PASS.txt"
chmod 600 "${RUN_DIR}/PASS.txt"
echo "PROCESS_LIFECYCLE_PASS"
echo "Artifacts: ${RUN_DIR}"
