#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
pytest -q \
  tests/test_fault_injection.py \
  tests/test_gateway_agent_loop.py \
  tests/test_api_server.py \
  tests/test_tool_transpiler.py \
  -k 'fault or empty_assistant or require_anonymous or sixteen_tool or malformed_tool or wrong_tool_call or duplicate_tool_results or commit_unknown or prewarm_failure or multiple_tool_calls or correction_budget or collapsed_newlines or invalid_python or workspace_escape'
