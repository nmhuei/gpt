"""Performance quick-win gates (master roadmap 2026-08-24, track 2).

Covers:
- P1: env-tunable poll_interval / stable_grace in the UI driver.
- P3: async persistence path (persist_async), TTL eviction at commit/resolve,
      and the hybrid session history cap.
"""

import json
import time
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from gpt.conversations import ConversationStore
from gpt.drivers.ui import DEFAULT_POLL_INTERVAL, DEFAULT_STABLE_GRACE, UIDriver
from gpt.transport.hybrid import HISTORY_MAXLEN, CurlCffiSession
from gpt.types import Turn


def _fake_page() -> MagicMock:
    page = AsyncMock()
    page.url = "https://chatgpt.com/"
    return page


# ---------------------------------------------------------------------------
# (a) env poll / stable_grace
# ---------------------------------------------------------------------------


def test_poll_defaults_without_env(monkeypatch):
    monkeypatch.delenv("WEBGPT_POLL_INTERVAL", raising=False)
    monkeypatch.delenv("WEBGPT_STABLE_GRACE", raising=False)
    driver = UIDriver(_fake_page())
    assert driver.poll_interval == DEFAULT_POLL_INTERVAL == 0.12
    assert driver.stable_grace == DEFAULT_STABLE_GRACE == 0.45


def test_poll_values_read_from_env(monkeypatch):
    monkeypatch.setenv("WEBGPT_POLL_INTERVAL", "0.33")
    monkeypatch.setenv("WEBGPT_STABLE_GRACE", "1.7")
    driver = UIDriver(_fake_page())
    assert driver.poll_interval == pytest.approx(0.33)
    assert driver.stable_grace == pytest.approx(1.7)


def test_explicit_arguments_override_env(monkeypatch):
    monkeypatch.setenv("WEBGPT_POLL_INTERVAL", "0.33")
    monkeypatch.setenv("WEBGPT_STABLE_GRACE", "1.7")
    driver = UIDriver(_fake_page(), poll_interval=0.5, stable_grace=2.0)
    assert driver.poll_interval == 0.5
    assert driver.stable_grace == 2.0


def test_invalid_env_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("WEBGPT_POLL_INTERVAL", "not-a-number")
    monkeypatch.setenv("WEBGPT_STABLE_GRACE", "")
    driver = UIDriver(_fake_page())
    assert driver.poll_interval == 0.12
    assert driver.stable_grace == 0.45


# ---------------------------------------------------------------------------
# (b) persist_async matches _persist
# ---------------------------------------------------------------------------


def _populate(store: ConversationStore) -> None:
    record, _pending, cached = store.resolve(
        [{"role": "user", "content": "hello"}], model="m1", tools=[]
    )
    assert not cached
    store.commit(
        record,
        [{"role": "user", "content": "hello"}],
        {"role": "assistant", "content": "hi there"},
        response={"id": "resp_1"},
        model="m1",
        tools=[{"type": "function", "function": {"name": "f"}}],
        conversation_id="conv_123",
    )


