"""History-cache bound tests (verify-fromscratch 2026-08-25 audit, RAM #1).

Scenario: ``ChatGPTWebSession._history_cache`` used to grow without limit --
every successful ``send`` appended a user + assistant turn pair and prompts can
reach ~250k chars, so long-lived workers leaked memory per turn.

Covers:
(a) exceeding ``WEBGPT_HISTORY_CACHE_MAX`` evicts the oldest entries first;
(b) cache hits still return the correct surviving data, and returned turns are
    copies (mutating them cannot corrupt the cache);
(c) ``WEBGPT_HISTORY_CACHE_MAX=0`` disables the cap entirely (legacy behaviour).
"""

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from gpt.session import ChatGPTWebSession
from gpt.state import SessionState
from gpt.transport.session import (
    DEFAULT_HISTORY_CACHE_MAX,
    HISTORY_CACHE_MAX_ENV,
    resolve_history_cache_max,
)
from gpt.types import (
    ResponseCompleted,
    ResponseStarted,
    Turn,
    TurnResult,
)


class FakeManager:
    connected = True


def make_page():
    page = MagicMock()
    page.url = "https://chatgpt.com/"
    page.is_closed.return_value = False
    return page


class CountingUI:
    """UI driver whose every send appends a distinct user/assistant pair."""

    def __init__(self):
        self.sent = 0

    async def send(self, request, event_callback=None):
        self.sent += 1
        await event_callback(ResponseStarted(turn_id=f"assistant-{self.sent}"))
        await event_callback(
            ResponseCompleted(
                turn_id=f"assistant-{self.sent}",
                text=f"reply-{self.sent}",
                conversation_id="conv-cap",
            )
        )
        return TurnResult(
            turn_id=f"assistant-{self.sent}",
            conversation_id="conv-cap",
            text=f"reply-{self.sent}",
        )

    def conversation_id(self):
        return "conv-cap"

    async def history(self):
        return []


class NoProtocol:
    available = False


class ExplodingHistoryUI(CountingUI):
    async def history(self):
        raise RuntimeError("history probe exploded")


def make_session(ui) -> ChatGPTWebSession:
    session = ChatGPTWebSession(cast(Any, FakeManager()), make_page())
    session.ui_driver = ui
    session.protocol_driver = cast(Any, NoProtocol())
    return session


async def send_n(session: ChatGPTWebSession, n: int) -> None:
    await session.state_machine.transition_to(SessionState.READY)
    session.drain_events()
    for i in range(n):
        await session.send(f"prompt-{i}")


# ---------------------------------------------------------------------------
# Env resolution
# ---------------------------------------------------------------------------


def test_env_unset_uses_default(monkeypatch):
    monkeypatch.delenv(HISTORY_CACHE_MAX_ENV, raising=False)
    assert resolve_history_cache_max() == DEFAULT_HISTORY_CACHE_MAX == 128


def test_env_invalid_keeps_default(monkeypatch):
    monkeypatch.setenv(HISTORY_CACHE_MAX_ENV, "not-a-number")
    assert resolve_history_cache_max() == DEFAULT_HISTORY_CACHE_MAX


def test_env_negative_keeps_default(monkeypatch):
    monkeypatch.setenv(HISTORY_CACHE_MAX_ENV, "-4")
    assert resolve_history_cache_max() == DEFAULT_HISTORY_CACHE_MAX


def test_env_zero_disables_cap(monkeypatch):
    monkeypatch.setenv(HISTORY_CACHE_MAX_ENV, "0")
    assert resolve_history_cache_max() == 0


# ---------------------------------------------------------------------------
# (a) Exceeding the cap evicts oldest entries, in order
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cap_evicts_oldest_turns_in_order(monkeypatch):
    monkeypatch.setenv(HISTORY_CACHE_MAX_ENV, "4")  # two send-pairs max
    ui = CountingUI()
    session = make_session(ui)

    await send_n(session, 3)  # appends 6 turns total

    # Only the newest 4 turns survive: pairs from send #2 and send #3.
    cached = session._history_cache
    assert len(cached) == 4
    assert [(turn.role, turn.text) for turn in cached] == [
        ("user", "prompt-1"),
        ("assistant", "reply-2"),
        ("user", "prompt-2"),
        ("assistant", "reply-3"),
    ]


@pytest.mark.anyio
async def test_cap_trims_replacement_history_from_open_path(monkeypatch):
    monkeypatch.setenv(HISTORY_CACHE_MAX_ENV, "2")
    session = make_session(CountingUI())

    visible = [Turn(turn_id=str(i), role="user", text=f"t{i}") for i in range(6)]
    session._set_history_cache(visible)

    assert len(session._history_cache) == 2
    assert [turn.text for turn in session._history_cache] == ["t4", "t5"]
    # The driver-provided list itself must never be mutated in place.
    assert len(visible) == 6


# ---------------------------------------------------------------------------
# (b) Hits still return the correct data; returns are alias-safe copies
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_capped_history_hit_returns_surviving_data(monkeypatch):
    monkeypatch.setenv(HISTORY_CACHE_MAX_ENV, "4")
    session = make_session(CountingUI())

    await send_n(session, 3)
    history = await session.history()

    assert [turn.text for turn in history] == [
        "prompt-1",
        "reply-2",
        "prompt-2",
        "reply-3",
    ]


@pytest.mark.anyio
async def test_history_fallback_returns_cached_turns_when_driver_fails():
    session = make_session(ExplodingHistoryUI())
    session._history_cache = [
        Turn(turn_id="u1", role="user", text="kept"),
        Turn(turn_id="a1", role="assistant", text="also kept"),
    ]

    history = await session.history()

    assert [turn.text for turn in history] == ["kept", "also kept"]


@pytest.mark.anyio
async def test_returned_turns_are_copies_not_aliases():
    session = make_session(CountingUI())
    original = Turn(turn_id="u1", role="user", text="immutable-ish", metadata={"k": "v"})
    session._history_cache = [original]

    history = await session.history()
    history[0].text = "MUTATED"
    history[0].metadata["k"] = "MUTATED"

    assert session._history_cache[0].text == "immutable-ish"
    assert session._history_cache[0].metadata == {"k": "v"}
    # Appending to the returned list must not grow the cache either.
    history.append(Turn(turn_id="x", role="user", text="extra"))
    assert len(session._history_cache) == 1


# ---------------------------------------------------------------------------
# (c) env=0 disables the cap (legacy unbounded behaviour)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_zero_env_restores_unbounded_cache(monkeypatch):
    monkeypatch.setenv(HISTORY_CACHE_MAX_ENV, "0")
    ui = CountingUI()
    session = make_session(ui)
    assert session._history_cache_max == 0

    await send_n(session, 5)  # 10 turns, far past any default cap

    assert len(session._history_cache) == 10
    assert session._history_cache[0].role == "user"
    assert session._history_cache[0].text == "prompt-0"
    assert session._history_cache[-1].text == "reply-5"
