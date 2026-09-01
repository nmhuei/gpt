"""LIVE-F4 stream hygiene tests: channel filter, duplicate collapse, prefix cut.

Evidence 2026-08-24: the web path leaked the thinking/draft channel into the
assistant text and doubled text when reconcile re-delivered draft + final of
the same message.  Each heuristic here has an env kill switch and a
false-positive guard case.
"""

import json

import pytest

from gpt.transport.curl_transport import CurlCffiTransport
from gpt.types import SendRequest


def _add_envelope(message, conversation_id="<CONV_X>"):
    return json.dumps({"p": "", "o": "add", "v": {
        "message": message, "conversation_id": conversation_id, "error": None}})


def _assistant_message(parts, *, channel=None, status="in_progress",
                       msg_id="<MSG_A>", model_slug="gpt-5-6-mini"):
    message = {
        "id": msg_id,
        "author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": parts},
        "status": status,
        "metadata": {"model_slug": model_slug},
    }
    if channel is not None:
        message["channel"] = channel
    return message


def _consume_all(records):
    state = {"text": "", "turn_id": "turn_x", "conversation_id": None, "model": None}
    deltas = []
    complete = False
    for record in records:
        (
            state["text"],
            state["turn_id"],
            state["conversation_id"],
            state["model"],
            is_complete,
            delta,
        ) = CurlCffiTransport._consume_record(
            record,
            state["text"],
            state["turn_id"],
            state["conversation_id"],
            state["model"],
        )
        if delta:
            deltas.append(delta)
        complete = complete or is_complete
    return deltas, complete, state


# ---------------------------------------------------------------------------
# (a) Channel filter: non-final channels must never contribute text.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["draft", "thinking", "tool_draft"])
def test_non_final_channel_snapshot_is_metadata_only(channel):
    draft_record = _add_envelope(
        _assistant_message(["DRAFT NOISE must not leak"], channel=channel)
    )
    deltas, _, state = _consume_all([draft_record, json.dumps({"v": "Real"})])

    assert "".join(deltas) == "Real"
    assert state["text"] == "Real"
    # Metadata from the draft snapshot is still absorbed.
    assert state["turn_id"] == "<MSG_A>"
    assert state["model"] == "gpt-5-6-mini"


def test_draft_channel_never_signals_completion():
    draft_record = _add_envelope(
        _assistant_message(["done-looking draft"], channel="draft",
                           status="finished_successfully")
    )
    _, complete, _ = _consume_all([draft_record])
    assert not complete


def test_final_channel_snapshot_still_carries_text():
    final_record = _add_envelope(
        _assistant_message(["Final answer."], channel="final",
                           status="finished_successfully")
    )
    _deltas, complete, state = _consume_all([final_record])
    assert state["text"] == "Final answer."
    assert complete


def test_snapshot_without_channel_keeps_legacy_behavior():
    legacy_like = _add_envelope(_assistant_message(["no channel field"]))
    _, _, state = _consume_all([legacy_like])
    assert state["text"] == "no channel field"


# ---------------------------------------------------------------------------
# (b) Anti-duplicate: single-event structural doubling collapses to one copy.
# ---------------------------------------------------------------------------


def test_doubled_final_snapshot_collapses_to_single_copy():
    seed = _add_envelope(_assistant_message(["Say hi"], channel="final"))
    double = _add_envelope(
        _assistant_message(["Say hiSay hi"], channel="final",
                           status="finished_successfully")
    )
    deltas, _, state = _consume_all([seed, double])
    assert state["text"] == "Say hi"
    assert "".join(deltas) == "Say hi"


def test_repeated_reconcile_appends_collapse():
    # Draft accumulated via small deltas, then one event re-delivers the
    # whole draft text again ("final lặp lại draft y hệt").
    draft = "Answer part one. "
    records = [
        _add_envelope(_assistant_message([], channel="final")),
        json.dumps({"v": draft}),
        json.dumps({"p": "/message/content/parts/0", "o": "append", "v": draft}),
    ]
    deltas, _, state = _consume_all(records)
    assert state["text"] == draft
    assert "".join(deltas) == draft


def test_small_growth_is_never_collapsed():
    # Growth below the 90% single-event gate must survive untouched even when
    # the candidate happens to start with the previous text.
    old = "0123456789"  # len 10; extra of 9 chars is exactly at the gate -> kept
    seed = _add_envelope(_assistant_message([old], channel="final"))
    grow = _add_envelope(_assistant_message([old + "012345678"], channel="final"))
    _, _, state = _consume_all([seed, grow])
    assert state["text"] == old + "012345678"


def test_legit_repetition_across_small_deltas_survives():
    # Legitimate repetition arrives as small suffix deltas, never as one
    # event re-delivering the ENTIRE previous text — so it never fires.
    records = [_add_envelope(_assistant_message([], channel="final"))]
    records += [json.dumps({"v": chunk}) for chunk in
                ["Line one.\n", "Line two.\n", "Line one again.\n"]]
    _, _, state = _consume_all(records)
    assert state["text"] == "Line one.\nLine two.\nLine one again.\n"

    # Character-level "ha ha ha!" also survives: no single event carries a
    # verbatim copy of everything before it.
    records = [_add_envelope(_assistant_message([], channel="final"))]
    records += [json.dumps({"v": c}) for c in "ha ha ha!"]
    _, _, state = _consume_all(records)
    assert state["text"] == "ha ha ha!"


