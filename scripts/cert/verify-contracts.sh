#!/usr/bin/env bash
set -euo pipefail

pytest -q \
  tests/test_api_server.py \
  tests/test_protocol_adapters.py \
  tests/test_requests.py \
  tests/test_conversations.py \
  tests/test_runtime_stress.py \
  tests/test_tool_transpiler.py \
  tests/test_gateway_agent_loop.py
