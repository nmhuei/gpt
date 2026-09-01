from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Protocol-neutral result returned by every local agent tool."""

    status: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False
    changed_files: tuple[str, ...] = ()
    error: str | None = None

    @property
    def is_error(self) -> bool:
        return self.status != "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "changed_files": list(self.changed_files),
            "error": self.error,
        }

    def to_model_text(self) -> str:
        lines = [f"status={self.status}"]
        if self.exit_code is not None:
            lines.append(f"exit_code={self.exit_code}")
        lines.append(f"duration_ms={self.duration_ms}")
        if self.timed_out:
            lines.append("timed_out=true")
        if self.truncated:
            lines.append("truncated=true")
        if self.changed_files:
            lines.append("changed_files=" + ",".join(self.changed_files))
        if self.error:
            lines.extend(("error:", self.error))
        lines.extend(("stdout:", self.stdout, "stderr:", self.stderr))
        return "\n".join(lines)


__all__ = ["ToolResult"]
