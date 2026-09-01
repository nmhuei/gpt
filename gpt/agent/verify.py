from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from gpt.tools.result import ToolResult

_MUTATION_RE = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:"
    r"sed\s+-i\b|perl\s+-pi\b|rm\b|mv\b|cp\b|touch\b|mkdir\b|"
    r"tee\b|truncate\b|install\b|git\s+(?:checkout|restore|reset|clean)\b"
    r")|(?:^|\s)(?:>|>>)(?:\s|$)",
    re.IGNORECASE,
)
_VERIFICATION_RE = re.compile(
    r"(?:pytest|python(?:\d+(?:\.\d+)*)?\s+-m\s+pytest|"
    r"python(?:\d+(?:\.\d+)*)?\s+-m\s+compileall|"
    r"npm\s+(?:test|run\s+test)|pnpm\s+(?:test|run\s+test)|"
    r"yarn\s+test|cargo\s+test|go\s+test|"
    r"git\s+diff\s+--check|"
    r"\bverify(?:\.py)?\b|\bcheck(?:\.py)?\b|"
    r"^\s*(?:cat|head|tail|grep|rg|sed\s+-n)\s+)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class VerificationGuard:
    mode: str = "auto"
    dirty: bool = False
    last_evidence: str | None = None
    gate_count: int = 0

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def observe(self, tool_name: str, tool_input: Any, result: ToolResult) -> None:
        if result.is_error:
            return
        if tool_name == "ApplyPatch":
            self.dirty = True
            self.last_evidence = None
            return
        if tool_name != "Bash" or not isinstance(tool_input, dict):
            return
        command = tool_input.get("command")
        if not isinstance(command, str):
            return
        if _VERIFICATION_RE.search(command):
            if result.exit_code == 0:
                self.last_evidence = command
                self.dirty = False
            return
        if _MUTATION_RE.search(command):
            self.dirty = True
            self.last_evidence = None

    def final_allowed(self) -> bool:
        return not self.enabled or not self.dirty

    def rejection_message(self) -> str:
        self.gate_count += 1
        level = {
            "quick": "Run a focused verification/read-back of the changed behavior.",
            "full": "Run the project's full relevant test/verification suite.",
        }.get(
            self.mode,
            "Run the most relevant tests or deterministic read-back for the changes.",
        )
        return (
            "Controller verification gate: the workspace was modified after the "
            "last successful verification, so completion is not accepted yet. "
            + level
            + " Use the Bash tool, inspect the real exit status, then finish only "
            "if verification succeeds."
        )


__all__ = ["VerificationGuard"]
