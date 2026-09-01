"""Tests for the v1 (``delta_encoding``) conversation SSE stream format.

Fixtures mirror a real capture from 2026-08-15 (anon guest stream), with all
identifiers redacted. The legacy parser ignored these records entirely; the
v1-aware parser must reconstruct the assistant text, model, and completion.
"""

import json

import pytest

from gpt.reverse.stream_parser import SSEDecoder
from gpt.state import ProtocolChanged
from gpt.transport.curl_transport import CurlCffiTransport


def _records():
    return [
        "\"v1\"",  # delta_encoding value — must be tolerated
        json.dumps({"type": "resume_conversation_token", "kind": "topic",
                    "token": "<REDACTED>", "conversation_id": "<MSG_47>"}),
        # system snapshot inside an add envelope — never user-visible text
        json.dumps({"p": "", "o": "add", "v": {
            "message": {"id": "<MSG_48>", "author": {"role": "system"},
                        "content": {"content_type": "text", "parts": [""]},
                        "status": "finished_successfully", "end_turn": True,
                        "metadata": {}},
            "conversation_id": "<MSG_49>", "error": None}}),
        # user echo snapshot — likewise invisible
        json.dumps({"p": "", "o": "add", "v": {
            "message": {"id": "<MSG_52>", "author": {"role": "user"},
                        "content": {"content_type": "text",
                                    "parts": ["BQA_E_SEND_A_9C6757CF"]},
                        "status": "finished_successfully",
                        "metadata": {"resolved_model_slug": "gpt-5-6-mini"}},
            "conversation_id": "<MSG_56>", "error": None}}),
        # assistant message opens (empty parts, in_progress)
        json.dumps({"p": "", "o": "add", "v": {
            "message": {"id": "<MSG_76>", "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": [""]},
                        "status": "in_progress",
                        "metadata": {"model_slug": "gpt-5-6-mini",
                                     "default_model_slug": "auto"}},
            "conversation_id": "<CONV_X>", "error": None}}),
        # typed marker events must be inert
        json.dumps({"type": "message_marker", "conversation_id": "<CONV_X>",
                    "marker": "user_visible_token", "event": "first"}),
        # text deltas: full path form and bare-string shorthand
        json.dumps({"p": "/message/content/parts/0", "o": "append",
                    "v": "`BQA_E_SEND_A_9C6757"}),
        json.dumps({"v": "CF` looks like an identifier or reference code. What would you like me"}),
        # patch batch: final text append + status flip + metadata noise
        json.dumps({"p": "", "o": "patch", "v": [
            {"p": "/message/content/parts/0", "o": "append", "v": " to do with it?"},
            {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
            {"p": "/message/end_turn", "o": "replace", "v": True},
            {"p": "/message/metadata", "o": "append",
             "v": {"is_complete": True}},
        ]}),
        json.dumps({"type": "server_ste_metadata",
                    "metadata": {"model_slug": "gpt-5-6-mini",
                                 "cluster_region": "japaneast",
                                 "plan_type": "guest"},
                    "conversation_id": "<CONV_X>"}),
        json.dumps({"type": "message_stream_complete", "conversation_id": "<CONV_X>"}),
        "[DONE]",
    ]


def _feed_all(records):
    SSEDecoder()
    state = {"text": "", "turn_id": "turn_x", "conversation_id": None, "model": None}
    deltas = []
    complete = False
    for record in records:
        payload_record = record
        (
            state["text"],
            state["turn_id"],
            state["conversation_id"],
            state["model"],
            is_complete,
            delta,
        ) = CurlCffiTransport._consume_record(
            payload_record,
            state["text"],
            state["turn_id"],
            state["conversation_id"],
            state["model"],
        )
        if delta:
            deltas.append(delta)
        complete = complete or is_complete
    return deltas, complete, state


def test_v1_stream_reconstructs_assistant_text_and_completion():
    deltas, complete, state = _feed_all(_records())

    assert "".join(deltas) == (
        "`BQA_E_SEND_A_9C6757"
        "CF` looks like an identifier or reference code. What would you like me"
        " to do with it?"
    )
    assert complete
    assert state["text"] == "".join(deltas)
    assert state["model"] == "gpt-5-6-mini"


def test_v1_stream_ignores_system_and_user_snapshots():
    # Stop immediately after the user-echo snapshot.  The eventual assistant
    # answer is allowed to quote the user's identifier, so string absence in
    # the final answer is not a valid oracle for role filtering.
    _, complete, state = _feed_all(_records()[:4])

    assert state["text"] == ""
    assert not complete


def test_sse_decoder_passes_event_lines_through_to_records():
    raw = (
        'event: delta_encoding\ndata: "v1"\n\n'
        'event: delta\ndata: {"p":"","o":"add","v":{"message":null}}\n\n'
        "data: [DONE]\n\n"
    )
    records = SSEDecoder().feed(raw.encode("utf-8"))
    assert len(records) == 3
    assert records[0] == '"v1"'
    assert json.loads(records[1])["o"] == "add"


def test_legacy_format_still_parses_after_v1_change():
    legacy = json.dumps({
        "message": {
            "id": "legacy_turn",
            "author": {"role": "assistant"},
            "content": {"content_type": "text", "parts": ["hello"]},
            "status": "finished_successfully",
            "metadata": {"model_slug": "gpt-5-5"},
        },
        "conversation_id": "conv1",
    })
    text, turn_id, conversation_id, model, complete, delta = (
        CurlCffiTransport._consume_record(legacy, "", "t0", None, None)
    )
    assert (text, turn_id, conversation_id, model, complete, delta) == (
        "hello", "legacy_turn", "conv1", "gpt-5-5", True, "hello"
    )


def test_error_event_still_raises_protocol_changed():
    with pytest.raises(ProtocolChanged):
        CurlCffiTransport._consume_record(
            json.dumps({"error": {"message": "boom"}}), "", "t", None, None
        )


async def test_full_stream_via_decoder_collects_deltas_in_order():
    decoder = SSEDecoder()
    raw = "".join(f"data: {record}\n\n" for record in _records())
    collected = []
    text = ""
    complete = False
    for record in decoder.feed(raw):
        new_text, _, _, _, is_complete, delta = CurlCffiTransport._consume_record(
            record, text, "turn_x", None, None
        )
        if delta:
            collected.append(delta)
        text = new_text
        complete = complete or is_complete
    assert complete
    assert "".join(collected) == text
    assert text.endswith("to do with it?")
