from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class RuntimeTraceEvent:
    sequence: int
    monotonic_ns: int
    component: str
    kind: str
    session_id: str | None = None
    conversation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class RuntimeTraceBus:
    """Bounded local trace bus for cross-layer execution evidence.

    Events intentionally contain structural metadata only. Prompt bodies,
    credentials, cookies and browser headers must never be emitted here.
    """

    def __init__(
        self,
        *,
        max_events: int = 2_000,
        output_path: Path | str | None = None,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        self._events: deque[RuntimeTraceEvent] = deque(maxlen=max_events)
        self._sequence = 0
        self._lock = Lock()
        self.output_path = Path(output_path).expanduser() if output_path else None

    def emit(
        self,
        component: str,
        kind: str,
        *,
        session_id: str | None = None,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeTraceEvent:
        with self._lock:
            self._sequence += 1
            event = RuntimeTraceEvent(
                sequence=self._sequence,
                monotonic_ns=time.monotonic_ns(),
                component=component,
                kind=kind,
                session_id=session_id,
                conversation_id=conversation_id,
                metadata=dict(metadata or {}),
            )
            self._events.append(event)
            if self.output_path is not None:
                self._append_file(event)
            return event

    def snapshot(self, *, after_sequence: int = 0) -> list[RuntimeTraceEvent]:
        with self._lock:
            return [event for event in self._events if event.sequence > after_sequence]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def _append_file(self, event: RuntimeTraceEvent) -> None:
        assert self.output_path is not None
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.output_path.parent, 0o700)
        except PermissionError:
            # Paths such as /tmp may be operator-owned policy locations rather
            # than WebGPT-owned artifact directories.  The trace file itself is
            # still locked down below; do not convert a safe external parent
            # into a request failure.
            pass
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        os.chmod(self.output_path, 0o600)


__all__ = ["RuntimeTraceBus", "RuntimeTraceEvent"]
