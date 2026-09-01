from __future__ import annotations

import os
import sys
from pathlib import Path


def claude_main() -> int:
    """Compatibility launcher for the legacy Claude Code bridge."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "webgpt-claude.sh"
    os.execv(str(script), [str(script), *sys.argv[1:]])
    return 1


__all__ = ["claude_main"]
