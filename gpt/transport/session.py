from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Page

from gpt.drivers.protocol import ProtocolDriver
from gpt.drivers.ui import UIDriver
from gpt.state import (
    AuthRequired,
    BrowserDisconnected,
    CommitUnknown,
    ModelUnavailable,
    ProtocolChanged,
    RateLimited,
    SessionState,
    SessionStateMachine,
    UIChanged,
    WebChatError,
)
from gpt.transport.browser import BrowserManager
from gpt.types import (
    CapabilitySnapshot,
    ModelInfo,
    ReconciliationResult,
    RequestSubmitted,
    ResponseFailed,
    ResponseStarted,
    SendRequest,
    SessionEvent,
    SessionInfo,
    StateChanged,
    Turn,
    TurnResult,
)


class ChatGPTWebSession:
    """One reliable logical ChatGPT Web conversation.

    All consumers use this class; selectors and transport details remain inside
    drivers. Sends are serialized to prevent accidental duplicate turns.
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        page: Page,
        session_id: str | None = None,
        *,
        owns_browser_manager: bool = True,
    ):
        self.browser_manager = browser_manager
        self.page = page
        self._owns_browser_manager = owns_browser_manager
        self.session_id = session_id or f"wc_{uuid.uuid4().hex[:10]}"
        self.state_machine = SessionStateMachine(SessionState.BOOTING)
        self.ui_driver = UIDriver(page)
        self.protocol_driver = ProtocolDriver(page)
        self._events: asyncio.Queue[SessionEvent | None] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._history_cache: list[Turn] = []
        self._conversation_id: str | None = None
        self._selected_model: ModelInfo | None = None
        self._selected_effort: str | None = None
        self._capability_snapshot: CapabilitySnapshot | None = None
        self._current_send_submitted = False
        self._current_send_text: str | None = None
        self._created_at = datetime.now(timezone.utc).isoformat()
        self._last_used_at = self._created_at
        self._last_state_change_ns = time.monotonic_ns()
        self.state_machine.add_listener(self._on_state_change)

    @property
    def state(self) -> SessionState:
        return self.state_machine.state

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id


    @staticmethod
    def _norm_choice(value: str | None) -> str:
        return (value or "").casefold().strip()

    def _selected_model_matches(self, model: str) -> bool:
        selected = self._selected_model
        if selected is None:
            return False
        wanted = self._norm_choice(model)
        return wanted in {self._norm_choice(selected.id), self._norm_choice(selected.label)}

    def _selected_effort_matches(self, effort: str) -> bool:
        return self._norm_choice(self._selected_effort) == self._norm_choice(effort)

    async def _on_state_change(
        self, old: SessionState, new: SessionState, reason: str | None
    ) -> None:
        now_ns = time.monotonic_ns()
        duration_ms = (now_ns - self._last_state_change_ns) / 1_000_000
        self._last_state_change_ns = now_ns
        self._emit(
            StateChanged(
                old_state=old.value,
                new_state=new.value,
                reason=reason,
                duration_ms=duration_ms,
            )
        )

    @classmethod
    async def create(
        cls,
        headless: bool = True,
        persistent: bool = False,
        profile_dir: str | None = None,
        executable_path: str | None = None,
        cdp_url: str | None = None,
        target_url: str = "https://chatgpt.com",
        browser_manager: BrowserManager | None = None,
    ) -> ChatGPTWebSession:
        owns_manager = browser_manager is None
        manager = browser_manager or BrowserManager(
            headless=headless,
            persistent=persistent,
            profile_dir=profile_dir,
            executable_path=executable_path,
            cdp_url=cdp_url,
        )
        page: Page | None = None
        try:
            await manager.start()
            page = await manager.new_page()
            session = cls(
                manager,
                page,
                owns_browser_manager=owns_manager,
            )
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
            await session.ui_driver.dismiss_popups()
            status = "blocked"
            bootstrap_deadline = asyncio.get_running_loop().time() + 15
            while asyncio.get_running_loop().time() < bootstrap_deadline:
                status = await session.ui_driver.auth_status()
                if status != "blocked":
                    break
                await asyncio.sleep(0.5)
            if status == "required":
                await session.state_machine.transition_to(
                    SessionState.RATE_LIMITED, "ChatGPT anonymous quota exhausted; redirected to login wall."
                )
                raise RateLimited("ChatGPT anonymous quota exhausted; redirected to login wall.")
            if status == "blocked":
                await session.state_machine.transition_to(
                    SessionState.RATE_LIMITED, "ChatGPT anonymous composer unavailable; rate limited."
                )
                raise RateLimited("ChatGPT anonymous quota exhausted; redirected to login wall.")

            try:
                session._capability_snapshot = await session.ui_driver.capabilities()
                selected = next(
                    (
                        model
                        for model in session._capability_snapshot.models
                        if model.selected
                    ),
                    None,
                )
                session._selected_model = selected
                session._selected_effort = session._capability_snapshot.selected_effort
            except Exception:
                session._capability_snapshot = None
            await session.state_machine.transition_to(SessionState.READY)
            return session
        except Exception:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            if owns_manager:
                await manager.stop()
            raise

    def _emit(self, event: SessionEvent) -> None:
        if self._events.qsize() >= 1_000:
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._events.put_nowait(event)

    async def _handle_driver_event(self, event: SessionEvent) -> None:
        self._emit(event)
        if isinstance(event, RequestSubmitted):
            self._current_send_submitted = True
            if event.conversation_id:
                self._conversation_id = event.conversation_id
            await self.state_machine.transition_to(SessionState.WAITING_RESPONSE)
        elif isinstance(event, ResponseStarted):
            await self.state_machine.transition_to(SessionState.GENERATING)

    async def _raise_commit_unknown_if_submitted(
        self,
        exc: Exception,
        turn_id: str,
    ) -> None:
        if not self._current_send_submitted:
            return
        try:
            observed_id = self.ui_driver.conversation_id()
        except Exception:
            observed_id = None
        if observed_id:
            self._conversation_id = observed_id
        message = (
            "The user turn may have reached ChatGPT but completion was not verified; "
            "reconcile conversation history before retrying."
        )
        await self.state_machine.transition_to(SessionState.COMMIT_UNKNOWN, message)
        self._emit(ResponseFailed(turn_id=turn_id, reason=message))
        raise CommitUnknown(
            message,
            conversation_id=self._conversation_id,
            submitted=True,
        ) from exc

    async def _ensure_page(self) -> None:
        if not self.browser_manager.connected:
            await self.state_machine.transition_to(
                SessionState.BROWSER_DISCONNECTED, "Browser is disconnected."
            )
            raise BrowserDisconnected("Browser process is no longer connected.")
        if self.page is None or self.page.is_closed():
            self.page = await self.browser_manager.new_page()
            self.ui_driver = UIDriver(self.page)
            self.protocol_driver = ProtocolDriver(self.page)
            if self._conversation_id:
                await self.ui_driver.open_conversation(self._conversation_id)
            else:
                await self.ui_driver.new_conversation()
            await self.state_machine.transition_to(SessionState.READY, "Page recovered.")

    async def new_conversation(self, model: str | None = None) -> SessionInfo:
        async with self._send_lock:
            await self._ensure_page()
            await self.ui_driver.new_conversation()
            self._conversation_id = None
            self._history_cache.clear()
            if model:
                self._selected_model = await self.ui_driver.select_model(model)
            else:
                try:
                    self._capability_snapshot = await self.ui_driver.capabilities()
                    self._selected_model = next(
                        (
                            item
                            for item in self._capability_snapshot.models
                            if item.selected
                        ),
                        None,
                    )
                    self._selected_effort = self._capability_snapshot.selected_effort
                except Exception:
                    self._capability_snapshot = None
                    self._selected_model = None
                    self._selected_effort = None
            await self.state_machine.transition_to(SessionState.READY)
            return self.get_info()

    async def open(self, conversation_id: str) -> SessionInfo:
        async with self._send_lock:
            await self._ensure_page()
            await self.ui_driver.open_conversation(conversation_id)
            self._conversation_id = conversation_id
            self._history_cache = await self.ui_driver.history()
            await self.state_machine.transition_to(SessionState.READY)
            return self.get_info()

    async def capabilities(self, refresh: bool = True) -> CapabilitySnapshot:
        await self._ensure_page()
        if refresh or self._capability_snapshot is None:
            snapshot = await self.ui_driver.capabilities()
            snapshot.protocol_send_eligible = bool(
                self.protocol_driver.available
                and await self.protocol_driver.probe_protocol_compatibility()
            )
            self._capability_snapshot = snapshot
            selected = next((model for model in snapshot.models if model.selected), None)
            if selected is not None:
                self._selected_model = selected
            self._selected_effort = snapshot.selected_effort
        return self._capability_snapshot

    async def models(self) -> list[ModelInfo]:
        snapshot = await self.capabilities(refresh=True)
        return list(snapshot.models)

    async def select_model(self, model: str) -> None:
        async with self._send_lock:
            await self._ensure_page()
            try:
                if self._selected_model_matches(model):
                    return
                self._selected_model = await self.ui_driver.select_model(model)
            except ModelUnavailable as exc:
                # A bad model id is a request-scoped client error.  Do not
                # poison the shared browser session and prevent later requests
                # using the default model from running.
                await self.state_machine.transition_to(SessionState.MODEL_UNAVAILABLE, str(exc))
                await self.state_machine.transition_to(SessionState.READY, str(exc))
                raise
            await self.state_machine.transition_to(SessionState.READY)

    async def select_reasoning_effort(self, effort: str) -> None:
        async with self._send_lock:
            await self._ensure_page()
            if self._selected_effort_matches(effort):
                return
            self._selected_effort = await self.ui_driver.select_reasoning_effort(effort)
            await self.state_machine.transition_to(SessionState.READY)

    async def send(
        self,
        text: str,
        timeout_seconds: float = 120,
        model: str | None = None,
        reasoning_effort: str | None = None,
        files: tuple[str, ...] | list[str | Path] | None = None,
    ) -> TurnResult:
        if not text.strip():
            raise ValueError("Prompt text cannot be empty.")
        async with self._send_lock:
            await self._ensure_page()
            if self.state not in {SessionState.READY, SessionState.RETRYABLE_ERROR}:
                raise WebChatError(f"Cannot send while session is {self.state.value}.")
            self._current_send_submitted = False
            self._current_send_text = text
            await self.state_machine.transition_to(SessionState.PREPARING_SEND)
            if model:
                try:
                    if not self._selected_model_matches(model):
                        self._selected_model = await self.ui_driver.select_model(model)
                except ModelUnavailable as exc:
                    await self.state_machine.transition_to(SessionState.MODEL_UNAVAILABLE, str(exc))
                    await self.state_machine.transition_to(SessionState.READY, str(exc))
                    raise
            if reasoning_effort and not self._selected_effort_matches(reasoning_effort):
                self._selected_effort = await self.ui_driver.select_reasoning_effort(
                    reasoning_effort
                )
            self._last_used_at = datetime.now(timezone.utc).isoformat()
            file_tuple = tuple(str(f) for f in files) if files else ()
            request = SendRequest(
                text=text,
                conversation_id=self._conversation_id,
                model=self._selected_model,
                reasoning_effort=self._selected_effort,
                timeout_seconds=timeout_seconds,
                files=file_tuple,
            )
            user_turn = Turn(
                turn_id=f"turn_{uuid.uuid4().hex[:10]}",
                role="user",
                text=text,
                model=self._selected_model.label if self._selected_model else None,
            )
            await self.state_machine.transition_to(SessionState.SENDING)
            try:
                await self.state_machine.transition_to(SessionState.WAITING_RESPONSE)
                if self.protocol_driver.available:
                    try:
                        result = await self.protocol_driver.send(
                            request, event_callback=self._handle_driver_event
                        )
                    except ProtocolChanged as exc:
                        await self.state_machine.transition_to(SessionState.PROTOCOL_CHANGED, str(exc))
                        await self.state_machine.transition_to(
                            SessionState.WAITING_RESPONSE, "Falling back to semantic UI driver."
                        )
                        result = await self.ui_driver.send(
                            request, event_callback=self._handle_driver_event
                        )
                else:
                    result = await self.ui_driver.send(
                        request, event_callback=self._handle_driver_event
                    )
                self._conversation_id = result.conversation_id or self.ui_driver.conversation_id()
                self._history_cache.extend(
                    [
                        user_turn,
                        Turn(
                            turn_id=result.turn_id,
                            role="assistant",
                            text=result.text,
                            model=result.model,
                        ),
                    ]
                )
                self._current_send_submitted = False
                self._current_send_text = None
                await self.state_machine.transition_to(SessionState.READY)
                return result
            except AuthRequired as exc:
                self._current_send_submitted = False
                self._current_send_text = None
                await self.state_machine.transition_to(SessionState.AUTH_REQUIRED, str(exc))
                self._emit(ResponseFailed(turn_id=user_turn.turn_id, reason=str(exc)))
                raise
            except RateLimited as exc:
                self._current_send_submitted = False
                self._current_send_text = None
                await self.state_machine.transition_to(SessionState.RATE_LIMITED, str(exc))
                self._emit(ResponseFailed(turn_id=user_turn.turn_id, reason=str(exc)))
                raise
            except UIChanged as exc:
                await self._raise_commit_unknown_if_submitted(exc, user_turn.turn_id)
                await self.state_machine.transition_to(SessionState.UI_CHANGED, str(exc))
                self._emit(ResponseFailed(turn_id=user_turn.turn_id, reason=str(exc)))
                raise
            except Exception as exc:
                await self._raise_commit_unknown_if_submitted(exc, user_turn.turn_id)
                await self.state_machine.transition_to(SessionState.RETRYABLE_ERROR, str(exc))
                self._emit(ResponseFailed(turn_id=user_turn.turn_id, reason=str(exc)))
                raise

    async def events(self) -> AsyncIterator[SessionEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    def drain_events(self) -> list[SessionEvent]:
        drained: list[SessionEvent] = []
        while True:
            try:
                event = self._events.get_nowait()
            except asyncio.QueueEmpty:
                return drained
            if event is not None:
                drained.append(event)

    async def history(self) -> list[Turn]:
        await self._ensure_page()
        try:
            visible = await self.ui_driver.history()
            if visible:
                self._history_cache = visible
        except Exception:
            pass
        return list(self._history_cache)

    async def reconcile(self, expected_user_text: str) -> ReconciliationResult:
        """Inspect authoritative web history before any retry of an uncertain send.

        This method never sends a message. It only determines whether the exact
        controller-rendered user turn is already present and, when available,
        returns the assistant turn immediately following it.
        """
        if not expected_user_text.strip():
            raise ValueError("expected_user_text cannot be empty")
        async with self._send_lock:
            await self._ensure_page()
            await self.state_machine.transition_to(SessionState.VERIFYING_PERSISTENCE)
            if self._conversation_id:
                actual_id = self.ui_driver.conversation_id()
                if actual_id != self._conversation_id:
                    await self.ui_driver.open_conversation(self._conversation_id)
            observed_id = self.ui_driver.conversation_id()
            if observed_id:
                self._conversation_id = observed_id
            visible = await self.ui_driver.history()
            if visible:
                self._history_cache = visible
            expected = expected_user_text.strip()
            for index in range(len(visible) - 1, -1, -1):
                turn = visible[index]
                if turn.role != "user" or turn.text.strip() != expected:
                    continue
                assistant_text: str | None = None
                for following in visible[index + 1 :]:
                    if following.role == "user":
                        break
                    if following.role == "assistant" and following.text.strip():
                        assistant_text = following.text
                        break
                if assistant_text is None:
                    await self.state_machine.transition_to(
                        SessionState.COMMIT_UNKNOWN,
                        "User turn is persisted but assistant completion is not yet verified.",
                    )
                else:
                    await self.state_machine.transition_to(
                        SessionState.READY,
                        "Uncertain send reconciled from persisted history.",
                    )
                return ReconciliationResult(
                    user_turn_present=True,
                    assistant_text=assistant_text,
                    conversation_id=self._conversation_id,
                )
            await self.state_machine.transition_to(
                SessionState.READY,
                "Expected user turn is absent from authoritative history.",
            )
            return ReconciliationResult(
                user_turn_present=False,
                assistant_text=None,
                conversation_id=self._conversation_id,
            )

    async def reload(self) -> None:
        async with self._send_lock:
            await self._ensure_page()
            await self.page.reload(wait_until="domcontentloaded", timeout=45_000)
            await self.ui_driver.dismiss_popups()
            await self.ui_driver.get_composer()
            actual_id = self.ui_driver.conversation_id()
            if self._conversation_id and actual_id != self._conversation_id:
                await self.ui_driver.open_conversation(self._conversation_id)
            await self.state_machine.transition_to(SessionState.READY)

    async def close(self) -> None:
        if self.state == SessionState.CLOSED:
            return
        await self.state_machine.transition_to(SessionState.CLOSED)
        self._events.put_nowait(None)
        if self._owns_browser_manager:
            await self.browser_manager.stop()
        elif self.page is not None and not self.page.is_closed():
            await self.page.close()

    def get_info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            conversation_id=self._conversation_id,
            conversation_url=self.page.url if not self.page.is_closed() else None,
            model=self._selected_model,
            state=self.state.value,
            created_at=self._created_at,
            last_used_at=self._last_used_at,
        )
