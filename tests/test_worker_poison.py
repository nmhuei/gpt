"""Worker-poisoning regression tests (verify-fromscratch 2026-08-25, ĐỨT #1/#2).

Scenario: an upstream live-SSE deadline expiry or client disconnect cancels the
completion task mid-``session.send``. CancelledError is a BaseException, so
historically it sailed past every ``except Exception`` handler, wedged the
session state machine in SENDING/WAITING_RESPONSE/GENERATING forever, and the
factory handed the poisoned worker back out -- every later turn on it failed
with "Cannot send while session is GENERATING".

Covers:
(a) cancel mid-send -> session reaches a terminal state, CancelledError still
    propagates (never swallowed);
(b) a cancelled worker is never repooled; the next turn gets a fresh worker;
(c) the derived stream deadline covers the correction-loop worst case.
"""

import asyncio
import time
import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gpt.debug import apply_stream_deadline_floor
from gpt.factory import ChatGPTWorkerFactory
from gpt.gateway.runtime import derived_stream_deadline_seconds
from gpt.session import ChatGPTWebSession
from gpt.state import SessionState
from gpt.transport.factory import _Worker
from gpt.types import (
    ResponseCompleted,
    ResponseDelta,
    ResponseStarted,
    TurnResult,
)

TERMINAL_STATES = {SessionState.FATAL_ERROR, SessionState.CLOSED}


async def wait_for_state(session: ChatGPTWebSession, target: SessionState) -> None:
    """Pump the loop until the state machine settles on ``target``."""
    for _ in range(200):
        if session.state == target:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"session never reached {target.value}; state={session.state.value}"
    )


class FakeManager:
    connected = True

    def __init__(self):
        self.stop = AsyncMock()

    async def new_page(self):  # pragma: no cover - page recovery not expected
        raise AssertionError("page recovery was not expected")


def make_page():
    page = MagicMock()
    page.url = "https://chatgpt.com/"
    page.is_closed.return_value = False
    page.close = AsyncMock()
    return page


class HangingUI:
    """UI driver whose send reaches GENERATING and then hangs forever."""

    def __init__(self):
        self.entered = asyncio.Event()

    async def send(self, request, event_callback=None):
        self.entered.set()
        await event_callback(ResponseStarted(turn_id="assistant-hang"))
        await event_callback(ResponseDelta(text="par", accumulated_text="par"))
        await asyncio.Event().wait()  # cancelled here in tests
        raise AssertionError("unreachable")  # pragma: no cover

    def conversation_id(self):
        return "conv-hang"

    async def history(self):
        return []


class SuccessUI:
    """UI driver completing a normal turn (healthy-reuse regression guard)."""

    def __init__(self):
        self.sent = 0

    async def send(self, request, event_callback=None):
        self.sent += 1
        await event_callback(ResponseStarted(turn_id=f"assistant-{self.sent}"))
        await event_callback(
            ResponseCompleted(
                turn_id=f"assistant-{self.sent}",
                text="done",
                conversation_id="conv-ok",
            )
        )
        return TurnResult(
            turn_id=f"assistant-{self.sent}", conversation_id="conv-ok", text="done"
        )

    def conversation_id(self):
        return "conv-ok"

    async def history(self):
        return []


def make_session(ui) -> ChatGPTWebSession:
    session = ChatGPTWebSession(cast(Any, FakeManager()), make_page())
    session.ui_driver = ui
    return session


def stub_worker_factory(session, *, max_workers=1, warm_workers=1) -> ChatGPTWorkerFactory:
    """Factory whose created workers wrap the given pre-built session object."""

    async def patched_new_worker():
        worker = _Worker(
            worker_id=f"worker_{uuid.uuid4().hex[:12]}",
            session=session,
            created_at=time.monotonic(),
            last_used=time.monotonic(),
        )
        async with factory._lock:
            factory._all[worker.worker_id] = worker
            factory._created_workers += 1
        return worker

    factory = ChatGPTWorkerFactory(
        cast(Any, FakeManager()), max_workers=max_workers, warm_workers=warm_workers
    )
    factory._new_worker = patched_new_worker  # type: ignore[method-assign]
    return factory


