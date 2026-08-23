from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass
class ProbeEvent:
    sequence: int
    monotonic_ns: int
    wall_time: str
    source: Literal[
        "playwright",
        "cdp",
        "fetch",
        "websocket",
        "eventsource",
        "xhr",
        "dom",
        "history",
        "console",
        "driver",
    ]
    kind: str
    experiment_id: str | None = None
    url: str | None = None
    method: str | None = None
    status: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        source: Literal[
            "playwright",
            "cdp",
            "fetch",
            "websocket",
            "eventsource",
            "xhr",
            "dom",
            "history",
            "console",
            "driver",
        ],
        kind: str,
        sequence: int = 0,
        experiment_id: str | None = None,
        url: str | None = None,
        method: str | None = None,
        status: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProbeEvent:
        return cls(
            sequence=sequence,
            monotonic_ns=time.monotonic_ns(),
            wall_time=datetime.now(timezone.utc).isoformat(),
            source=source,
            kind=kind,
            experiment_id=experiment_id,
            url=url,
            method=method,
            status=status,
            metadata=metadata or {},
        )


@dataclass
class Experiment:
    id: str
    variable: str
    marker: str
    started_ns: int
    ended_ns: int | None = None
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ElementFingerprint:
    name: str
    role: str | None
    tag: str
    aria_label: str | None
    test_id: str | None
    selector_candidates: list[str] = field(default_factory=list)
    stable_attrs: dict[str, str] = field(default_factory=dict)


@dataclass
class ModelInfo:
    id: str | None
    label: str
    selected: bool = False
    available: bool = True
    source: Literal["protocol", "ui"] = "ui"
    reasoning_efforts: list[str] = field(default_factory=list)
    selected_effort: str | None = None


@dataclass
class CapabilitySnapshot:
    auth_status: str
    has_model_picker: bool
    models: list[ModelInfo] = field(default_factory=list)
    reasoning_efforts: list[str] = field(default_factory=list)
    selected_model: str | None = None
    selected_effort: str | None = None
    protocol_send_eligible: bool = False


@dataclass
class Turn:
    turn_id: str
    role: Literal["user", "assistant", "system"]
    text: str
    model: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnResult:
    turn_id: str
    conversation_id: str | None
    text: str
    model: str | None = None
    status: Literal["completed", "interrupted", "failed"] = "completed"
    error: str | None = None
    duration_ms: int = 0
    raw_events: list[ProbeEvent] = field(default_factory=list)


@dataclass(frozen=True)
class SendRequest:
    """Transport-independent request passed to a chat driver."""

    text: str
    conversation_id: str | None = None
    model: ModelInfo | None = None
    reasoning_effort: str | None = None
    timeout_seconds: float = 120.0
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProtocolFingerprint:
    """Observed protocol contract. Replay is allowed only when verified."""

    transport: Literal["fetch", "sse", "websocket"]
    path: str
    request_signature: tuple[str, ...]
    response_content_type: str
    completion_signal: str
    supporting_experiments: tuple[str, ...] = ()
    verified: bool = False


@dataclass
class RequestSubmitted:
    turn_id: str
    conversation_id: str | None = None


@dataclass
class ResponseStarted:
    turn_id: str
    model: str | None = None


@dataclass
class ResponseDelta:
    text: str
    accumulated_text: str = ""
    revision: bool = False


@dataclass
class ResponseCompleted:
    turn_id: str
    text: str
    model: str | None = None
    conversation_id: str | None = None


@dataclass
class ResponseFailed:
    turn_id: str
    reason: str
    partial_text: str = ""


@dataclass
class StateChanged:
    old_state: str
    new_state: str
    reason: str | None = None
    duration_ms: float | None = None


SessionEvent = (
    RequestSubmitted
    | ResponseStarted
    | ResponseDelta
    | ResponseCompleted
    | ResponseFailed
    | StateChanged
)


@dataclass(frozen=True)
class ReconciliationResult:
    user_turn_present: bool
    assistant_text: str | None = None
    conversation_id: str | None = None


@dataclass
class SessionInfo:
    session_id: str
    conversation_id: str | None
    conversation_url: str | None
    model: ModelInfo | None
    state: str
    created_at: str
    last_used_at: str


@dataclass
class ProtocolFinding:
    name: str
    hypothesis: str
    supporting_experiments: list[str] = field(default_factory=list)
    contradicting_experiments: list[str] = field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "medium"
    details: dict[str, Any] = field(default_factory=dict)
