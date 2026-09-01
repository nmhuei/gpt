#!/usr/bin/env bash
# WebGPT gateway watchdog.
# Kiểm tra /health của gateway; fail 3 lần liên tiếp -> systemctl --user restart webgpt-gateway.service
# Luôn exit 0 để timer không đánh dấu failure.
set -u

HEALTH_URL="http://127.0.0.1:18000/health"
STATE_FILE="/tmp/webgpt-watchdog-fail-count"
RUNTIME_ROOT="${WEBGPT_RUNTIME_ROOT:-${HOME}/.local/share/webgpt}"
LOG_DIR="${RUNTIME_ROOT}/logs"
LOG_FILE="$LOG_DIR/watchdog.log"
UNIT="webgpt-gateway.service"
THRESHOLD=3

mkdir -p "$LOG_DIR"

fails=0
if [[ -f "$STATE_FILE" ]]; then
    fails=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
    case "$fails" in
        ''|*[!0-9]*) fails=0 ;;
    esac
fi

ts() { date '+%Y-%m-%d %H:%M:%S%z'; }

if curl -sf -m 2 "$HEALTH_URL" >/dev/null 2>&1; then
    if (( fails > 0 )); then
        echo "$(ts) OK — health recovered after ${fails} consecutive failures" >> "$LOG_FILE"
    fi
    echo 0 > "$STATE_FILE"
    exit 0
fi

# Health check failed
fails=$(( fails + 1 ))
echo "$fails" > "$STATE_FILE"
echo "$(ts) FAIL ${fails}/${THRESHOLD} — cannot reach $HEALTH_URL" >> "$LOG_FILE"

if (( fails >= THRESHOLD )); then
    echo "$(ts) RESTART — threshold reached, restarting $UNIT" >> "$LOG_FILE"
    systemctl --user restart "$UNIT" >> "$LOG_FILE" 2>&1
    echo 0 > "$STATE_FILE"
    echo "$(ts) RESTART issued for $UNIT" >> "$LOG_FILE"
fi

exit 0
