from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any


class SessionState(str, Enum):
    CLOSED = "closed"
    BOOTING = "booting"
    AUTH_REQUIRED = "auth_required"
    READY = "ready"
    PREPARING_SEND = "preparing_send"
    SENDING = "sending"
    WAITING_RESPONSE = "waiting_response"
    GENERATING = "generating"
    VERIFYING_PERSISTENCE = "verifying_persistence"
    COMMIT_UNKNOWN = "commit_unknown"
    RETRYABLE_ERROR = "retryable_error"
    RATE_LIMITED = "rate_limited"
    MODEL_UNAVAILABLE = "model_unavailable"
    UI_CHANGED = "ui_changed"
    PROTOCOL_CHANGED = "protocol_changed"
    PAGE_CRASHED = "page_crashed"
    BROWSER_DISCONNECTED = "browser_disconnected"
    FATAL_ERROR = "fatal_error"


class WebChatError(Exception):
    """Base exception for webchat module."""


ChatGPTWebError = WebChatError


class AuthRequired(WebChatError):
    """Authentication is required to proceed with ChatGPT."""


class AnonymousSessionUnavailable(WebChatError):
    """A certification/live run expected an unauthenticated Free session."""


class ModelUnavailable(WebChatError):
    """Requested model is not available in the current account or UI state."""


class ConversationNotFound(WebChatError):
    """Conversation could not be found or loaded."""


class ConversationConflict(WebChatError, ValueError):
    """Client transcript/tool state conflicts with the selected conversation."""



class GenerationTimeout(WebChatError):
    """Generation exceeded the maximum configured timeout."""


class GenerationInterrupted(WebChatError):
    """Generation was interrupted or cancelled."""


class EmptyModelResponse(WebChatError):
    """ChatGPT Web completed without usable assistant text or tool calls."""


class CommitUnknown(WebChatError):
    """A send may have reached ChatGPT, so retry requires reconciliation first."""

    def __init__(
        self,
        message: str,
        *,
        conversation_id: str | None = None,
        submitted: bool = True,
    ) -> None:
        super().__init__(message)
        self.conversation_id = conversation_id
        self.submitted = submitted


class ProtocolChanged(WebChatError):
    """Detected ChatGPT wire protocol incompatibility or signature drift."""


class UIChanged(WebChatError):
    """Detected UI changes or DOM invariant failure."""


class RateLimited(WebChatError):
    """ChatGPT returned a rate limit response (e.g., 429 or message cap)."""


class PageCrashed(WebChatError):
    """Browser tab or page crashed unexpectedly."""


class BrowserDisconnected(WebChatError):
    """Browser context was disconnected or closed."""


class MalformedResponse(WebChatError):
    """Received a malformed or unparseable event stream/payload."""


class MalformedToolCall(WebChatError):
    """Tool output was ambiguous, invalid, unknown, or unsafe to map."""


class SessionStateMachine:
    """Manages state transitions and notifies subscribers."""

    def __init__(self, initial_state: SessionState = SessionState.CLOSED):
        self._state: SessionState = initial_state
        self._listeners: list[Callable[[SessionState, SessionState, str | None], Any]] = []
        self._lock = asyncio.Lock()

    @property
    def state(self) -> SessionState:
        return self._state

    def add_listener(
        self,
        listener: Callable[
            [SessionState, SessionState, str | None], Awaitable[Any] | Any
        ],
    ) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def add_async_listener(
        self, listener: Callable[[SessionState, SessionState, str | None], Awaitable[Any]]
    ) -> None:
        self.add_listener(listener)

    async def transition_to(
        self,
        new_state: SessionState,
        reason: str | None = None,
    ) -> None:
        async with self._lock:
            old_state = self._state
            if old_state == new_state:
                return

            self._state = new_state
            for listener in self._listeners:
                try:
                    result = listener(old_state, new_state, reason)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    pass

    def is_usable(self) -> bool:
        return self._state in {
            SessionState.READY,
            SessionState.GENERATING,
            SessionState.SENDING,
            SessionState.WAITING_RESPONSE,
        }

    def require_ready(self) -> None:
        if self._state != SessionState.READY:
            raise WebChatError(f"Session is not ready (state={self._state.value}).")
