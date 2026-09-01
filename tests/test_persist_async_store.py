"""Wave-3 gate tests: async-safe persistence in ConversationStore.

Design under test:
- Default (no env): mutations called inside a running event loop never write
  disk synchronously; they set the dirty flag and a coalescing background
  worker thread performs the flush off-loop.
- ``mark_pending`` is the durability exception: it always writes synchronously
  before returning so the two-phase crash-recovery marker is on disk before
  the request goes to the web backend.
- ``WEBGPT_SYNC_PERSIST=1`` restores the legacy fully-synchronous behavior.
- ``close()`` performs a final synchronous flush of any dirty state.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from gpt.conversations import ConversationStore


def _commit_once(store: ConversationStore, record, text: str) -> None:
    messages = [{"role": "user", "content": text}]
    store.commit(
        record,
        messages,
        {"role": "assistant", "content": f"re: {text}"},
        {"id": f"resp_{text}"},
        "chatgpt-web",
        [],
        f"conv_{text}",
    )


def _read_state(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def _clear_sync_env(monkeypatch):
    monkeypatch.delenv("WEBGPT_SYNC_PERSIST", raising=False)


def _drain(store: ConversationStore, timeout: float = 5.0) -> None:
    """Wait until no background flush is active and the dirty flag is clear."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not store._dirty and not store._flush_active:
            # One extra beat: let the worker actually finish its final write.
            time.sleep(0.05)
            if not store._dirty and not store._flush_active:
                return
        time.sleep(0.01)
    raise AssertionError("background flush did not settle in time")


def test_default_commit_does_not_write_disk_in_event_loop_thread(tmp_path, monkeypatch):
    """(a) Inside a loop, commit only dirties + schedules; disk writes happen off-loop."""
    import threading

    state_path = tmp_path / "state.json"
    store = ConversationStore(state_path=state_path)
    writer_threads: list[threading.Thread] = []
    original = ConversationStore._write_state_locked

    def spy(self):
        writer_threads.append(threading.current_thread())
        return original(self)

    monkeypatch.setattr(store, "_write_state_locked", spy.__get__(store, ConversationStore))

    async def scenario():
        record, _, _ = store.resolve(
            [{"role": "user", "content": "hi"}], "chatgpt-web", []
        )
        loop_thread = threading.current_thread()
        _commit_once(store, record, "m1")
        # The mutation itself must not have hit disk from the loop thread.
        assert all(t is not loop_thread for t in writer_threads), (
            "default mode must not persist synchronously on the event-loop thread"
        )
        await asyncio.sleep(0.3)  # give the background worker time to flush
        # The background worker did the write instead.
        assert writer_threads, "dirty state must eventually be flushed"
        assert all(t is not loop_thread for t in writer_threads)
        return record

    record = asyncio.run(scenario())
    _drain(store)
    payload = _read_state(state_path)
    assert any(r["session_id"] == record.session_id for r in payload["records"])


def test_mark_pending_is_durable_before_return(tmp_path, _clear_sync_env):
    """(b) mark_pending writes synchronously even in default mode: crash-recovery
    requires the pending marker on disk before the web request is submitted."""
    state_path = tmp_path / "pending.json"
    store = ConversationStore(state_path=state_path)
    messages = [{"role": "user", "content": "in flight"}]

    async def scenario():
        record, _, _ = store.resolve(messages, "chatgpt-web", [])
        fingerprint = store.mark_pending(
            record,
            messages=messages,
            model="chatgpt-web",
            tools=[],
            tool_choice="auto",
            prompt="in flight",
        )
        # Immediately after mark_pending returns — with zero yields to the
        # background flusher — the marker must already be durable on disk.
        payload = _read_state(state_path)
        loaded = next(
            r for r in payload["records"] if r["session_id"] == record.session_id
        )
        assert loaded["pending_request_fingerprint"] == fingerprint
        assert loaded["pending_prompt"] == "in flight"
        return fingerprint

    fingerprint = asyncio.run(scenario())
    restored = ConversationStore(state_path=state_path)
    loaded = next(iter(restored._records.values()))
    assert loaded is not None
    assert loaded.pending_request_fingerprint == fingerprint


def test_close_flushes_dirty_state_synchronously(tmp_path, _clear_sync_env):
    """(c) close() performs a final synchronous flush of everything dirty."""
    state_path = tmp_path / "close.json"
    store = ConversationStore(state_path=state_path)

    async def scenario():
        record, _, _ = store.resolve([{"role": "user", "content": "x"}], "chatgpt-web", [])
        _commit_once(store, record, "final")
        # Return immediately without awaiting any flush.
        return record

    record = asyncio.run(scenario())
    store.close()
    assert not store._dirty
    payload = _read_state(state_path)
    loaded = next(r for r in payload["records"] if r["session_id"] == record.session_id)
    assert loaded["last_response"] == {"id": "resp_final"}


def test_sync_persist_env_restores_legacy_behavior(tmp_path, monkeypatch):
    """(d) WEBGPT_SYNC_PERSIST=1 keeps the old synchronous in-loop write."""
    monkeypatch.setenv("WEBGPT_SYNC_PERSIST", "1")
    state_path = tmp_path / "sync.json"
    store = ConversationStore(state_path=state_path)

    async def scenario():
        record, _, _ = store.resolve(
            [{"role": "user", "content": "legacy"}], "chatgpt-web", []
        )
        _commit_once(store, record, "sync")
        # With sync mode, the write completed before commit returned.
        assert state_path.exists()
        payload = _read_state(state_path)
        assert any(
            r["session_id"] == record.session_id for r in payload["records"]
        )
        return record

    asyncio.run(scenario())


def test_burst_commits_do_not_block_event_loop(tmp_path, _clear_sync_env):
    """(e) 500 consecutive commits must never stall the loop >50ms per await."""
    state_path = tmp_path / "burst.json"
    store = ConversationStore(state_path=state_path, max_sessions=512)

    async def scenario():
        record, _, _ = store.resolve([{"role": "user", "content": "0"}], "chatgpt-web", [])
        worst = 0.0
        heartbeat = {"i": 0}
        for i in range(500):
            start = time.perf_counter()
            _commit_once(store, record, f"m{i}")
            # Simulate an interleaved await: the loop must stay responsive.
            await asyncio.sleep(0)
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            worst = max(worst, elapsed_ms)
            heartbeat["i"] = i
        return worst, heartbeat

    worst, heartbeat = asyncio.run(scenario())
    assert heartbeat["i"] == 499
    assert worst < 50.0, f"event loop blocked {worst:.1f}ms during burst commits"
    _drain(store)
