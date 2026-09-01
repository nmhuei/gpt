from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentEvent:
    kind: str
    round_index: int
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentResult:
    success: bool
    text: str
    rounds: int
    tool_calls: int
    session_id: str | None
    stop_reason: str | None
    elapsed_seconds: float
    error: str | None = None
    verification_gate_count: int = 0


__all__ = ["AgentEvent", "AgentResult"]
