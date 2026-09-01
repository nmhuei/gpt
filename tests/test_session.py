from unittest.mock import AsyncMock, MagicMock

import pytest

from gpt.session import ChatGPTWebSession
from gpt.state import (
    AuthRequired,
    CommitUnknown,
    ProtocolChanged,
    RateLimited,
    SessionState,
)
from gpt.types import (
    ModelInfo,
    RequestSubmitted,
    ResponseCompleted,
    ResponseDelta,
    ResponseStarted,
    StateChanged,
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
        self.selected_models: list[str] = []

    async def select_model(self, model: str) -> ModelInfo:
        self.selected_models.append(model)
        return ModelInfo(id=model, label=model)

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
    transitions = [event for event in events if isinstance(event, StateChanged)]
    assert transitions
    assert all(event.duration_ms is not None and event.duration_ms >= 0 for event in transitions)
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


@pytest.mark.anyio
async def test_session_send_applies_direct_model_selection_once():
    session = make_session()
    await session.state_machine.transition_to(SessionState.READY)

    await session.send("use the selected model", model="GPT Coding")

    assert session.ui_driver.selected_models == ["GPT Coding"]


class SubmittedThenRateLimitedUI(FakeUI):
    async def send(self, request, event_callback=None):
        self.sent += 1
        await event_callback(RequestSubmitted(turn_id="user-rate", conversation_id="conv-rate"))
        raise RateLimited("anonymous quota exhausted")


@pytest.mark.anyio
async def test_session_preserves_rate_limit_instead_of_commit_unknown_after_submit():
    session = make_session()
    session.ui_driver = SubmittedThenRateLimitedUI()
    await session.state_machine.transition_to(SessionState.READY)
    session.drain_events()

    with pytest.raises(RateLimited):
        await session.send("trigger quota")

    assert session.state == SessionState.RATE_LIMITED
    assert not isinstance(session.state, CommitUnknown)
    states = [
        event.new_state
        for event in session.drain_events()
        if hasattr(event, "new_state")
    ]
    assert SessionState.COMMIT_UNKNOWN.value not in states
    assert SessionState.RATE_LIMITED.value in states


@pytest.mark.anyio
async def test_create_waits_for_full_page_load_before_capability_probe(monkeypatch):
    events: list[str] = []

    class BootstrapPage:
        url = "https://chatgpt.com/"

        async def goto(self, *_args, **_kwargs):
            events.append("domcontentloaded")

        async def wait_for_load_state(self, state, **_kwargs):
            assert state == "load"
            events.append("load")

        def is_closed(self):
            return False

        async def close(self):
            events.append("page_close")

    page = BootstrapPage()

    class BootstrapManager:
        connected = True

        async def start(self):
            events.append("manager_start")

        async def new_page(self):
            return page

    class BootstrapUI:
        def __init__(self, _page):
            pass

        async def dismiss_popups(self):
            events.append("dismiss")

        async def auth_status(self):
            return "anonymous"

        async def capabilities(self):
            events.append("capabilities")
            return MagicMock(models=[], selected_effort=None)

    monkeypatch.setattr("gpt.transport.session.UIDriver", BootstrapUI)

    session = await ChatGPTWebSession.create(browser_manager=BootstrapManager())

    assert "load" in events
    assert events.index("domcontentloaded") < events.index("load") < events.index("capabilities")
    await session.close()


@pytest.mark.anyio
async def test_create_maps_login_required_to_auth_required(monkeypatch):
    events: list[str] = []

    class LoginPage:
        url = "https://chatgpt.com/"

        async def goto(self, *_args, **_kwargs):
            events.append("goto")

        async def wait_for_load_state(self, _state, **_kwargs):
            events.append("load")

        def is_closed(self):
            return False

        async def close(self):
            events.append("page_close")

    page = LoginPage()

    class LoginManager:
        connected = True

        async def start(self):
            events.append("manager_start")

        async def new_page(self):
            return page

    class LoginUI:
        def __init__(self, _page):
            pass

        async def dismiss_popups(self):
            pass

        async def auth_status(self):
            return "required"

    monkeypatch.setattr("gpt.transport.session.UIDriver", LoginUI)

    with pytest.raises(AuthRequired, match="authentication is required"):
        await ChatGPTWebSession.create(browser_manager=LoginManager())

    assert "page_close" in events
