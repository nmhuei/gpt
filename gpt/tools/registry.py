from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .patch import ApplyPatchTool
from .process import ProcessRunner
from .result import ToolResult
from .shell import ShellTool


class LocalTool(Protocol):
    name: str
    schema: dict[str, Any]

    def execute(self, tool_input: Any) -> ToolResult: ...


class ToolRegistry:
    """The deliberately tiny model-facing local tool surface."""

    def __init__(
        self,
        workspace: Path,
        *,
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        runner = process_runner or ProcessRunner()
        tools: list[LocalTool] = [
            ShellTool(self.workspace, runner),
            ApplyPatchTool(self.workspace, runner),
        ]
        self._tools = {tool.name: tool for tool in tools}

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema for tool in self._tools.values()]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def execute(self, name: str, tool_input: Any) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                status="error",
                error=f"unsupported tool {name!r}; available: {', '.join(self.names)}",
            )
        return tool.execute(tool_input)


__all__ = ["ToolRegistry"]