def test_dedupe_flag_off_restores_raw_behavior(monkeypatch):
    monkeypatch.setenv("WEBGPT_STREAM_DEDUPE", "0")
    seed = _add_envelope(_assistant_message([], channel="final"))
    double = _add_envelope(
        _assistant_message(["Say hiSay hi"], channel="final")
    )
    deltas, _, state = _consume_all([seed, double])
    assert state["text"] == "Say hiSay hi"  # raw pre-F4 behavior
    assert "".join(deltas) == "Say hiSay hi"


# ---------------------------------------------------------------------------
# (c) Leading noise-prefix strip (stream level, once).
# ---------------------------------------------------------------------------


class _FakeBundle:
    is_local_mock = False
    cookies: dict[str, str] = {}  # noqa: RUF012
    access_token = "tok"
    cf_clearance = "cf"
    oai_device_id = "dev"


class _FakeSentinel:
    requirements_token = "req"
    proof_token = None
    turnstile_token = None


class _FakeTokenManager:
    async def refresh_if_needed(self):
        return _FakeBundle()

    async def get_sentinel_tokens(self, conversation_id):
        return _FakeSentinel()

    def invalidate_sentinel(self):
        pass


class _FakeResponse:
    status_code = 200

    def __init__(self, records):
        self._raw = "".join(f"data: {r}\n\n" for r in records).encode("utf-8")

    async def aiter_bytes(self):
        yield self._raw

    async def aclose(self):
        pass


class _FakeSession:
    def __init__(self, response):
        self._response = response

    async def post(self, url, **kwargs):
        return self._response


def _thinking_stream_records():
    return [
        _add_envelope(_assistant_message([], channel="final")),
        json.dumps({"v": "Thinking\n"}),
        json.dumps({"v": "42 is the answer."}),
        json.dumps({"p": "/message/status", "o": "replace",
                    "v": "finished_successfully"}),
        json.dumps({"type": "message_stream_complete", "conversation_id": "<C>"}),
        "[DONE]",
    ]


async def test_thinking_prefix_cut_across_stream_and_deltas():
    transport = CurlCffiTransport(
        _FakeTokenManager(), session=_FakeSession(_FakeResponse(_thinking_stream_records()))
    )
    collected: list[str] = []

    async def on_delta(delta, turn_id):
        collected.append(delta)

    result = await transport.send(SendRequest(text="hi"), on_delta=on_delta)
    assert result.text == "42 is the answer."
    assert "".join(collected) == "42 is the answer."


def test_thought_prefix_variant_cut_at_unit_level():
    stripped, done = CurlCffiTransport._strip_leading_noise("Thought: the answer\nnext")
    assert stripped == "the answer\nnext"
    assert done


def test_partial_noise_word_waits_for_more_bytes_then_decides():
    # "Thi" alone is ambiguous -> pending; once it clearly is not noise, keep.
    stripped, done = CurlCffiTransport._strip_leading_noise("Thi")
    assert (stripped, done) == ("Thi", False)


# ---------------------------------------------------------------------------
# (d) False-positive guards: legitimate text must pass untouched.
# ---------------------------------------------------------------------------


async def test_lowercase_thinking_mid_sentence_not_cut():
    records = [
        _add_envelope(_assistant_message([], channel="final")),
        json.dumps({"v": "I was thinking about it.\nAnswer: 4."}),
        json.dumps({"p": "/message/status", "o": "replace",
                    "v": "finished_successfully"}),
        "[DONE]",
    ]
    transport = CurlCffiTransport(
        _FakeTokenManager(), session=_FakeSession(_FakeResponse(records))
    )
    result = await transport.send(SendRequest(text="hi"))
    assert result.text == "I was thinking about it.\nAnswer: 4."


async def test_prose_starting_with_word_thinking_not_cut():
    # "Thinking" followed by prose (no newline) is real content, not the
    # noise prefix — only "Thinking\\n" / standalone "Thinking" are cut.
    records = [
        _add_envelope(_assistant_message([], channel="final")),
        json.dumps({"v": "Thinking about it carefully, the answer is 4."}),
        json.dumps({"p": "/message/status", "o": "replace",
                    "v": "finished_successfully"}),
        "[DONE]",
    ]
    transport = CurlCffiTransport(
        _FakeTokenManager(), session=_FakeSession(_FakeResponse(records))
    )
    result = await transport.send(SendRequest(text="hi"))
    assert result.text == "Thinking about it carefully, the answer is 4."


# ---------------------------------------------------------------------------
# (e) Kill switches restore the pre-F4 behavior.
# ---------------------------------------------------------------------------


async def test_strip_prefix_flag_off_keeps_noise(monkeypatch):
    monkeypatch.setenv("WEBGPT_STREAM_STRIP_PREFIX", "0")
    transport = CurlCffiTransport(
        _FakeTokenManager(), session=_FakeSession(_FakeResponse(_thinking_stream_records()))
    )
    result = await transport.send(SendRequest(text="hi"))
    assert result.text == "Thinking\n42 is the answer."
