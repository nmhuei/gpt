"""Session and pool adapters that let the existing runtime use curl transport."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gpt.state import CommitUnknown, SessionState
from gpt.transport.browser import BrowserManager
from gpt.transport.curl_transport import CurlCffiTransport
from gpt.transport.factory import WorkerFactoryStats, WorkerQueueTimeout
from gpt.transport.token_manager import TokenManager
from gpt.types import (
    ModelInfo,
    ReconciliationResult,
    ResponseCompleted,
    ResponseDelta,
    ResponseFailed,
    ResponseStarted,
    SendRequest,
    SessionEvent,
    SessionInfo,
    Turn,
    TurnResult,
)

logger = logging.getLogger("gpt.transport.hybrid")

# Cap on the in-memory turn history kept per curl session (P3 leak guard):
# 2 turns per send, so 40 turns == 20 round-trips of context.
HISTORY_MAXLEN = 40

# Cap on the live event queue (RAM-TOP5 guard): when no stream_callback
# consumes events (the common tool-call turn path), a warm worker reused for
# the life of the process would otherwise buffer every ResponseDelta forever.
# Overflow drops the OLDEST buffered event, keeps FIFO order and counts drops.
EVENT_QUEUE_CAP_DEFAULT = 512


def _resolve_event_queue_cap() -> int:
    """Resolve ``WEBGPT_HYBRID_EVENT_QUEUE_CAP``; invalid values keep default."""
    raw = os.environ.get("WEBGPT_HYBRID_EVENT_QUEUE_CAP", "").strip()
    if not raw:
        return EVENT_QUEUE_CAP_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return EVENT_QUEUE_CAP_DEFAULT
    return value if value >= 1 else EVENT_QUEUE_CAP_DEFAULT


class CurlCffiSession:
    """Small session-compatible wrapper around one ``CurlCffiTransport``."""

    def __init__(self, transport: CurlCffiTransport) -> None:
        self.transport = transport
        self.session_id = f"hy_{uuid.uuid4().hex[:10]}"
        self._conversation_id: str | None = None
        self._model: ModelInfo | None = None
        self._reasoning_effort: str | None = None
        self._history: deque[Turn] = deque(maxlen=HISTORY_MAXLEN)
        self._state = SessionState.READY
        self._events: asyncio.Queue[SessionEvent | None] = asyncio.Queue(
            maxsize=_resolve_event_queue_cap()
        )
        self._events_dropped = 0
        self._event_history: list[SessionEvent] = []
        self._created_at = datetime.now(timezone.utc).isoformat()
        self._last_used_at = self._created_at

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

    async def new_conversation(self, model: str | None = None) -> SessionInfo:
        self._conversation_id = None
        self._history.clear()
        if model:
            await self.select_model(model)
        return self.get_info()

    async def open(self, conversation_id: str) -> SessionInfo:
        self._conversation_id = conversation_id
        return self.get_info()

    async def select_model(self, model: str) -> None:
        self._model = ModelInfo(id=model, label=model, source="protocol")

    async def select_reasoning_effort(self, effort: str) -> None:
        self._reasoning_effort = effort

    async def models(self) -> list[ModelInfo]:
        return [self._model] if self._model else []

    async def send(
        self,
        text: str,
        timeout_seconds: float = 120,
        model: str | None = None,
        reasoning_effort: str | None = None,
        files: tuple[str, ...] | None = None,
    ) -> TurnResult:
        if not text.strip():
            raise ValueError("Prompt text cannot be empty.")
        if files:
            raise ValueError("Hybrid transport does not support file attachments.")
        if model:
            await self.select_model(model)
        if reasoning_effort:
            await self.select_reasoning_effort(reasoning_effort)
        self._state = SessionState.SENDING
        emitted_text = ""
        provisional_turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        self._emit(ResponseStarted(turn_id=provisional_turn_id, model=self._model_label()))

        async def on_delta(delta: str, turn_id: str) -> None:
            nonlocal emitted_text
            emitted_text += delta
            self._emit(ResponseDelta(text=delta, accumulated_text=emitted_text))

        try:
            result = await self.transport.send(
                SendRequest(
                    text=text,
                    conversation_id=self._conversation_id,
                    model=self._model,
                    reasoning_effort=self._reasoning_effort,
                    timeout_seconds=timeout_seconds,
                ),
                on_delta=on_delta,
            )
        except Exception as exc:
            self._state = SessionState.RETRYABLE_ERROR
            self._emit(ResponseFailed(turn_id=provisional_turn_id, reason=str(exc), partial_text=emitted_text))
            raise
        self._conversation_id = result.conversation_id or self._conversation_id
        self._history.extend(
            [
                Turn(turn_id=f"user_{uuid.uuid4().hex[:10]}", role="user", text=text),
                Turn(turn_id=result.turn_id, role="assistant", text=result.text, model=result.model),
            ]
        )
        self._last_used_at = datetime.now(timezone.utc).isoformat()
        self._state = SessionState.READY
        self._emit(
            ResponseCompleted(
                turn_id=result.turn_id,
                text=result.text,
                model=result.model,
                conversation_id=result.conversation_id,
            )
        )
        return result

    def _emit(self, event: SessionEvent) -> None:
        self._event_history.append(event)
        self._enqueue(event)

    def _enqueue(self, event: SessionEvent | None) -> None:
        """Queue an event, dropping the oldest buffered one on overflow.

        Runs without await points, so the drop-oldest swap is atomic within
        the event loop: after ``get_nowait`` frees a slot the re-put cannot
        hit ``QueueFull`` again.
        """
        try:
            self._events.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        try:
            self._events.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self._events_dropped += 1
        if self._events_dropped == 1 or self._events_dropped % 500 == 0:
            logger.warning(
                "hybrid_event_queue_overflow dropped=%d cap=%d",
                self._events_dropped,
                self._events.maxsize,
            )
        self._events.put_nowait(event)

    def _model_label(self) -> str | None:
        return self._model.label if self._model else None

    async def events(self) -> AsyncIterator[SessionEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    def drain_events(self) -> list[SessionEvent]:
        events = self._event_history
        self._event_history = []
        return events

    async def reconcile(self, expected_user_text: str) -> ReconciliationResult:
        # Direct HTTP has no authoritative history endpoint.  Never claim that
        # an uncertain submission is absent, as that would permit an unsafe resend.
        raise CommitUnknown(
            "Hybrid transport cannot reconcile an uncertain request without browser history.",
            conversation_id=self._conversation_id,
        )

    async def close(self) -> None:
        self._state = SessionState.CLOSED
        self._enqueue(None)
        await self.transport.close()

    def get_info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            conversation_id=self._conversation_id,
            conversation_url=None,
            model=self._model,
            state=self._state.value,
            created_at=self._created_at,
            last_used_at=self._last_used_at,
        )


class HybridWorkerFactory:
    """One browser token page shared by a bounded pool of curl sessions."""

    def __init__(
        self,
        browser_manager: BrowserManager,
        *,
        max_workers: int = 1,
        warm_workers: int = 1,
        queue_timeout: float = 30.0,
        target_url: str = "https://chatgpt.com",
        auto_login: Any | None = None,
        allow_local_mock: bool | None = None,
        rate_limit_breaker: Any | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if not 0 <= warm_workers <= max_workers:
            raise ValueError("warm_workers must be between 0 and max_workers")
        if queue_timeout <= 0:
            raise ValueError("queue_timeout must be positive")
        self.browser_manager = browser_manager
        self.max_workers = max_workers
        # POOL-PER-ACCT-BREAKER (row S): an explicitly injected breaker gates
        # every acquire() exactly like ChatGPTWorkerFactory -- fail fast with
        # BackendCoolingDown while that account's cooldown window is open, one
        # half-open probe after it elapses. When no breaker is injected the
        # attribute stays None and acquisition is completely ungated:
        # byte-for-byte the historical hybrid behaviour, so scope=global
        # deployments never change.
        self.rate_limit_breaker = rate_limit_breaker
        self.warm_workers = warm_workers
        self.queue_timeout = queue_timeout
        self.target_url = target_url
        self.auto_login = auto_login
        self.allow_local_mock = allow_local_mock
        self._capacity = asyncio.Semaphore(max_workers)
        self._idle: list[CurlCffiSession] = []
        self._leased: dict[str, CurlCffiSession] = {}
        self._all: dict[str, CurlCffiSession] = {}
        self._lock = asyncio.Lock()
        self._page: Any | None = None
        self._token_manager: TokenManager | None = None
        self._started = False
        self._closed = False
        self._queue_waiters = 0
        self._created_workers = 0
        self._closed_workers = 0
        self._local_mock_backend = False

    @property
    def local_mock_backend(self) -> bool:
        """Whether this factory is serving the explicit local dev/test fallback."""
        return self._local_mock_backend

    def _resolve_token_cache_dir(self) -> Path | None:
        """Derive the T4-PERSIST token cache dir from the browser profile.

        The TokenBundle disk cache lives next to the browser profile so a
        gateway restart within ``refresh_interval`` can reuse it without
        touching the browser.  When no usable profile dir is known (attribute
        missing or not a path), return ``None`` — ``TokenManager`` then keeps
        its pre-cache behaviour instead of crashing.
        """
        profile = getattr(self.browser_manager, "profile_dir", None)
        try:
            return Path(profile) if profile else None
        except (TypeError, ValueError):
            logger.warning("hybrid_token_cache_dir_unusable", exc_info=True)
            return None

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("worker factory is closed")
        if self._started:
            return
        await self.browser_manager.start()
        self._page = await self.browser_manager.new_page()
        await self._page.goto(self.target_url, wait_until="domcontentloaded", timeout=45_000)
        self._token_manager = TokenManager(
            self._page,
            auto_login=self.auto_login,
            allow_local_mock=self.allow_local_mock,
            cache_dir=self._resolve_token_cache_dir(),
        )
        bundle = await self._token_manager.extract_all()
        self._local_mock_backend = bundle.is_local_mock
        if self._local_mock_backend:
            logger.warning("hybrid_local_mock_enabled")
        self._started = True
        for _ in range(self.warm_workers):
            self._idle.append(self._new_session())

    def _new_session(self) -> CurlCffiSession:
        if self._token_manager is None:
            raise RuntimeError("hybrid worker factory is not started")
        session = CurlCffiSession(CurlCffiTransport(self._token_manager))
        self._all[session.session_id] = session
        self._created_workers += 1
        return session

    async def _acquire_capacity(self) -> None:
        async with self._lock:
            self._queue_waiters += 1
        try:
            await asyncio.wait_for(self._capacity.acquire(), timeout=self.queue_timeout)
        except TimeoutError as exc:
            raise WorkerQueueTimeout(
                f"No hybrid worker became available within {self.queue_timeout:.1f}s"
            ) from exc
        finally:
            async with self._lock:
                self._queue_waiters -= 1

    async def acquire(self) -> tuple[str, CurlCffiSession]:
        await self.start()
        # POOL-PER-ACCT-BREAKER (row S): only an explicitly injected breaker
        # gates acquisition -- while that account's cooldown window is open
        # this raises BackendCoolingDown instead of burning queue capacity,
        # and after the window elapses exactly one half-open probe passes.
        # No injected breaker means fully ungated historical behaviour.
        breaker = self.rate_limit_breaker
        ticket = None if breaker is None else breaker.before_acquire()
        try:
            await self._acquire_capacity()
            async with self._lock:
                session = self._idle.pop() if self._idle else self._new_session()
                self._leased[session.session_id] = session
        except BaseException:
            if breaker is not None:
                # Abandon the half-open probe slot so it can never wedge.
                breaker.finish_probe(ticket)
            raise
        if breaker is not None:
            # A clean worker handout counts as the successful probe.
            breaker.record_success(ticket)
        return session.session_id, session

    async def release(self, session_id: str, *, reusable: bool = True) -> None:
        async with self._lock:
            session = self._leased.pop(session_id, None)
        if session is None:
            return
        close_session = False
        if reusable and session.state in {SessionState.READY, SessionState.RETRYABLE_ERROR}:
            async with self._lock:
                if len(self._idle) < self.warm_workers:
                    self._idle.append(session)
                else:
                    self._all.pop(session.session_id, None)
                    self._closed_workers += 1
                    close_session = True
        else:
            async with self._lock:
                self._all.pop(session.session_id, None)
                self._closed_workers += 1
            close_session = True
        if close_session:
            await session.close()
        self._capacity.release()

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[CurlCffiSession]:
        session_id, session = await self.acquire()
        try:
            yield session
        finally:
            await self.release(session_id, reusable=session.state != SessionState.CLOSED)

    async def stats(self) -> WorkerFactoryStats:
        async with self._lock:
            return WorkerFactoryStats(
                max_workers=self.max_workers,
                live_workers=len(self._all),
                idle_workers=len(self._idle),
                leased_workers=len(self._leased),
                queue_waiters=self._queue_waiters,
                created_workers=self._created_workers,
                closed_workers=self._closed_workers,
            )

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            sessions = list(self._all.values())
            self._all.clear()
            self._idle.clear()
            self._leased.clear()
        for session in sessions:
            await session.close()
        await self.browser_manager.stop()
