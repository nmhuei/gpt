#!/usr/bin/env bash
set -euo pipefail

case_id="${1:?usage: run-live-case.sh <test-id>}"
account_mode="${WEBGPT_ACCOUNT_MODE:-}"

case "$case_id" in
  M-AUTH)
    echo "M-AUTH is retired for current certification: authenticated browser sessions are invalid." >&2
    exit 77
    ;;
  M-BOOT|M-CHAT|M-STREAM|M-RESP|M-ANTH|M-RECOVERY|M-TOOLS|M-STORE|M-MODEL|S-BROWSER)
    expected_mode="free_anonymous"
    ;;
  *)
    echo "Unknown live case: $case_id" >&2
    exit 64
    ;;
esac

if [[ "$account_mode" != "$expected_mode" ]]; then
  echo "$case_id requires WEBGPT_ACCOUNT_MODE=$expected_mode; got ${account_mode:-unset}." >&2
  exit 77
fi

echo "Live case $case_id is authorized for $account_mode."
echo "Follow MASTER_EXECUTION_PLAN.md and save a redacted evidence record under ~/Downloads/webgpt/."
echo "Stop immediately if auth_status is not anonymous. A pre-workflow rate-limit may use one fresh ephemeral session for the run; never rotate mid-conversation."
echo "This runner deliberately does not batch or silently launch a browser."
