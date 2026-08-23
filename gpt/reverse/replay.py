from __future__ import annotations

import asyncio
import inspect
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gpt.reverse.normalize import normalize_trace

ReplayHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True)
class ReplaySummary:
    event_count: int
    sources: dict[str, int] = field(default_factory=dict)
    kinds: dict[str, int] = field(default_factory=dict)
    experiments: dict[str, int] = field(default_factory=dict)
    first_sequence: int | None = None
    last_sequence: int | None = None


class TraceReplay:
    """Deterministic offline replay of redacted reverse-harness NDJSON.

    Replay intentionally knows nothing about the current ChatGPT protocol. A
    protocol adapter/observer subscribes through ``handler`` so the exact same
    raw corpus can exercise successive parser implementations.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self._validate_order()

    @classmethod
    def from_ndjson(cls, path: Path | str, *, max_events: int = 250_000) -> TraceReplay:
        source = Path(path).expanduser()
        events: list[dict[str, Any]] = []
        with source.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                if len(events) >= max_events:
                    raise ValueError(f"Trace exceeds max_events={max_events}")
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON at line {line_number}") from exc
                if not isinstance(payload, dict):
                    raise ValueError(f"Trace line {line_number} must be a JSON object")
                events.append(payload)
        return cls(events)

    def normalized(self) -> TraceReplay:
        return TraceReplay(normalize_trace(self.events))

    def summary(self) -> ReplaySummary:
        source_counts: Counter[str] = Counter()
        kind_counts: Counter[str] = Counter()
        experiment_counts: Counter[str] = Counter()
        sequences: list[int] = []
        for event in self.events:
            source = event.get("source")
            kind = event.get("kind")
            experiment = event.get("experiment_id")
            sequence = event.get("sequence")
            if isinstance(source, str):
                source_counts[source] += 1
            if isinstance(kind, str):
                kind_counts[kind] += 1
            if isinstance(experiment, str):
                experiment_counts[experiment] += 1
            if isinstance(sequence, int):
                sequences.append(sequence)
        return ReplaySummary(
            event_count=len(self.events),
            sources=dict(source_counts),
            kinds=dict(kind_counts),
            experiments=dict(experiment_counts),
            first_sequence=sequences[0] if sequences else None,
            last_sequence=sequences[-1] if sequences else None,
        )

    async def replay(
        self,
        handler: ReplayHandler,
        *,
        timing_scale: float = 0.0,
        max_sleep_seconds: float = 1.0,
    ) -> ReplaySummary:
        if timing_scale < 0:
            raise ValueError("timing_scale must be >= 0")
        previous_ns: int | None = None
        for event in self.events:
            current_ns = event.get("monotonic_ns")
            if (
                timing_scale > 0
                and previous_ns is not None
                and isinstance(current_ns, int)
                and current_ns >= previous_ns
            ):
                delay = min(
                    ((current_ns - previous_ns) / 1_000_000_000) * timing_scale,
                    max_sleep_seconds,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
            result = handler(dict(event))
            if inspect.isawaitable(result):
                await result
            if isinstance(current_ns, int):
                previous_ns = current_ns
        return self.summary()

    def _validate_order(self) -> None:
        previous_sequence: int | None = None
        previous_ns: int | None = None
        for index, event in enumerate(self.events):
            sequence = event.get("sequence")
            monotonic_ns = event.get("monotonic_ns")
            if not isinstance(sequence, int):
                raise ValueError(f"Trace event {index} is missing integer sequence")
            if previous_sequence is not None and sequence <= previous_sequence:
                raise ValueError("Trace sequence must be strictly increasing")
            if isinstance(monotonic_ns, int):
                if previous_ns is not None and monotonic_ns < previous_ns:
                    raise ValueError("Trace monotonic_ns must be non-decreasing")
                previous_ns = monotonic_ns
            previous_sequence = sequence


__all__ = ["ReplayHandler", "ReplaySummary", "TraceReplay"]
