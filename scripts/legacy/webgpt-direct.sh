#!/usr/bin/env bash
# Thin user launcher: all policy/state now lives in gpt.cli.main.
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${SCRIPT_PATH}")/../.." && pwd)"
PYTHON_BIN="${WEBGPT_PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"

exec "${PYTHON_BIN}" -m gpt.cli.main "$@"
