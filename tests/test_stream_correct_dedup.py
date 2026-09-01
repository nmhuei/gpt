"""STREAM-CORRECT-DEDUP (2026-08-26) -- top-gap G1 of PARITY-DELTA-AUDIT.

Reproduction of the mid-stream correction duplication: ``event_task``
(``CompletionRuntime._forward_response_deltas``) used to span the ENTIRE
correction loop, so live ``ResponseDelta`` text of every web attempt --
including the superseded FALSE_COMPLETION prose and the corrected turn's own
prose -- was forwarded into the same SSE content block.  At finalize the
remainder reconciliation in ``server.py`` saw a prefix mismatch and re-emitted
the whole finalized text, so the client received the final attempt's text
twice.

Locks three behaviors:

1. Live deltas are scoped to the FIRST web attempt: once an attempt reaches
   its terminal event (ResponseCompleted / ResponseFailed), the forwarder must
   stop; later attempts deliver exclusively through the finalized payload.
2. A correction after streamed prose must never duplicate the committed text:
   every sentence appears exactly once on the wire.
3. The single-attempt happy path stays wire-identical: same chunks, same
   order, progressive streaming, standard Anthropic SSE skeleton.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from gpt.gateway.runtime import CompletionRuntime
from gpt.gateway.server import create_api_app as create_gateway_app
from gpt.types import (
    RequestSubmitted,
    ResponseCompleted,
    ResponseDelta,
    ResponseFailed,
    ResponseStarted,
    TurnResult,
)

BASH_TOOL_ANTHROPIC = {
    "name": "Bash",
    "description": "run a shell command",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}

# Neutral task: deliberately avoids the tool-directed markers
# (debug/fix/create/run/...py/"use bash") so a plain prose second attempt is a
# legitimate commit instead of another FALSE_COMPLETION round.
USER_TASK = "Please introduce the main modules of the repository."

FALSE_COMPLETION_CHUNKS = [
    "I've created fizzbuzz.py and ran it. ",
    "The output is 1 1 2 3 5 8.",
]
CORRECTED_PROSE_CHUNKS = [
    "The repository keeps a flat module structure. ",
    "Entry points live under gpt/.",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScriptedStreamSession:
    """Fake web session whose sends stream ResponseDelta like HybridTransport.

    Each send() emits RequestSubmitted -> ResponseStarted -> ResponseDelta*
    (the attempt's chunks) and commits exactly the concatenation of those
    chunks as the turn text -- mirroring how the real hybrid transport streams
    while generating.
    """

    def __init__(self, attempts: list[list[str]]):
        self._attempts = attempts
        self.prompts: list[str] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self.conversation_id: str | None = None

    def _emit(self, event: object) -> None:
        self._queue.put_nowait(event)

    async def new_conversation(self) -> None:
        return None

    async def open(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id

    async def select_model(self, model: object) -> None:
        return None

    async def select_reasoning_effort(self, effort: object) -> None:
        return None

    async def send(self, prompt: str, timeout_seconds: float | None = None) -> TurnResult:
        index = len(self.prompts)
        self.prompts.append(prompt)
        chunks = self._attempts[min(index, len(self._attempts) - 1)]
        turn_id = f"turn_{index}"
        # Real transports stream while generating, so every emit yields to the
        # loop once -- without an await point the delta-forwarder task would
        # never get scheduled and the repro would be unrealistically dead.
        self._emit(RequestSubmitted(turn_id=turn_id))
        await asyncio.sleep(0)
        self._emit(ResponseStarted(turn_id=turn_id))
        await asyncio.sleep(0)
        accumulated = ""
        for chunk in chunks:
            accumulated += chunk
            self._emit(ResponseDelta(text=chunk, accumulated_text=accumulated))
            await asyncio.sleep(0)
        self._emit(
            ResponseCompleted(
                turn_id=turn_id,
                text=accumulated,
                conversation_id=f"conv_web_{index}",
            )
        )
        # Give the forwarder enough loop ticks to drain everything queued by
        # this attempt (including its terminal event) before send returns.
        for _ in range(len(chunks) + 3):
            await asyncio.sleep(0)
        return TurnResult(
            turn_id=turn_id,
            conversation_id=f"conv_web_{index}",
            text="".join(chunks),
        )

    async def events(self):
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event


def _install_fake_lease(monkeypatch: pytest.MonkeyPatch, session: _ScriptedStreamSession) -> object:
    app = create_gateway_app()

    @asynccontextmanager
    async def lease(*args, affinity_key=None):
        yield session

    monkeypatch.setattr(app.state.server, "_lease_session", lease)
    monkeypatch.setattr(app.state.server.completion_runtime, "lease_session", lease)
    return app


def _sse_events(lines: list[str]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    current: str | None = None
    for line in lines:
        if line.startswith("event: "):
            current = line.removeprefix("event: ").strip()
        elif line.startswith("data: ") and current is not None:
            events.append({"event": current, "data": json.loads(line[6:])})
            current = None
    return events


async def _post_messages(app, payload: dict[str, object]) -> list[dict[str, object]]:
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://gateway") as client,
        client.stream("POST", "/v1/messages", json=payload) as response,
    ):
        assert response.status_code == 200
        lines = [line async for line in response.aiter_lines()]
    return _sse_events(lines)


def _text_deltas(events: list[dict[str, object]]) -> list[str]:
    """Ordered text_delta payloads of content block index 0."""
    out: list[str] = []
    for event in events:
        if event["event"] != "content_block_delta":
            continue
        data = event["data"]
        assert isinstance(data, dict)
        delta = data.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "text_delta" and data.get("index") == 0:
            out.append(str(delta.get("text", "")))
    return out


def _stop_reason(events: list[dict[str, object]]) -> str | None:
    for event in events:
        if event["event"] == "message_delta" and isinstance(event["data"], dict):
            delta = event["data"].get("delta")
            if isinstance(delta, dict):
                return delta.get("stop_reason")  # type: ignore[no-any-return]
    return None


def _stream_payload(chunks_per_attempt: list[list[str]]) -> dict[str, object]:
    return {
        "model": "claude-code-local",
        "max_tokens": 128,
        "stream": True,
        "tools": [BASH_TOOL_ANTHROPIC],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": USER_TASK}]}
        ],
    }


# ---------------------------------------------------------------------------
# Repro: correction between attempts duplicated the committed text on the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correction_mid_stream_does_not_duplicate_committed_text(monkeypatch):
    """Attempt 1 streams FALSE_COMPLETION prose, gets corrected, attempt 2
    commits different prose.  Every sentence must appear EXACTLY ONCE in the
    streamed content block (pre-fix the corrected text arrived twice)."""
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _ScriptedStreamSession([FALSE_COMPLETION_CHUNKS, CORRECTED_PROSE_CHUNKS])
    app = _install_fake_lease(monkeypatch, session)

    events = await _post_messages(app, _stream_payload([FALSE_COMPLETION_CHUNKS, CORRECTED_PROSE_CHUNKS]))

    event_names = [str(e["event"]) for e in events]
    assert "error" not in event_names, events
    block0 = "".join(_text_deltas(events))
    false_prose = "".join(FALSE_COMPLETION_CHUNKS).strip()
    corrected = "".join(CORRECTED_PROSE_CHUNKS).strip()

    # THE dedup contract: the committed (corrected) text is delivered once...
    assert block0.count(corrected) == 1, (
        "corrected final text duplicated on the wire:\n"
        f"count={block0.count(corrected)}\nblock0={block0!r}"
    )
    # ...and the superseded FALSE_COMPLETION prose at most once (it was
    # already delivered live before the gateway could know better).
    assert block0.count(false_prose) <= 1
    # Both texts are present: the stale one cannot be unsent, but nothing may
    # be replayed on top of it.
    assert false_prose in block0
    assert corrected in block0
    assert _stop_reason(events) == "end_turn"
    assert event_names[-1] == "message_stop"


@pytest.mark.asyncio
async def test_corrected_tool_use_after_streamed_prose_keeps_sieve_and_blocks_intact(monkeypatch):
    """Attempt 1 streams FALSE_COMPLETION prose; the corrected attempt commits
    a <cmd> tool call.  The tool call must arrive as exactly one tool_use
    block, no emit tag may leak into any text delta, and the stale prose stays
    limited to its single live delivery."""
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    cmd_chunks = ["<cmd>", "echo done > out.txt</cmd>"]
    session = _ScriptedStreamSession([FALSE_COMPLETION_CHUNKS, cmd_chunks])
    app = _install_fake_lease(monkeypatch, session)

    events = await _post_messages(app, _stream_payload([FALSE_COMPLETION_CHUNKS, cmd_chunks]))

    event_names = [str(e["event"]) for e in events]
    assert "error" not in event_names, events
    raw = json.dumps(events, ensure_ascii=False)

    tool_starts = [
        e
        for e in events
        if e["event"] == "content_block_start"
        and isinstance(e["data"], dict)
        and e["data"].get("content_block", {}).get("type") == "tool_use"
    ]
    assert len(tool_starts) == 1, raw
    assert _stop_reason(events) == "tool_use"

    all_text = "".join(_text_deltas(events))
    assert "<cmd>" not in all_text, raw  # protocol tags never leak as text
    false_prose = "".join(FALSE_COMPLETION_CHUNKS).strip()
    assert all_text.count(false_prose) <= 1
    assert event_names[-1] == "message_stop"


# ---------------------------------------------------------------------------
# Regression lock: single-attempt streaming must stay byte-identical
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_attempt_stream_is_progressive_and_byte_identical(monkeypatch):
    """Happy path (one clean attempt): each streamed chunk is forwarded
    verbatim, in order, as its own text_delta; the remainder reconciliation
    adds NOTHING on top; the SSE skeleton follows the standard Anthropic
    ordering."""
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    chunks = ["Alpha ", "beta ", "gamma"]
    session = _ScriptedStreamSession([chunks])
    app = _install_fake_lease(monkeypatch, session)

    events = await _post_messages(app, _stream_payload([chunks]))

    event_names = [str(e["event"]) for e in events]
    assert event_names[0] == "message_start"
    assert event_names[-1] == "message_stop"
    assert "error" not in event_names, events

    # Byte-for-byte progressive identity: the deltas ARE the source chunks.
    assert _text_deltas(events) == chunks

    # Standard skeleton ordering around the single text block.
    first_start = event_names.index("content_block_start")
    first_delta = event_names.index("content_block_delta")
    last_delta = len(event_names) - 1 - event_names[::-1].index("content_block_delta")
    stop_index = event_names.index("content_block_stop")
    message_delta_index = event_names.index("message_delta")
    assert first_start < first_delta <= last_delta < stop_index < message_delta_index
    assert event_names[message_delta_index + 1] == "message_stop"
    assert _stop_reason(events) == "end_turn"


# ---------------------------------------------------------------------------
# Unit: the forwarder itself must stop at the attempt boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_response_deltas_stops_at_attempt_boundary():
    """Live-delta forwarding covers ONLY the first web attempt.  Deltas after
    the terminal event of an attempt belong to a later send (correction /
    failover) and must never be forwarded."""
    session = _ScriptedStreamSession([])
    received: list[str] = []

    async def cb(text: str) -> None:
        received.append(text)

    # Enqueue a full two-attempt timeline by hand, FIFO-ordered, ending with
    # the queue sentinel so the generator terminates even pre-fix.
    session._emit(ResponseDelta(text="one ", accumulated_text="one "))
    session._emit(ResponseDelta(text="two", accumulated_text="one two"))
    session._emit(ResponseCompleted(turn_id="turn_0", text="one two"))
    session._emit(ResponseDelta(text="three ", accumulated_text="three "))
    session._emit(ResponseDelta(text="four", accumulated_text="three four"))
    session._emit(ResponseFailed(turn_id="turn_1", reason="never forwarded"))
    session._emit(None)

    await asyncio.wait_for(
        CompletionRuntime._forward_response_deltas(session, cb), timeout=5.0
    )

    assert received == ["one ", "two"], received
