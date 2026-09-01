from __future__ import annotations

from pathlib import Path
from typing import Any

from .process import ProcessRunner
from .result import ToolResult

SHELL_TOOL_SCHEMA: dict[str, Any] = {
    "name": "Bash",
    "description": (
        "Run one shell command in the workspace. Use this for inspection, "
        "search, git, builds, tests, and verification. Prefer ApplyPatch for "
        "file edits. Real stdout/stderr/exit status are returned."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["command"],
    },
}


class ShellTool:
    name = "Bash"
    schema = SHELL_TOOL_SCHEMA

    def __init__(self, workspace: Path, runner: ProcessRunner) -> None:
        self.workspace = workspace
        self.runner = runner

    def execute(self, tool_input: Any) -> ToolResult:
        if not isinstance(tool_input, dict):
            return ToolResult(status="error", error="Bash input must be an object")
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult(status="error", error="Bash.command must be non-empty")
        return self.runner.run(command, cwd=self.workspace)


__all__ = ["SHELL_TOOL_SCHEMA", "ShellTool"]
