#!/usr/bin/env bash
set -euo pipefail

if [[ "${WEBGPT_RUN_CLAUDE_BENCHMARK:-}" != "1" ]]; then
  echo "Set WEBGPT_RUN_CLAUDE_BENCHMARK=1 to authorize a real browser-backed benchmark." >&2
  exit 77
fi
if [[ "${WEBGPT_ACCOUNT_MODE:-}" != "free_anonymous" ]]; then
  echo "Claude Code benchmark requires WEBGPT_ACCOUNT_MODE=free_anonymous." >&2
  exit 77
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runtime_root="${WEBGPT_RUNTIME_ROOT:-${HOME}/.local/share/webgpt}"
claude_bin="${CLAUDE_BIN:-claude}"
if ! command -v "$claude_bin" >/dev/null 2>&1; then
  echo "Claude Code CLI was not found: $claude_bin" >&2
  exit 69
fi

mkdir -p "$runtime_root/runs/claude" "$runtime_root/tmp"
chmod 700 "$runtime_root" "$runtime_root/runs/claude" "$runtime_root/tmp" 2>/dev/null || true
export TMPDIR="$runtime_root/tmp"
benchmark_root="$(mktemp -d "$runtime_root/runs/claude/relayqueue-benchmark.XXXXXX")"
project_dir="$benchmark_root/relayqueue"
smoke_port="${WEBGPT_CLAUDE_SMOKE_PORT:-18765}"
gateway_port="${WEBGPT_GATEWAY_PORT:-18766}"
before_status="$(git -C "$repo_root" status --porcelain)"
smoke_pid=""
gateway_pid=""
active_claude_pid=""

cleanup() {
  if [[ -n "$smoke_pid" ]]; then kill "$smoke_pid" 2>/dev/null || true; fi
  if [[ -n "$gateway_pid" ]]; then kill "$gateway_pid" 2>/dev/null || true; fi
  if [[ -n "$active_claude_pid" ]]; then kill "$active_claude_pid" 2>/dev/null || true; fi
  if [[ "${WEBGPT_KEEP_BENCHMARK:-}" != "1" ]]; then rm -rf "$benchmark_root"; fi
}
trap cleanup EXIT

wait_http() {
  local port="$1"
  local path="$2"
  for _ in {1..60}; do
    if python - "$port" "$path" <<'PY'
import sys
from urllib.request import urlopen
try:
    with urlopen(f"http://127.0.0.1:{sys.argv[1]}{sys.argv[2]}", timeout=1) as response:
        raise SystemExit(0 if 200 <= response.status < 300 else 1)
except Exception:
    raise SystemExit(1)
PY
    then
      return 0
    fi
    sleep 1
  done
  return 1
}

run_claude() {
  local base_url="$1"
  local prompt="$2"
  (
    cd "$project_dir"
    exec env \
    HOME="$benchmark_root/home" \
    XDG_CONFIG_HOME="$benchmark_root/config" \
    CLAUDE_CONFIG_DIR="$benchmark_root/claude-config" \
    ANTHROPIC_BASE_URL="$base_url" \
    ANTHROPIC_AUTH_TOKEN="local-webgpt-placeholder" \
    ANTHROPIC_API_KEY="local-webgpt-placeholder" \
    ANTHROPIC_MODEL="claude-fable-5" \
    ANTHROPIC_SMALL_FAST_MODEL="claude-fable-5" \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    DISABLE_BUG_COMMAND=1 \
    timeout 2700 "$claude_bin" --bare --print --no-session-persistence \
      --allow-dangerously-skip-permissions --dangerously-skip-permissions \
      --tools "Bash,Read,Edit,Write,Glob,Grep" \
      --output-format json "$prompt"
  )
}

run_with_gateway_watchdog() {
  local output_path="$1"
  local error_path="$2"
  local prompt="$3"
  run_claude "http://127.0.0.1:$gateway_port" "$prompt" >"$output_path" 2>"$error_path" &
  active_claude_pid=$!
  while kill -0 "$active_claude_pid" 2>/dev/null; do
    local gateway_failures
    gateway_failures=$(rg -c 'POST /v1/messages.*" (429|502|503|504)' "$benchmark_root/gateway.log" || true)
    if [[ "$gateway_failures" -ge 2 ]]; then
      echo "Stopping benchmark after $gateway_failures repeated gateway 5xx responses." >&2
      kill "$active_claude_pid" 2>/dev/null || true
      wait "$active_claude_pid" 2>/dev/null || true
      active_claude_pid=""
      return 75
    fi
    sleep 2
  done
  wait "$active_claude_pid"
  active_claude_pid=""
}

mkdir -p "$project_dir" "$benchmark_root/home"
cp "$repo_root/tests/benchmark/relayqueue_spec.md" "$project_dir/SPEC.md"
chmod 0444 "$project_dir/SPEC.md"

python "$repo_root/tests/benchmark/claude_code_smoke_server.py" --port "$smoke_port" \
  >"$benchmark_root/smoke-server.log" 2>&1 &
smoke_pid="$!"
wait_http "$smoke_port" "/health" || true
if ! run_claude "http://127.0.0.1:$smoke_port" \
  "Reply with exactly OK. Do not inspect files or call tools." \
  >"$benchmark_root/smoke-client.json" 2>"$benchmark_root/smoke-client.err"; then
  echo "Claude Code local-Anthropic smoke test failed; see $benchmark_root." >&2
  exit 1
fi
kill "$smoke_pid" 2>/dev/null || true
smoke_pid=""

gateway_args=(api-server --ephemeral --port "$gateway_port" --anthropic-force-initial-tool)
if [[ "${WEBGPT_GATEWAY_HEADFUL:-1}" == "1" ]]; then
  gateway_args+=(--headful)
fi
python -m gpt.debug "${gateway_args[@]}" \
  >"$benchmark_root/gateway.log" 2>&1 &
gateway_pid="$!"
if ! python "$repo_root/scripts/cert/wait-for-anonymous-ready.py" "$gateway_port" \
  --json-out "$benchmark_root/ready.json" >/dev/null; then
  echo "Local gateway did not become Free-anonymous ready; see $benchmark_root/gateway.log" >&2
  exit 1
fi
chmod 600 "$benchmark_root/ready.json"

assignment="Use the advertised external tools. First call Read with exactly {\"file_path\":\"SPEC.md\"}; do not claim that filesystem tools are unavailable. Then implement the complete RelayQueue project in the current directory. You may create source, tests, README, and project metadata only here. Run your own tests. Do not access or modify the benchmark runner, grader, gateway configuration, parent directory, or files outside this project. Stop when implementation is complete."
if ! run_with_gateway_watchdog \
  "$benchmark_root/claude-output.json" \
  "$benchmark_root/claude-output.err" \
  "$assignment"; then
  echo "Claude Code implementation attempt failed; preserved at $benchmark_root when WEBGPT_KEEP_BENCHMARK=1." >&2
  exit 1
fi

python "$repo_root/tests/benchmark/relayqueue_grader.py" "$project_dir" \
  | tee "$benchmark_root/grader-result.json"

after_status="$(git -C "$repo_root" status --porcelain)"
if [[ "$before_status" != "$after_status" ]]; then
  echo "Benchmark modified the toolkit worktree outside its sandbox." >&2
  exit 1
fi

echo "Benchmark PASS. Set WEBGPT_KEEP_BENCHMARK=1 before running to retain artifacts."
