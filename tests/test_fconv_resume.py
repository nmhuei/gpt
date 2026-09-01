"""FCONV-RESUME-HANDOFF — split-stream resume follow (fake session, no live).

Covers (spec docs/reports/sse-resume-research-2026-08-26.md row M):
  * ``resume_conversation_token`` captured when WEBGPT_FCONV_RESUME is ON and
    the handoff POST /backend-api/f/conversation/resume is issued with
    X-Conduit-Token set to the captured token;
  * contiguous streams ([DONE], no token) are never followed;
  * offsets advance 0->1->2 ONLY on 404;
  * the follow chain is capped at 64 handoffs per turn;
  * OFF keeps today's behavior byte-for-byte: the event is dropped silently
    and no resume POST ever leaves the process.

All traffic here terminates in scripted fake responses — nothing touches the
network.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from gpt.transport.curl_transport import (
    _FCONV_RESUME_FLAG,
    CONVERSATION_URL,
    CurlCffiTransport,
)
from gpt.types import SendRequest

RESUME_URL = CONVERSATION_URL + "/resume"
CONV = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ENVELOPE = {
    "X-Conduit-Token": "prepare-conduit-token",
    "Authorization": "Bearer access",
    "x-oai-turn-trace-id": "trace-turn-1",
}


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class StubTokenManager:
    """Never touched by _stream_sse; satisfies __init__ only."""


class FakeSegment:
    """One scripted HTTP response: SSE records plus a status code."""

    def __init__(self, records, status_code=200):
        self.status_code = status_code
        self._records = records

    async def aiter_bytes(self):
        for record in self._records:
            yield f"data: {record}\n\n".encode()

    async def aclose(self):
        pass


class ScriptedSession:
    """session.post double: pops scripted responses in order, logs calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict, dict]] = []

    async def post(self, url, **kwargs):
        assert self.responses, "script ran dry: an unexpected extra POST happened"
        self.calls.append((url, kwargs.get("headers") or {}, kwargs.get("json") or {}))
        return self.responses.pop(0)


def _transport(session) -> CurlCffiTransport:
    return CurlCffiTransport(cast(Any, StubTokenManager()), session=session)


def _request() -> SendRequest:
    return SendRequest(text="hi", conversation_id=CONV)


def _delta(text: str) -> str:
    return json.dumps({"v": text})


def _resume_event(token: str, conversation_id: str = CONV) -> str:
    return json.dumps(
        {
            "type": "resume_conversation_token",
            "kind": "topic",
            "token": token,
            "conversation_id": conversation_id,
        }
    )


DONE = "[DONE]"


async def _run(session, records):
    deltas: list[str] = []
    result = await _transport(session)._stream_sse(
        FakeSegment(records),
        _request(),
        on_delta=lambda delta, turn_id: deltas.append(delta),
        envelope_headers=dict(ENVELOPE),
    )
    return result, deltas


def _resume_calls(session) -> list[tuple[str, dict, dict]]:
    return [call for call in session.calls if call[0] == RESUME_URL]


# ---------------------------------------------------------------------------
# (a) ON: event captured, handoff followed, continuity preserved
# ---------------------------------------------------------------------------


async def test_resume_token_captured_and_followed_when_on(monkeypatch):
    monkeypatch.setenv(_FCONV_RESUME_FLAG, "1")
    session = ScriptedSession(
        [
            FakeSegment([_delta("world"), DONE]),
        ]
    )
    result, deltas = await _run(
        session,
        [_delta("Hello "), _resume_event("tok-1"), DONE],
    )

    calls = _resume_calls(session)
    assert len(calls) == 1
    url, headers, body = calls[0]
    assert url == RESUME_URL
    # The captured resume token REPLACES the prepare conduit token on the
    # handoff; the rest of the turn envelope rides along untouched.
    assert headers["X-Conduit-Token"] == "tok-1"
    assert headers["x-oai-turn-trace-id"] == "trace-turn-1"
    assert headers["Authorization"] == "Bearer access"
    assert body == {"conversation_id": CONV, "offset": 0}

    assert result.text == "Hello world"
    assert "".join(deltas) == result.text  # decoder continuity: order kept
    assert result.status == "completed"
    assert result.metadata["fconv_resume"]["hops"] == 1
    assert result.metadata["fconv_resume"]["token"] == "tok-1"


async def test_offsets_advance_only_on_404(monkeypatch):
    monkeypatch.setenv(_FCONV_RESUME_FLAG, "1")
    session = ScriptedSession(
        [
            FakeSegment([], status_code=404),          # offset 0 -> retry
            FakeSegment([_delta(" continued"), DONE]),  # offset 1 -> streams
        ]
    )
    result, deltas = await _run(
        session,
        [_delta("part one"), _resume_event("tok-off"), DONE],
    )

    bodies = [body for _, _, body in _resume_calls(session)]
    assert bodies == [
        {"conversation_id": CONV, "offset": 0},
        {"conversation_id": CONV, "offset": 1},
    ]
    assert result.text == "part one continued"
    assert "".join(deltas) == result.text


