from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .process import ProcessRunner
from .result import ToolResult

APPLY_PATCH_TOOL_SCHEMA: dict[str, Any] = {
    "name": "ApplyPatch",
    "description": (
        "Apply a unified diff to files inside the workspace. Use this instead "
        "of shell quoting for ordinary edits. Paths must be workspace-relative."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "Unified diff using workspace-relative file paths.",
            }
        },
        "required": ["patch"],
    },
}

_HEADER_RE = re.compile(r"^(?:---|\+\+\+)\s+([^\t\n]+)", re.MULTILINE)


def _changed_paths(patch: str) -> tuple[str, ...]:
    paths: set[str] = set()
    for raw in _HEADER_RE.findall(patch):
        value = raw.strip()
        if value == "/dev/null":
            continue
        if value.startswith(("a/", "b/")):
            value = value[2:]
        paths.add(value)
    return tuple(sorted(paths))


def _paths_are_safe(paths: tuple[str, ...]) -> bool:
    for value in paths:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            return False
    return True


class ApplyPatchTool:
    name = "ApplyPatch"
    schema = APPLY_PATCH_TOOL_SCHEMA

    def __init__(self, workspace: Path, runner: ProcessRunner) -> None:
        self.workspace = workspace
        self.runner = runner

    def execute(self, tool_input: Any) -> ToolResult:
        if not isinstance(tool_input, dict):
            return ToolResult(status="error", error="ApplyPatch input must be an object")
        patch = tool_input.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            return ToolResult(status="error", error="ApplyPatch.patch must be non-empty")
        paths = _changed_paths(patch)
        if not paths:
            return ToolResult(status="error", error="patch has no file headers")
        if not _paths_are_safe(paths):
            return ToolResult(status="error", error="patch path escapes the workspace")

        # --forward refuses already-applied/reversed hunks; --batch prevents
        # interactive prompts. -p0 matches the model-facing workspace-relative
        # path contract and avoids hidden git assumptions.
        result = self.runner.run(
            "patch --batch --forward -p0",
            cwd=self.workspace,
            input_bytes=patch.encode("utf-8"),
        )
        return ToolResult(
            status=result.status,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            truncated=result.truncated,
            changed_files=paths if not result.is_error else (),
            error=result.error,
        )


__all__ = ["APPLY_PATCH_TOOL_SCHEMA", "ApplyPatchTool"]