# ---------------------------------------------------------------------------
# (a) Session-level: cancellation must not wedge the state machine
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cancel_mid_send_moves_session_to_terminal_state_and_reraises():
    ui = HangingUI()
    session = make_session(ui)
    await session.state_machine.transition_to(SessionState.READY)
    session.drain_events()

    task = asyncio.create_task(session.send("hi"))
    await asyncio.wait_for(ui.entered.wait(), timeout=5)
    await wait_for_state(session, SessionState.GENERATING)  # wedged precondition
    task.cancel()

    # Cancellation propagates: cleanup must never swallow it.
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.state in TERMINAL_STATES
    assert session.state == SessionState.FATAL_ERROR
    # No partial commit: history must not contain the aborted user turn.
    assert await session.history() == []
    events = session.drain_events()
    assert any(type(event).__name__ == "ResponseFailed" for event in events)


@pytest.mark.anyio
async def test_cancel_before_driver_send_still_reaches_terminal_state():
    ui = HangingUI()
    session = make_session(ui)
    await session.state_machine.transition_to(SessionState.READY)

    task = asyncio.create_task(session.send("hi"))
    await asyncio.wait_for(ui.entered.wait(), timeout=5)
    # Cancel from outside while the driver await is parked (same code path as
    # an SSE deadline firing): terminal state regardless of where it landed.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.state in TERMINAL_STATES


# ---------------------------------------------------------------------------
# (b) Factory-level: a cancelled/poisoned worker is closed, never repooled
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cancelled_turn_closes_worker_and_next_turn_gets_fresh_one(monkeypatch):
    """End-to-end poison scenario through factory.lease + real session.send."""
    corpse = make_session(HangingUI())
    fresh = make_session(SuccessUI())
    await corpse.state_machine.transition_to(SessionState.READY)
    await fresh.state_machine.transition_to(SessionState.READY)
    created = [corpse, fresh]

    async def fake_create(**_kwargs):
        if not created:
            raise AssertionError("factory unexpectedly needed a third worker")
        return created.pop(0)

    monkeypatch.setattr(
        "gpt.transport.factory.ChatGPTWebSession.create", fake_create
    )
    factory = ChatGPTWorkerFactory(FakeManager(), max_workers=2, warm_workers=1)
    entered = asyncio.Event()

    async def work():
        # Single task owns the lease AND the send, exactly like the gateway's
        # completion task cancelled by the live-SSE deadline.
        async with factory.lease() as session:
            entered.set()
            assert session is corpse
            await session.send("hi")  # cancelled mid-generation below

    task = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), timeout=5)
    await wait_for_state(corpse, SessionState.GENERATING)

    # Upstream deadline/disconnect cancels the whole turn task.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The poisoned worker reached a terminal state and was NOT repooled.
    assert corpse.state in TERMINAL_STATES
    stats = await factory.stats()
    assert stats.idle_workers == 0
    assert stats.closed_workers == 1

    # Next turn leases a different, healthy worker: no zombie reuse.
    async with factory.lease() as session_two:
        assert session_two is fresh
        assert session_two.state == SessionState.READY
    # Healthy releases are still pooled -- the fix did not disable warm reuse.
    stats = await factory.stats()
    assert stats.idle_workers == 1
    await factory.close()


@pytest.mark.anyio
async def test_release_with_nonterminal_mid_send_state_is_not_repooled():
    stub = SimpleNamespace(state=SessionState.WAITING_RESPONSE, close=AsyncMock())
    factory = stub_worker_factory(stub)

    worker_id, leased = await factory.acquire()
    assert leased is stub
    leased.state = SessionState.WAITING_RESPONSE  # interrupted mid-turn
    await factory.release(worker_id)

    stats = await factory.stats()
    assert stats.idle_workers == 0
    assert stats.closed_workers == 1
    stub.close.assert_awaited_once()
    await factory.close()


