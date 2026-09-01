"""P2 worker-affinity tests: conversation -> worker pinning in ChatGPTWorkerFactory.

Workers/pages are fully mocked (no browser, no network).
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from gpt.conversations import ConversationRecord
from gpt.gateway.runtime import CompletionRuntime
from gpt.state import SessionState
from gpt.tracing import RuntimeTraceBus
from gpt.transport.factory import ChatGPTWorkerFactory


class FakeManager:
    def __init__(self):
        self.start = AsyncMock()
        self.stop = AsyncMock()


class FakeSession:
    def __init__(self, name: str):
        self.name = name
        self.state = SessionState.READY
        self.close = AsyncMock()


@pytest.fixture
def factory_env(monkeypatch):
    monkeypatch.delenv("WEBGPT_WORKER_AFFINITY", raising=False)
    counter = {"n": 0}

    def make_create():
        async def create(**_kwargs):
            session = FakeSession(f"w{counter['n']}")
            counter["n"] += 1
            return session

        return create

    return make_create


def patch_create(monkeypatch, make_create):
    monkeypatch.setattr(
        "gpt.factory.ChatGPTWebSession.create", make_create(), raising=False
    )


async def test_same_affinity_key_returns_same_worker_when_idle(monkeypatch, factory_env):
    patch_create(monkeypatch, factory_env)
    factory = ChatGPTWorkerFactory(FakeManager(), max_workers=3, warm_workers=3)

    # Two turns of the same conversation back to back while the worker sits idle.
    worker_a, _session_a = await factory.acquire("conv_1")
    await factory.release(worker_a, affinity_key="conv_1")
    worker_b, _session_b = await factory.acquire("conv_1")
    assert worker_b == worker_a
    await factory.release(worker_b, affinity_key="conv_1")

    # Pin must beat plain LIFO: make the pinned worker *not* the LIFO head.
    id_c, _ = await factory.acquire("k2")
    id_b, _ = await factory.acquire("k1")
    await factory.release(id_c, affinity_key="k2")
    await factory.release(id_b, affinity_key="k1")  # idle order: [.., C, B]
    pinned, _ = await factory.acquire("k2")
    assert pinned == id_c  # LIFO would have returned B here
    await factory.close()


async def test_leased_pinned_worker_falls_through_without_blocking(
    monkeypatch, factory_env
):
    patch_create(monkeypatch, factory_env)
    factory = ChatGPTWorkerFactory(FakeManager(), max_workers=2, warm_workers=2)

    first_id, _first_session = await factory.acquire("conv_1")
    # Pinned worker is currently leased: acquire must pick another worker
    # immediately instead of waiting for it.
    second_id, _second_session = await asyncio.wait_for(
        factory.acquire("conv_1"), timeout=2.0
    )
    assert second_id != first_id
    await factory.release(second_id, affinity_key="conv_1")
    await factory.release(first_id, affinity_key="conv_1")

    stats = await factory.stats()
    assert stats.leased_workers == 0
    await factory.close()


async def test_lru_cap_evicts_oldest_entries(monkeypatch, factory_env):
    patch_create(monkeypatch, factory_env)
    # max_workers=1 -> affinity cap = 4.
    factory = ChatGPTWorkerFactory(FakeManager(), max_workers=1, warm_workers=1)

    for index in range(6):
        worker_id, _session = await factory.acquire(f"k{index}")
        await factory.release(worker_id, affinity_key=f"k{index}")

    assert len(factory._affinity_map) == 4
    assert "k0" not in factory._affinity_map
    assert "k1" not in factory._affinity_map
    assert set(factory._affinity_map) == {"k2", "k3", "k4", "k5"}
    await factory.close()


async def test_affinity_flag_off_restores_plain_lifo(monkeypatch, factory_env):
    monkeypatch.setenv("WEBGPT_WORKER_AFFINITY", "0")
    patch_create(monkeypatch, factory_env)
    factory = ChatGPTWorkerFactory(FakeManager(), max_workers=2, warm_workers=2)

    worker_id, _session = await factory.acquire("conv_1")
    await factory.release(worker_id, affinity_key="conv_1")
    # No mapping recorded when the rollback flag disables affinity.
    assert factory._affinity_map == {}
    again_id, _session = await factory.acquire("conv_1")
    assert isinstance(again_id, str)  # plain LIFO path still serves requests
    await factory.release(again_id, affinity_key="conv_1")
    assert factory._affinity_map == {}
    await factory.close()


class _NullConversations:
    """Placeholder store; _lease_for_record never touches it."""


async def _capture_lease(**_kwargs):
    captured: dict = {}

    @asynccontextmanager
    async def lease(record=None, *, affinity_key=None):
        captured["record"] = record
        captured["affinity_key"] = affinity_key
        yield object()

    return lease, captured


async def test_runtime_passes_affinity_key_from_record():
    lease, captured = await _capture_lease()
    runtime = CompletionRuntime(_NullConversations(), lease)
    record = ConversationRecord(session_id="sess_1", conversation_id="conv_42")

    async with runtime._lease_for_record(record):
        pass

    assert captured["affinity_key"] == "conv_42"
    assert captured["record"] is record

    record_no_conversation = ConversationRecord(session_id="sess_2")
    async with runtime._lease_for_record(record_no_conversation):
        pass
    assert captured["affinity_key"] == "sess_2"


async def test_runtime_legacy_lease_signature_gets_no_affinity_key():
    seen: dict = {}

    @asynccontextmanager
    async def legacy_lease(record=None):
        seen["args"] = (record,)
        seen["kwargs"] = None
        yield object()

    runtime = CompletionRuntime(_NullConversations(), legacy_lease)
    record = ConversationRecord(session_id="sess_3", conversation_id="conv_7")

    async with runtime._lease_for_record(record):
        pass

    assert seen["args"] == (record,)


async def test_position_session_emits_affinity_hit_event():
    bus = RuntimeTraceBus()

    class FakePositionSession:
        def __init__(self, conversation_id=None):
            self.conversation_id = conversation_id
            self.open = AsyncMock()
            self.new_conversation = AsyncMock()
            self.select_model = AsyncMock()
            self.select_reasoning_effort = AsyncMock()

    runtime = CompletionRuntime(_NullConversations(), lambda: None, trace=bus)

    # Hit: worker already on this conversation -> no open(), event true.
    session = FakePositionSession(conversation_id="conv_9")
    record = ConversationRecord(session_id="s1", conversation_id="conv_9")
    await runtime.position_session(session, record, ui_model=None)
    session.open.assert_not_awaited()
    events = [
        event for event in bus.snapshot() if event.kind == "position_skipped_affinity_hit"
    ]
    assert len(events) == 1
    assert events[0].metadata["position_skipped_affinity_hit"] is True

    # Miss: different worker -> open() runs, event false.
    bus.clear()
    session_miss = FakePositionSession(conversation_id=None)
    await runtime.position_session(session_miss, record, ui_model=None)
    session_miss.open.assert_awaited_once_with("conv_9")
    events = [
        event for event in bus.snapshot() if event.kind == "position_skipped_affinity_hit"
    ]
    assert len(events) == 1
    assert events[0].metadata["position_skipped_affinity_hit"] is False
