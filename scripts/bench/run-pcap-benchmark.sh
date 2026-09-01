#!/usr/bin/env bash
set -euo pipefail

# Backward-compatible entrypoint. The master plan requires one canonical PCAP
# clean-room path: a fresh workspace containing only SPEC.md, a single Claude
# Code implementation attempt, deterministic grading, and failure archival.
# Keep all of those invariants in run-pcap-certification.sh instead of growing
# a second retrying benchmark implementation here.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/run-pcap-certification.sh" "$@"