@pytest.mark.anyio
async def test_error_exit_with_ready_state_is_still_reusable():
    """Whitelist semantics: READY survives an error exit (transient failures).

    Guards against overcorrecting: only interrupted/fatal workers are excluded,
    ordinary retryable turns still reuse the warm worker.
    """
    stub = SimpleNamespace(state=SessionState.READY, close=AsyncMock())
    factory = stub_worker_factory(stub)

    with pytest.raises(RuntimeError):
        async with factory.lease():
            raise RuntimeError("transient client-side failure")

    stats = await factory.stats()
    assert stats.idle_workers == 1
    assert stats.closed_workers == 0
    await factory.close()


@pytest.mark.anyio
async def test_fatal_state_on_success_exit_is_not_repooled():
    stub = SimpleNamespace(state=SessionState.FATAL_ERROR, close=AsyncMock())
    factory = stub_worker_factory(stub)

    async with factory.lease():
        pass  # body succeeded but the session died underneath

    stats = await factory.stats()
    assert stats.idle_workers == 0
    assert stats.closed_workers == 1
    await factory.close()


# ---------------------------------------------------------------------------
# (c) Stream deadline vs correction loop
# ---------------------------------------------------------------------------


def test_derived_deadline_covers_correction_worst_case_default_config():
    deadline = derived_stream_deadline_seconds(
        queue_timeout=180.0, generation_timeout=120.0, max_corrections=2
    )
    worst_case_sends = 180.0 + (1 + 2) * 120.0
    assert deadline >= worst_case_sends
    assert deadline >= 180.0 + 2 * 120.0 + 30.0  # budget x per-send timeout + margin
    assert deadline == pytest.approx(570.0)


def test_derived_deadline_beats_old_default_that_raced_corrections():
    deadline = derived_stream_deadline_seconds(
        queue_timeout=180.0, generation_timeout=120.0, max_corrections=4
    )
    old_default = 180.0 + 120.0 + 30.0  # 330s: fired mid correction loop
    assert deadline > old_default
    assert deadline >= 180.0 + (1 + 4) * 120.0
    assert deadline == pytest.approx(810.0)


def test_derived_deadline_invariant_holds_across_configs():
    for queue in (30.0, 180.0, 600.0):
        for generation in (60.0, 120.0, 300.0):
            for corrections in (0, 1, 2, 4, 8):
                deadline = derived_stream_deadline_seconds(
                    queue_timeout=queue,
                    generation_timeout=generation,
                    max_corrections=corrections,
                )
                assert deadline >= corrections * generation + 30.0
                assert deadline >= queue + (1 + corrections) * generation + 30.0


def test_apply_stream_deadline_floor_raises_unsafe_default(monkeypatch):
    monkeypatch.delenv("WEBGPT_STREAM_DEADLINE_SECONDS", raising=False)
    server = SimpleNamespace(
        queue_timeout=180.0,
        stream_deadline_seconds=330.0,
        completion_runtime=SimpleNamespace(
            generation_timeout_seconds=120.0, max_corrections=4
        ),
    )
    raised = apply_stream_deadline_floor(server)
    assert raised == pytest.approx(810.0)
    assert server.stream_deadline_seconds == pytest.approx(810.0)


def test_apply_stream_deadline_floor_respects_env_override(monkeypatch):
    """WEBGPT_STREAM_DEADLINE_SECONDS keeps its verbatim override behavior."""
    monkeypatch.setenv("WEBGPT_STREAM_DEADLINE_SECONDS", "400")
    server = SimpleNamespace(
        queue_timeout=180.0,
        stream_deadline_seconds=330.0,
        completion_runtime=SimpleNamespace(
            generation_timeout_seconds=120.0, max_corrections=4
        ),
    )
    assert apply_stream_deadline_floor(server) is None
    assert server.stream_deadline_seconds == pytest.approx(330.0)


def test_apply_stream_deadline_floor_noop_when_already_safe(monkeypatch):
    monkeypatch.delenv("WEBGPT_STREAM_DEADLINE_SECONDS", raising=False)
    server = SimpleNamespace(
        queue_timeout=180.0,
        stream_deadline_seconds=900.0,
        completion_runtime=SimpleNamespace(
            generation_timeout_seconds=120.0, max_corrections=2
        ),
    )
    assert apply_stream_deadline_floor(server) is None
    assert server.stream_deadline_seconds == pytest.approx(900.0)
