from __future__ import annotations

import glob
import json
import logging
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

# RAM-TOP5 disk guard: the active trace.jsonl segment is rotated once it grows
# past this many bytes; the number of retained rotated segments is capped by
# WEBGPT_DEBUG_MAX_FILES (see gpt.debug.resolve_debug_max_files).
_TRACE_ROTATE_BYTES = 50_000_000


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
        self._segment_bytes = 0
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
        if self._segment_bytes == 0 and self.output_path.exists():
            try:
                self._segment_bytes = self.output_path.stat().st_size
            except OSError:
                self._segment_bytes = 0
        if self._segment_bytes >= _TRACE_ROTATE_BYTES:
            self._rotate_file()
        payload = json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":"))
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
        os.chmod(self.output_path, 0o600)
        self._segment_bytes += len(payload.encode("utf-8")) + 1

    def _rotate_file(self) -> None:
        """Rename the active trace segment and prune old ones past the cap.

        Called between appends (the file is opened/closed per event), so no
        file descriptor is held across the rename.  Best-effort: rotation or
        pruning failures never compromise the running turn.
        """
        assert self.output_path is not None
        stem = self.output_path.stem
        suffix = self.output_path.suffix or ".jsonl"
        rotated = self.output_path.with_name(f"{stem}.{self._sequence:09d}{suffix}")
        try:
            os.replace(self.output_path, rotated)
        except OSError:
            logging.getLogger(__name__).warning(
                "trace_segment_rotate_failed", exc_info=True
            )
            self._segment_bytes = 0
            return
        self._segment_bytes = 0
        try:
            # Lazy import: gpt.debug pulls browser/session modules at import
            # time and would create an import cycle from this leaf module.
            from gpt.debug import prune_debug_files, resolve_debug_max_files

            prune_debug_files(
                self.output_path.parent,
                max_files=resolve_debug_max_files(),
                patterns=(f"{glob.escape(stem)}.*{suffix}",),
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "trace_segment_prune_failed", exc_info=True
            )


__all__ = ["RuntimeTraceBus", "RuntimeTraceEvent"]
