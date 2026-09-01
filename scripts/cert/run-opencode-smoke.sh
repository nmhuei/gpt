#!/usr/bin/env bash
set -euo pipefail

if ! command -v opencode >/dev/null 2>&1; then
  echo "opencode not found on PATH" >&2
  exit 127
fi

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
RUNTIME_ROOT="${WEBGPT_RUNTIME_ROOT:-${HOME}/.local/share/webgpt}"
RUN_DIR=${OPENCODE_SMOKE_DIR:-"${RUNTIME_ROOT}/runs/opencode/fake-$(date +%Y%m%d-%H%M%S)"}
TMP_DIR="${RUNTIME_ROOT}/tmp"
mkdir -p "$RUN_DIR/home" "$RUN_DIR/config/opencode" "$RUN_DIR/proj" "$TMP_DIR"
chmod 700 "$RUNTIME_ROOT" "$RUN_DIR" "$TMP_DIR" 2>/dev/null || true
export TMPDIR="$TMP_DIR"
REQUEST_LOG="$RUN_DIR/requests.jsonl"
SERVER_LOG="$RUN_DIR/server.log"
CLIENT_OUT="$RUN_DIR/client.out"
CLIENT_ERR="$RUN_DIR/client.err"
PORT_FILE="$RUN_DIR/port.txt"

python "$ROOT/tests/benchmark/opencode_smoke_server.py" \
  --port 0 \
  --request-log "$REQUEST_LOG" \
  >"$PORT_FILE" 2>"$SERVER_LOG" &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

for _ in $(seq 1 100); do
  if [[ -s "$PORT_FILE" ]]; then
    break
  fi
  sleep 0.05
done
if [[ ! -s "$PORT_FILE" ]]; then
  echo "opencode smoke server did not start" >&2
  exit 1
fi
PORT=$(cat "$PORT_FILE")

cat >"$RUN_DIR/config/opencode/opencode.jsonc" <<JSON
{
  "\$schema": "https://opencode.ai/config.json",
  "provider": {
    "webgpt": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "WebGPT Smoke",
      "options": {
        "baseURL": "http://127.0.0.1:$PORT/v1",
        "apiKey": "fake-key"
      },
      "models": {
        "webgpt-opencode-fake": {
          "name": "WebGPT OpenCode Fake"
        }
      }
    }
  },
  "model": "webgpt/webgpt-opencode-fake",
  "small_model": "webgpt/webgpt-opencode-fake"
}
JSON

set +e
(
  cd "$RUN_DIR/proj"
  HOME="$RUN_DIR/home" \
  XDG_CONFIG_HOME="$RUN_DIR/config" \
  OPENCODE_DISABLE_AUTOUPDATE=1 \
  timeout 60 \
  opencode run --pure --model webgpt/webgpt-opencode-fake --format json \
    "Reply exactly OPENCODE_FAKE_OK and do not use tools."
) >"$CLIENT_OUT" 2>"$CLIENT_ERR"
EXIT_CODE=$?
set -e

if [[ $EXIT_CODE -ne 0 ]]; then
  echo "opencode exited with $EXIT_CODE" >&2
  echo "RUN_DIR=$RUN_DIR" >&2
  sed -n '1,160p' "$CLIENT_ERR" >&2 || true
  exit "$EXIT_CODE"
fi

python - "$REQUEST_LOG" "$CLIENT_OUT" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

request_log = Path(sys.argv[1])
client_out = Path(sys.argv[2])
requests = [json.loads(line) for line in request_log.read_text().splitlines() if line.strip()]
posts = [entry for entry in requests if entry.get("method") == "POST"]
if not posts:
    raise SystemExit("no POST request captured from opencode")
chat_posts = [entry for entry in posts if entry.get("path") == "/v1/chat/completions"]
if not chat_posts:
    raise SystemExit(f"opencode did not call /v1/chat/completions: {posts!r}")
main = chat_posts[-1]
body = main.get("body") or {}
if body.get("stream") is not True:
    raise SystemExit("opencode request did not use stream=true")
if body.get("stream_options", {}).get("include_usage") is not True:
    raise SystemExit("opencode request did not include stream_options.include_usage=true")
if body.get("max_tokens") != 32000:
    raise SystemExit("opencode request did not include expected max_tokens=32000")
if not body.get("tools"):
    raise SystemExit("opencode request did not include OpenAI function tools")
if "OPENCODE_FAKE_OK" not in client_out.read_text():
    raise SystemExit("opencode output did not include expected response text")
print(json.dumps({
    "ok": True,
    "post_count": len(posts),
    "chat_completion_count": len(chat_posts),
    "model": body.get("model"),
    "stream": body.get("stream"),
    "include_usage": body.get("stream_options", {}).get("include_usage"),
    "max_tokens": body.get("max_tokens"),
    "tool_count": len(body.get("tools") or []),
}, sort_keys=True))
PY

echo "RUN_DIR=$RUN_DIR"