@pytest.mark.anyio
async def test_persist_async_writes_same_payload_as_persist(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("WEBGPT_SYNC_PERSIST", raising=False)
    sync_store = ConversationStore(state_path=tmp_path / "sync.json")
    async_store = ConversationStore(state_path=tmp_path / "async.json")
    _populate(sync_store)
    _populate(async_store)

    sync_store._persist()
    await async_store.persist_async()

    sync_payload = json.loads((tmp_path / "sync.json").read_text(encoding="utf-8"))
    async_payload = json.loads((tmp_path / "async.json").read_text(encoding="utf-8"))
    assert sync_payload["version"] == async_payload["version"]
    sync_records = sync_payload["records"]
    async_records = async_payload["records"]
    for entry in sync_records + async_records:
        entry.pop("saved_at")  # wall-clock write timestamp is expected to differ
        entry["last_used"] = int(entry["last_used"])  # tolerate second boundaries
    assert len(sync_records) == len(async_records) == 1
    async_records[0]["session_id"] = sync_records[0]["session_id"]
    assert sync_records == async_records


@pytest.mark.anyio
async def test_persist_async_respects_sync_env_flag(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WEBGPT_SYNC_PERSIST", "1")
    store = ConversationStore(state_path=tmp_path / "flagged.json")
    _populate(store)
    await store.persist_async()
    payload = json.loads((tmp_path / "flagged.json").read_text(encoding="utf-8"))
    assert len(payload["records"]) == 1
    assert payload["records"][0]["conversation_id"] == "conv_123"


@pytest.mark.anyio
async def test_persist_async_noop_without_state_path():
    store = ConversationStore(state_path=None)  # must not raise
    await store.persist_async()


# ---------------------------------------------------------------------------
# (c) TTL eviction at commit / resolve
# ---------------------------------------------------------------------------


def test_ttl_eviction_on_resolve_removes_stale_record():
    store = ConversationStore(ttl_seconds=50)
    record, _, _ = store.resolve([{"role": "user", "content": "old"}], model="m1", tools=[])
    record.last_used = time.time() - 100  # older than ttl_seconds

    # A non-matching resolve triggers the TTL sweep; the stale record goes away.
    fresh_record, _, _ = store.resolve([{"role": "user", "content": "new"}], model="m1", tools=[])
    assert store.get(record.session_id) is None
    assert store.get(fresh_record.session_id) is not None


def test_ttl_eviction_on_commit_keeps_live_records():
    store = ConversationStore(ttl_seconds=3600)
    live, _, _ = store.resolve([{"role": "user", "content": "live"}], model="m1", tools=[])
    stale, _, _ = store.resolve([{"role": "user", "content": "stale"}], model="m1", tools=[])
    stale.last_used = time.time() - 7200

    store.commit(
        live,
        [{"role": "user", "content": "live"}],
        {"role": "assistant", "content": "ok"},
        response={"id": "r"},
        model="m1",
        tools=[],
        conversation_id="conv_x",
    )

    assert store.get(stale.session_id) is None
    assert store.get(live.session_id) is not None


def test_ttl_eviction_keeps_records_within_ttl():
    store = ConversationStore(ttl_seconds=3600)
    record, _, _ = store.resolve([{"role": "user", "content": "recent"}], model="m1", tools=[])
    record.last_used = time.time() - 60

    store.resolve([{"role": "user", "content": "unrelated"}], model="m1", tools=[])
    assert store.get(record.session_id) is not None


# ---------------------------------------------------------------------------
# (d) hybrid _history cap
# ---------------------------------------------------------------------------


def _turn(role: Literal["user", "assistant", "system"], text: str) -> Turn:
    return Turn(turn_id=f"{role}_{text}", role=role, text=text)


def test_history_deque_capped_after_many_fake_turns():
    session = CurlCffiSession(transport=MagicMock())
    pair = [_turn("user", "u"), _turn("assistant", "a")]
    for _ in range(30):  # 60 fake turns pushed through extend()
        session._history.extend(pair)
    assert len(session._history) == HISTORY_MAXLEN == 40
    # Oldest entries are dropped: only the most recent turns remain.
    assert list(session._history)[-2] == pair[0]
    assert list(session._history)[-1] == pair[1]


@pytest.mark.anyio
async def test_history_cap_via_real_send_loop():
    class FakeTransport:
        async def send(self, request, on_delta=None):
            if on_delta is not None:
                result = on_delta("ok", request.text)
                if hasattr(result, "__await__"):
                    await result
            from gpt.types import TurnResult

            return TurnResult(
                turn_id="t",
                conversation_id="c1",
                text=request.text,
                model=None,
            )

        async def close(self) -> None:
            return None

    session = CurlCffiSession(transport=FakeTransport())
    for i in range(25):  # 50 turns > cap
        await session.send(f"prompt {i}")
    assert len(session._history) == HISTORY_MAXLEN