async def test_non_404_refusal_stops_chain_without_failing_turn(monkeypatch):
    monkeypatch.setenv(_FCONV_RESUME_FLAG, "1")
    session = ScriptedSession(
        [
            FakeSegment(["server exploded"], status_code=403),
        ]
    )
    result, _deltas = await _run(
        session,
        [_delta("kept"), _resume_event("tok-x"), DONE],
    )

    # 403 is not retried with the next offset; the turn stands on segment one.
    assert len(_resume_calls(session)) == 1
    assert result.text == "kept"
    assert result.status == "completed"
    assert result.metadata["fconv_resume"]["hops"] == 1


async def test_repeated_token_ends_chain_loop_guard(monkeypatch):
    monkeypatch.setenv(_FCONV_RESUME_FLAG, "1")
    session = ScriptedSession(
        [
            FakeSegment([_delta(" more"), _resume_event("tok-same"), DONE]),
        ]
    )
    result, _deltas = await _run(
        session,
        [_delta("base"), _resume_event("tok-same"), DONE],
    )

    # The resumed segment re-offers the SAME handle: following it again would
    # loop forever, so the chain ends after this single hop.
    assert len(_resume_calls(session)) == 1
    assert result.text == "base more"


# ---------------------------------------------------------------------------
# (b) contiguous streams and the 64-follow cap
# ---------------------------------------------------------------------------


async def test_contiguous_stream_is_never_followed(monkeypatch):
    monkeypatch.setenv(_FCONV_RESUME_FLAG, "1")
    session = ScriptedSession([])  # any POST would fail the "ran dry" assert
    result, deltas = await _run(
        session,
        [_delta("whole "), _delta("stream"), DONE],
    )

    assert session.calls == []
    assert result.text == "whole stream"
    assert "".join(deltas) == result.text
    assert result.metadata == {}


async def test_follow_chain_caps_at_64_handoffs(monkeypatch):
    monkeypatch.setenv(_FCONV_RESUME_FLAG, "1")
    # Every continuation offers yet another fresh handle — an endless split
    # stream.  The cap, not the script, must end the chain.
    responses = [
        FakeSegment([_delta("."), _resume_event(f"tok-{n}"), DONE])
        for n in range(1, 80)
    ]
    session = ScriptedSession(responses)
    result, _deltas = await _run(
        session,
        [_delta("start"), _resume_event("tok-0"), DONE],
    )

    assert len(_resume_calls(session)) == 64
    _last_url, last_headers, last_body = _resume_calls(session)[-1]
    assert last_headers["X-Conduit-Token"] == "tok-63"
    assert last_body["offset"] == 0
    assert result.metadata["fconv_resume"]["hops"] == 64


# ---------------------------------------------------------------------------
# (c) OFF: legacy drop-in-place behavior, byte for byte
# ---------------------------------------------------------------------------


async def test_flag_off_drops_event_and_never_posts(monkeypatch):
    monkeypatch.delenv(_FCONV_RESUME_FLAG, raising=False)
    session = ScriptedSession(
        [
            FakeSegment([_delta("NEVER STREAMED"), DONE]),
        ]
    )
    result, deltas = await _run(
        session,
        [_delta("legacy "), _resume_event("tok-off-path"), DONE],
    )

    assert session.calls == []  # no resume POST ever leaves the process
    assert result.text == "legacy "
    assert "".join(deltas) == result.text
    assert result.metadata == {}
    assert "NEVER STREAMED" not in result.text


def test_consume_record_captures_only_when_capture_dict_given():
    record = _resume_event("tok-unit", conversation_id="conv-unit")

    capture: dict[str, str] = {}
    text, turn_id, conversation_id, model, complete, delta = (
        CurlCffiTransport._consume_record(record, "", "t0", None, None, capture=capture)
    )
    assert (text, turn_id, conversation_id, model, complete, delta) == (
        "", "t0", None, None, False, ""
    )
    assert capture == {"token": "tok-unit", "conversation_id": "conv-unit"}

    # Legacy positional callers (no capture arg): silent drop, unchanged tuple.
    legacy = CurlCffiTransport._consume_record(record, "", "t0", None, None)
    assert legacy == ("", "t0", None, None, False, "")


@pytest.mark.parametrize("value", ["0", "", "false", "off"])
def test_flag_truthiness_default_off(monkeypatch, value):
    monkeypatch.setenv(_FCONV_RESUME_FLAG, value)
    assert CurlCffiTransport._fconv_resume_enabled() is False
    monkeypatch.delenv(_FCONV_RESUME_FLAG, raising=False)
    assert CurlCffiTransport._fconv_resume_enabled() is False
