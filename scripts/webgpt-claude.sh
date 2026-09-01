#!/usr/bin/env bash
# ==============================================================================
# WebGPT → Claude Code launcher (repo-hosted).
#
# Symlinked from ~/.local/bin/gpt so `pip install -e` can never clobber it.
# Environment policy: values already present in the shell win; this script
# only fills what is missing, so each terminal keeps its own scoping
# (see also: eval "$(gpt-web env)").
# ==============================================================================
set -e

GATEWAY_PORT="${WEBGPT_GATEWAY_PORT:-18000}"
GATEWAY_URL="http://127.0.0.1:${GATEWAY_PORT}"
CLAUDE_BIN="${WEBGPT_CLAUDE_BIN:-$HOME/.local/bin/claude}"

case "$1" in
status)
    echo "🔍 WebGPT Gateway Status (${GATEWAY_URL}):"
    curl -s "${GATEWAY_URL}/health" | jq . || echo "❌ Gateway is not responding."
    exit 0
    ;;
restart)
    echo "🔄 Restarting WebGPT Gateway..."
    systemctl --user restart webgpt-gateway.service
    sleep 1
    curl -s "${GATEWAY_URL}/health" | jq .
    exit 0
    ;;
esac

# Ensure the gateway service is up before handing over to Claude Code.
if ! curl -s -f -m 1 "${GATEWAY_URL}/health" > /dev/null 2>&1; then
    systemctl --user start webgpt-gateway.service 2>/dev/null || true
    for _ in {1..6}; do
        if curl -s -f -m 1 "${GATEWAY_URL}/health" > /dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done
fi

# Fill only what the terminal has not already set.
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-${GATEWAY_URL}}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-sk-webgpt-local}"
export CLAUDE_DEFAULT_MODEL="${CLAUDE_DEFAULT_MODEL:-claude-3-5-sonnet}"
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="${CLAUDE_CODE_MAX_CONTEXT_TOKENS:-200000}"
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${CLAUDE_CODE_MAX_OUTPUT_TOKENS:-8192}"

if [ $# -eq 0 ]; then
    exec "${CLAUDE_BIN}" --dangerously-skip-permissions
else
    exec "${CLAUDE_BIN}" "$@" --dangerously-skip-permissions
fi
