"""RAM-TOP5 regression caps.

Covers three unbounded-growth sources flagged by the 2026-08-25 architecture
audit (docs/reports/verify-fromscratch-2026-08-25/architecture.md):

(a) CurlCffiSession._events -- live event queue now drops the OLDEST buffered
    event past WEBGPT_HYBRID_EVENT_QUEUE_CAP instead of growing forever when
    no stream callback consumes deltas;
(b) gpt.conversations._canonical_memo -- LRU bound is env-tunable
    (WEBGPT_CANONICAL_MEMO_MAX) and evicts least-recently-used entries first,
    preserving cache correctness (hits stay hits until evicted);
(c) debug disk growth -- prune_debug_files deletes the oldest files beyond
    WEBGPT_DEBUG_MAX_FILES, and RuntimeTraceBus rotates its active trace.jsonl
    segment by size while keeping at most that many rotated segments.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, cast

import gpt.conversations as conversations
import gpt.transport.hybrid as hybrid
import gpt.utils.tracing as tracing
from gpt.debug import (
    DEFAULT_DEBUG_MAX_FILES,
    prune_debug_files,
    resolve_debug_max_files,
)
from gpt.transport.hybrid import CurlCffiSession
from gpt.utils.types import ResponseDelta

# ---------------------------------------------------------------------------
# (a) Hybrid event queue: drop-oldest past the cap
# ---------------------------------------------------------------------------


class _DummyTransport:
    async def close(self) -> None:
        return None


def _make_session(cap: int | None = None) -> CurlCffiSession:
    if cap is not None:
        os.environ["WEBGPT_HYBRID_EVENT_QUEUE_CAP"] = str(cap)
    try:
        return CurlCffiSession(cast(Any, _DummyTransport()))
    finally:
        if cap is not None:
            del os.environ["WEBGPT_HYBRID_EVENT_QUEUE_CAP"]


def test_events_queue_drop_oldest_keeps_fifo_order():
    session = _make_session(cap=4)
    assert session._events.maxsize == 4
    for i in range(10):
        session._emit(ResponseDelta(text=str(i), accumulated_text=str(i)))
    assert session._events.qsize() == 4
    drained = []
    while True:
        try:
            drained.append(session._events.get_nowait())
        except asyncio.QueueEmpty:
            break
    # The six oldest deltas were dropped; the newest survive in send order.
    assert [event.text for event in drained] == ["6", "7", "8", "9"]
    assert session._events_dropped == 6


def test_events_queue_close_sentinel_survives_full_queue():
    session = _make_session(cap=2)
    for _i in range(5):
        session._emit(ResponseDelta(text="d", accumulated_text="d"))
    asyncio.run(session.close())
    assert session._events.qsize() == 2
    drained = []
    while True:
        try:
            drained.append(session._events.get_nowait())
        except asyncio.QueueEmpty:
            break
    assert drained[-1] is None  # close sentinel always enqueued, never crashes


def test_event_queue_cap_env_resolution(monkeypatch):
    monkeypatch.delenv("WEBGPT_HYBRID_EVENT_QUEUE_CAP", raising=False)
    assert hybrid._resolve_event_queue_cap() == hybrid.EVENT_QUEUE_CAP_DEFAULT
    assert _make_session()._events.maxsize == hybrid.EVENT_QUEUE_CAP_DEFAULT
    for bad in ("abc", "0", "-5"):
        monkeypatch.setenv("WEBGPT_HYBRID_EVENT_QUEUE_CAP", bad)
        assert hybrid._resolve_event_queue_cap() == hybrid.EVENT_QUEUE_CAP_DEFAULT
    monkeypatch.setenv("WEBGPT_HYBRID_EVENT_QUEUE_CAP", "7")
    assert hybrid._resolve_event_queue_cap() == 7
    assert _make_session()._events.maxsize == 7


# ---------------------------------------------------------------------------
# (b) Canonical memo: env-tunable LRU bound with correct eviction order
# ---------------------------------------------------------------------------


def _memo_call_counter(monkeypatch):
    """Count real canonical_messages computations inside the memo module."""
    calls = {"n": 0}
    real = conversations.canonical_messages

    def counting(messages):
        calls["n"] += 1
        return real(messages)

    monkeypatch.setattr(conversations, "canonical_messages", counting)
    return calls


def _memo_lookup(text: str, model: str = "test-model"):
    return conversations._canonical_with_fingerprint(
        [{"role": "user", "content": text}], model, [], ""
    )


def test_canonical_memo_lru_eviction_and_hit_rate(monkeypatch):
    monkeypatch.setattr(conversations, "_CANONICAL_MEMO_MAX", 2)
    conversations._canonical_memo.clear()
    calls = _memo_call_counter(monkeypatch)

    canonical_a, fingerprint_a = _memo_lookup("a")
    assert calls["n"] == 1  # miss -> computed once

    # Hit path: no recompute and the returned list is an independent deep copy.
    copy_a, fingerprint_again = _memo_lookup("a")
    assert calls["n"] == 1
    assert fingerprint_again == fingerprint_a
    assert copy_a == canonical_a
    copy_a.append({"role": "mutant"})
    copy_a2, _ = _memo_lookup("a")
    assert copy_a2 == canonical_a  # mutation of the returned copy did not leak

    # Fill to capacity then overflow: oldest entry ("a") is evicted first.
    _memo_lookup("b")
    _memo_lookup("c")
    assert len(conversations._canonical_memo) == 2
    assert calls["n"] == 3

    # Touch "b" (refreshes recency), then evict via "d": "c" goes, "b" stays.
    _memo_lookup("b")
    assert calls["n"] == 3
    _memo_lookup("d")
    assert len(conversations._canonical_memo) == 2
    assert calls["n"] == 4

    _memo_lookup("b")  # refreshed entry survived -> still a cache hit
    assert calls["n"] == 4
    _memo_lookup("a")  # evicted earlier -> recomputed exactly once more
    assert calls["n"] == 5


def test_canonical_memo_cap_from_env(monkeypatch):
    monkeypatch.setattr(
        conversations, "_CANONICAL_MEMO_MAX", conversations.DEFAULT_CANONICAL_MEMO_MAX
    )
    conversations._canonical_memo.clear()
    monkeypatch.setenv("WEBGPT_CANONICAL_MEMO_MAX", "7")
    assert conversations._resolve_canonical_memo_max() == 7
    for bad in ("bogus", "0", "-1", ""):
        monkeypatch.setenv("WEBGPT_CANONICAL_MEMO_MAX", bad)
        assert (
            conversations._resolve_canonical_memo_max()
            == conversations.DEFAULT_CANONICAL_MEMO_MAX
        )
    monkeypatch.delenv("WEBGPT_CANONICAL_MEMO_MAX", raising=False)
    assert (
        conversations._resolve_canonical_memo_max()
        == conversations.DEFAULT_CANONICAL_MEMO_MAX
    )


# ---------------------------------------------------------------------------
# (c) Disk rotation: oldest debug files deleted first, cap respected
# ---------------------------------------------------------------------------


def _write_with_mtime(path, mtime: float) -> None:
    path.write_text("x", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_prune_debug_files_deletes_oldest_beyond_cap(tmp_path):
    base = time.time() - 10_000
    for i in range(7):
        _write_with_mtime(tmp_path / f"f{i:02d}.jsonl", base + i)
    keeper = tmp_path / "notes.txt"
    _write_with_mtime(keeper, base + 3)

    removed = prune_debug_files(tmp_path, max_files=5, patterns=("*.jsonl",))
    assert removed == 2
    survivors = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert survivors == [
        "f02.jsonl",
        "f03.jsonl",
        "f04.jsonl",
        "f05.jsonl",
        "f06.jsonl",
    ]
    assert keeper.exists()  # non-matching files are never touched
    # Idempotent once under the cap.
    assert prune_debug_files(tmp_path, max_files=5, patterns=("*.jsonl",)) == 0


def test_prune_debug_files_counts_per_file_not_per_dump(tmp_path):
    base = time.time() - 10_000
    # Two dumps = four files (.txt + .json each); cap 2 slots keeps one dump.
    # Distinct mtimes keep deletion order deterministic: the older dump's
    # pair goes first.
    _write_with_mtime(tmp_path / "000001_s_pre_gpt.txt", base + 0.1)
    _write_with_mtime(tmp_path / "000001_s_pre_gpt.json", base + 0.2)
    _write_with_mtime(tmp_path / "000002_s_correction.txt", base + 0.3)
    _write_with_mtime(tmp_path / "000002_s_correction.json", base + 0.4)

    removed = prune_debug_files(
        tmp_path, max_files=2, patterns=("*.txt", "*.json")
    )
    assert removed == 2
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["000002_s_correction.json", "000002_s_correction.txt"]


def test_prune_debug_files_edge_cases(tmp_path):
    assert prune_debug_files(None, max_files=5) == 0
    assert prune_debug_files(tmp_path, max_files=None) == 0
    assert prune_debug_files(tmp_path / "missing-dir", max_files=5) == 0
    assert prune_debug_files(tmp_path, max_files=-1) == 0


def test_resolve_debug_max_files(monkeypatch):
    monkeypatch.delenv("WEBGPT_DEBUG_MAX_FILES", raising=False)
    assert resolve_debug_max_files() == DEFAULT_DEBUG_MAX_FILES == 500
    for bad in ("bogus", "0", "-3"):
        monkeypatch.setenv("WEBGPT_DEBUG_MAX_FILES", bad)
        assert resolve_debug_max_files() == DEFAULT_DEBUG_MAX_FILES
    monkeypatch.setenv("WEBGPT_DEBUG_MAX_FILES", "42")
    assert resolve_debug_max_files() == 42


def test_trace_rotation_deletes_oldest_segments_respects_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBGPT_DEBUG_MAX_FILES", "3")
    monkeypatch.setattr(tracing, "_TRACE_ROTATE_BYTES", 256)
    bus = tracing.RuntimeTraceBus(output_path=tmp_path / "trace.jsonl")

    for i in range(40):
        bus.emit("ram_test", "tick", metadata={"i": i, "pad": "p" * 24})

    current = tmp_path / "trace.jsonl"
    assert current.is_file()
    segments = sorted(tmp_path.glob("trace.*.jsonl"))
    assert 1 <= len(segments) <= 3  # cap holds even after many rotations

    # Every retained segment is complete, valid JSONL.
    for segment in segments:
        for line in segment.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert record["component"] == "ram_test"

    # Surviving segments are the NEWEST rotations (largest sequence numbers).
    seqs = [int(path.name.split(".")[1]) for path in segments]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    # Segments pruned away are strictly older than everything retained.
    assert min(seqs) < max(seqs)

    # The active file ends with the most recent event, intact across rotation.
    last_line = current.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert json.loads(last_line)["metadata"]["i"] == 39
    assert bus.snapshot()[-1].metadata["i"] == 39
