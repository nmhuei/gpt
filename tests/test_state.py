
import pytest

from gpt.state import (
    SessionState,
    SessionStateMachine,
)


@pytest.mark.anyio
async def test_state_transitions_and_listeners():
    sm = SessionStateMachine(SessionState.CLOSED)
    assert sm.state == SessionState.CLOSED
    assert not sm.is_usable()

    events = []

    def on_change(old, new, reason):
        events.append((old, new, reason))

    sm.add_listener(on_change)

    await sm.transition_to(SessionState.BOOTING)
    assert sm.state == SessionState.BOOTING

    await sm.transition_to(SessionState.READY)
    assert sm.state == SessionState.READY
    assert sm.is_usable()

    assert len(events) == 2
    assert events[0] == (SessionState.CLOSED, SessionState.BOOTING, None)
    assert events[1] == (SessionState.BOOTING, SessionState.READY, None)
