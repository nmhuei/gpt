from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from playwright.async_api import Page

from gpt.browser import BrowserManager
from gpt.drivers.protocol import ProtocolDriver
from gpt.drivers.ui import UIDriver
from gpt.state import (
    AuthRequired,
    BrowserDisconnected,
    GenerationTimeout,
    ModelUnavailable,
    ProtocolChanged,
    RateLimited,
    SessionState,
    SessionStateMachine,
    UIChanged,
    WebChatError,
)
from gpt.types import (
    ModelInfo,
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
    ):
        self.browser_manager = browser_manager
        self.page = page
        self.session_id = session_id or f"wc_{uuid.uuid4().hex[:10]}"
        self.state_machine = SessionStateMachine(SessionState.BOOTING)
        self.ui_driver = UIDriver(page)
        self.protocol_driver = ProtocolDriver(page)
        self._events: asyncio.Queue[SessionEvent | None] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._history_cache: list[Turn] = []
        self._conversation_id: str | None = None
        self._selected_model: ModelInfo | None = None
        self._created_at = datetime.now(timezone.utc).isoformat()
        self._last_used_at = self._created_at
        self.state_machine.add_listener(self._on_state_change)

    @property
    def state(self) -> SessionState:
        return self.state_machine.state

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

    async def _on_state_change(
        self, old: SessionState, new: SessionState, reason: str | None
    ) -> None:
        self._emit(StateChanged(old_state=old.value, new_state=new.value, reason=reason))

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
            session = cls(manager, page)
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
                    SessionState.AUTH_REQUIRED, "Manual login is required."
                )
                raise AuthRequired("Log in manually with a persistent, headful browser profile.")
            if status == "blocked":
                await session.state_machine.transition_to(
                    SessionState.UI_CHANGED, "Composer is unavailable after bootstrap."
                )
                raise UIChanged("ChatGPT did not expose a usable composer.")
            await session.state_machine.transition_to(SessionState.READY)
            return session
        except Exception:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
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
        if isinstance(event, ResponseStarted):
            await self.state_machine.transition_to(SessionState.GENERATING)

    async def _ensure_page(self) -> None:
        if not self.browser_manager.connected:
            await self.state_machine.transition_to(
                SessionState.BROWSER_DISCONNECTED, "Browser is disconnected."
            )
            raise BrowserDisconnected("Browser is disconnected.")
        if not self.page.is_closed():
            return
        await self.state_machine.transition_to(SessionState.PAGE_CRASHED, "Page was closed.")
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
                self._selected_model = None
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

    async def models(self) -> list[ModelInfo]:
        await self._ensure_page()
        models = await self.ui_driver.models()
        selected = next((model for model in models if model.selected), None)
        if selected:
            self._selected_model = selected
        return models

    async def select_model(self, model: str) -> None:
        async with self._send_lock:
            await self._ensure_page()
            try:
                self._selected_model = await self.ui_driver.select_model(model)
            except ModelUnavailable as exc:
                # A bad model id is a request-scoped client error.  Do not
                # poison the shared browser session and prevent later requests
                # using the default model from running.
                await self.state_machine.transition_to(SessionState.MODEL_UNAVAILABLE, str(exc))
                await self.state_machine.transition_to(SessionState.READY, str(exc))
                raise
            await self.state_machine.transition_to(SessionState.READY)

    async def send(self, text: str, timeout_seconds: float = 120) -> TurnResult:
        if not text.strip():
            raise ValueError("Prompt text cannot be empty.")
        async with self._send_lock:
            await self._ensure_page()
            if self.state not in {SessionState.READY, SessionState.RETRYABLE_ERROR}:
                raise WebChatError(f"Cannot send while session is {self.state.value}.")
            self._last_used_at = datetime.now(timezone.utc).isoformat()
            request = SendRequest(
                text=text,
                conversation_id=self._conversation_id,
                model=self._selected_model,
                timeout_seconds=timeout_seconds,
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
                await self.state_machine.transition_to(SessionState.READY)
                return result
            except AuthRequired as exc:
                await self.state_machine.transition_to(SessionState.AUTH_REQUIRED, str(exc))
                self._emit(ResponseFailed(turn_id=user_turn.turn_id, reason=str(exc)))
                raise
            except RateLimited as exc:
                await self.state_machine.transition_to(SessionState.RATE_LIMITED, str(exc))
                self._emit(ResponseFailed(turn_id=user_turn.turn_id, reason=str(exc)))
                raise
            except UIChanged as exc:
                await self.state_machine.transition_to(SessionState.UI_CHANGED, str(exc))
                self._emit(ResponseFailed(turn_id=user_turn.turn_id, reason=str(exc)))
                raise
            except (GenerationTimeout, Exception) as exc:
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
        await self.browser_manager.stop()

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
