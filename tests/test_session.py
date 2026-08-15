from unittest.mock import AsyncMock, MagicMock

import pytest

from gpt.session import ChatGPTWebSession
from gpt.state import ProtocolChanged, SessionState
from gpt.types import (
    ResponseCompleted,
    ResponseDelta,
    ResponseStarted,
    TurnResult,
)


class FakeManager:
    connected = True

    def __init__(self):
        self.stop = AsyncMock()

    async def new_page(self):
        raise AssertionError("page recovery was not expected")


class FakeUI:
    def __init__(self):
        self.sent = 0

    async def send(self, request, event_callback=None):
        self.sent += 1
        await event_callback(ResponseStarted(turn_id="assistant-1"))
        await event_callback(ResponseDelta(text="hello", accumulated_text="hello"))
        await event_callback(
            ResponseCompleted(
                turn_id="assistant-1", text="hello", conversation_id="conv-1"
            )
        )
        return TurnResult(
            turn_id="assistant-1", conversation_id="conv-1", text="hello"
        )

    def conversation_id(self):
        return "conv-1"

    async def history(self):
        return []


class NoProtocol:
    available = False


class BrokenProtocol:
    available = True

    async def send(self, request, event_callback=None):
        raise ProtocolChanged("fixture drift")


def make_session():
    page = MagicMock()
    page.url = "https://chatgpt.com/"
    page.is_closed.return_value = False
    session = ChatGPTWebSession(FakeManager(), page)
    session.ui_driver = FakeUI()
    session.protocol_driver = NoProtocol()
    return session


@pytest.mark.anyio
async def test_session_send_uses_boundary_and_returns_to_ready():
    session = make_session()
    await session.state_machine.transition_to(SessionState.READY)
    session.drain_events()

    result = await session.send("hi")

    assert result.text == "hello"
    assert session.conversation_id == "conv-1"
    assert session.state == SessionState.READY
    events = session.drain_events()
    assert any(isinstance(event, ResponseDelta) for event in events)
    history = await session.history()
    assert [turn.role for turn in history] == ["user", "assistant"]


@pytest.mark.anyio
async def test_session_falls_back_only_after_protocol_changed():
    session = make_session()
    session.protocol_driver = BrokenProtocol()
    await session.state_machine.transition_to(SessionState.READY)
    session.drain_events()

    result = await session.send("fallback")

    assert result.text == "hello"
    assert session.ui_driver.sent == 1
    states = [
        event.new_state
        for event in session.drain_events()
        if hasattr(event, "new_state")
    ]
    assert SessionState.PROTOCOL_CHANGED.value in states
