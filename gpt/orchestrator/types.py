from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path


class ChallengeStatus(str, enum.Enum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    ANALYZING = "ANALYZING"
    TESTING = "TESTING"
    RETRYING = "RETRYING"
    SESSION_REBOOT = "SESSION_REBOOT"
    SOLVED = "SOLVED"
    ESCALATED = "ESCALATED_NEEDS_HUMAN"


class InstanceNotLiveError(Exception):
    """Raised when the challenge instance stays unresponsive past its wait deadline."""


class SolvingStrategy(str, enum.Enum):
    STANDARD_TRIAGE = "STANDARD_TRIAGE"
    ERROR_FEEDBACK = "ERROR_FEEDBACK"
    ALTERNATIVE_VECTOR = "ALTERNATIVE_VECTOR"
    CLEAN_REBOOT = "CLEAN_REBOOT"
    EXTREME_RESONING = "EXTREME_REASONING"


@dataclass
class ChallengeTask:
    directory: Path
    name: str
    category: str = "Unknown"
    points: int = 0
    target_url: str | None = None
    status: ChallengeStatus = ChallengeStatus.PENDING
    attempt: int = 0
    max_attempts: int = 5
    current_strategy: SolvingStrategy = SolvingStrategy.STANDARD_TRIAGE
    flag: str | None = None
    error_history: list[str] = field(default_factory=list)
    diagnostic_report: str | None = None
    start_time: float = 0.0
    end_time: float = 0.0
    log_messages: list[str] = field(default_factory=list)

    @property
    def is_finished(self) -> bool:
        return self.status in {ChallengeStatus.SOLVED, ChallengeStatus.ESCALATED}
