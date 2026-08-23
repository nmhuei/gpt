#!/usr/bin/env bash
set -euo pipefail

pytest -q
ruff check .
mypy gpt --ignore-missing-imports
python -m compileall -q gpt
