"""LIVE-T3 regression: false-completion prose from Claude Code CLI requests.

Live verification (docs/reports/live-cli-verify-2026-08-24.md) showed T3 fail
twice: the CLI sent its full tool definitions (Bash/Read/Write/...) with a
Vietnamese multi-step task, the web model answered in pure prose claiming the
work was already done ("Đã tạo và chạy script fizzbuzz.py..."), and the
correction loop never fired -- the CLI received the prose as a successful final
answer while no file existed.

Root cause: ``_tool_correction_issue`` returned None because
(a) every task heuristic in ``_looks_like_tool_directed_task`` was English-only,
    so the Vietnamese task was not classified tool-directed;
(b) neither hard nor soft refusal markers matched -- the prose claims success
    instead of declining;
(c) plumbing was NOT the problem: stream and non-stream paths both run the
correction loop inside ``CompletionRuntime.execute_raw_on_session``.

Fix under test here:
- layer 3 ``_looks_like_action_claim_prose``: multilingual claims of completed
  work in a fresh tool conversation classify as FALSE_COMPLETION regardless of
  the user's task language;
- Vietnamese task markers extend ``_looks_like_tool_directed_task``;
- both stream (stream_callback) and non-stream runtime paths correct the prose;
- the P0-2 budget / early-raise mechanism stays intact (MalformedToolCall -> 502).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from gpt.api.server import create_api_app
from gpt.conversations import ConversationRecord
from gpt.gateway.runtime import (
    CompletionRuntime,
    _correction_prompt_for,
    _fresh_tool_conversation,
    _looks_like_action_claim_prose,
    _looks_like_tool_directed_task,
    _tool_correction_issue,
)
from gpt.state import MalformedToolCall
from gpt.types import ResponseDelta, TurnResult

# ---------------------------------------------------------------------------
# Fixtures mirroring the live T3 CLI payload
# ---------------------------------------------------------------------------

CLI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object"},
        },
    }
    for name in ("Agent", "Bash", "Edit", "Read", "Write")
]

CLI_TASK_TEXT = (
    "<system-reminder>\n"
    "This is a reminder that your todo list is currently empty.\n"
    "Working directory: /tmp/cc-live-test\n"
    "</system-reminder>\n"
    "\n"
    "# Task\n"
    "Tạo script python `fizzbuzz.py` in các số 1..15 theo luật FizzBuzz "
    "(chia hết cho 3 -> \"Fizz\", chia hết cho 5 -> \"Buzz\", cả hai -> "
    "\"FizzBuzz\", còn lại in số).\n"
    "Chạy script đó, và ghi output vào file `output.txt`.\n"
)

# Real shape: Anthropic content blocks, as sent by claude-cli/2.1.241.
CLI_MESSAGES = [
    {
        "role": "user",
        "content": [{"type": "text", "text": CLI_TASK_TEXT}],
    }
]

T3_PROSE_A = (
    "Đã tạo fizzbuzz.py, chạy script và ghi kết quả vào output.txt.\n\n"
    "Nội dung output.txt:\n\n1\n2\nFizz\n4\nBuzz\n\n"
    "Bạn muốn tiếp tục theo hướng Python cơ bản hay CTF/script automation?"
)
T3_PROSE_B = (
    "Đã tạo và chạy script fizzbuzz.py, kết quả đã được ghi vào output.txt.\n\n"
    "Output:\n\n1\n2\nFizz\n4\nBuzz\nFizz\n7\n8\nFizz\nBuzz\n11\nFizz\n13\n14\nFizzBuzz"
)

VALID_WRITE_BLOCK = (
    "<tool_calls>\n"
    '  <invoke name="Write">\n'
    '    <parameter name="file_path"><![CDATA[fizzbuzz.py]]></parameter>\n'
    '    <parameter name="lines"><![CDATA[print("fizzbuzz")]]></parameter>\n'
    "  </invoke>\n"
    "</tool_calls>"
)


def _issue(text, *, messages=CLI_MESSAGES, tools=CLI_TOOLS, tail=None):
    return _tool_correction_issue(
        text,
        tail=[] if tail is None else tail,
        messages=messages,
        tools=tools,
        tool_choice=None,
    )


# ---------------------------------------------------------------------------
# Layer 3 unit coverage: claim-based false completion detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [T3_PROSE_A, T3_PROSE_B])
def test_live_t3_prose_is_now_classified_false_completion(text):
    issue = _issue(text)
    assert issue is not None
    reason, detail = issue
    assert reason == "FALSE_COMPLETION"
    assert "without any controller tool call" in detail


@pytest.mark.parametrize(
    "text",
    [
        "I've created and run fizzbuzz.py successfully.",
        "I've written the results to output.txt. Here is the output: ...",
        "The file has been created and the script ran.",
    ],
)
def test_english_false_completion_claims_are_also_caught(text):
    assert _looks_like_action_claim_prose(text)
    assert _issue(text)[0] == "FALSE_COMPLETION"


def test_vietnamese_task_text_now_counts_as_tool_directed():
    assert _looks_like_tool_directed_task([], CLI_MESSAGES, CLI_TOOLS) is True


def test_claim_layer_requires_fresh_tool_conversation():
    assert _fresh_tool_conversation(CLI_MESSAGES, []) is True
    transcript_with_results = [*CLI_MESSAGES, {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "Bash", "arguments": "{}"}}]}, {"role": "tool", "content": "ok"}]
    # After real tool execution the identical prose is a legitimate summary.
    assert _fresh_tool_conversation(transcript_with_results, []) is False
    assert (
        _issue(
            T3_PROSE_B,
            messages=transcript_with_results,
            tail=[transcript_with_results[-1]],
        )
        is None
    )


def test_conceptual_vietnamese_question_stays_unflagged():
    messages = [
        {"role": "user", "content": "Giải thích FizzBuzz là gì và dùng để làm gì?"}
    ]
    text = "FizzBuzz là một bài tập lập trình kinh điển để luyện điều kiện rẽ nhánh."
    assert _issue(text, messages=messages) is None


def test_correction_prompt_forbids_prose_descriptions():
    prompt = _correction_prompt_for(
        "FALSE_COMPLETION", CLI_TOOLS, None, detail="detail"
    )
    assert "Return ONLY one valid tool call block" in prompt
    assert "does NOT count" in prompt


# ---------------------------------------------------------------------------
# Integration (non-stream HTTP): the exact live T3 scenario over the API
# ---------------------------------------------------------------------------


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    return session


def _turn(text: str, turn_id: str) -> TurnResult:
    return TurnResult(turn_id=turn_id, conversation_id="conv_t3_live", text=text)


def _post_completion(monkeypatch, side_effect):
    app = create_api_app(headless=True)
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(side_effect=list(side_effect))
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))
    return app, server, session


def test_t3_prose_receives_correction_and_second_send_happens(monkeypatch):
    """The core live-verify gate: T3 prose must trigger a second model turn."""
    app, server, session = _post_completion(
        monkeypatch,
        [_turn(T3_PROSE_B, "turn_t3_prose"), _turn(VALID_WRITE_BLOCK, "turn_t3_fixed")],
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": CLI_MESSAGES,
                "tools": CLI_TOOLS,
            },
        )

    assert response.status_code == 200
    calls = response.json()["choices"][0]["message"]["tool_calls"]
    # The Write invoke is transpiled to the gateway's virtual Write adapter
    # (a Bash-shaped call carrying file_path/lines); either way the committed
    # answer is an executable tool call, never the fabricated-success prose.
    assert calls
    assert calls[0]["function"]["name"] in {"Write", "Bash"}
    assert "fizzbuzz.py" in calls[0]["function"]["arguments"]
    # The prose turn must NOT be delivered as a final answer: exactly one
    # correction round happened before the valid tool block.
    assert session.send.await_count == 2
    correction_prompt = session.send.call_args_list[1].args[0]
    assert "WEBGPT CONTROLLER CORRECTION" in correction_prompt
    assert "does NOT count" in correction_prompt
    reasons = [
        event.metadata.get("reason")
        for event in server.trace.snapshot()
        if event.component == "completionruntime"
        and event.kind == "tool_correction"
    ]
    assert reasons == ["FALSE_COMPLETION"]


def test_t3_prose_forever_raises_malformed_after_budget(monkeypatch):
    """If the model keeps claiming success, the client gets a clean 502 --
    never the fabricated-success prose."""
    app, _server, session = _post_completion(
        monkeypatch,
        [
            _turn(T3_PROSE_A, "turn_p0"),
            _turn(T3_PROSE_B, "turn_p1"),
            _turn(T3_PROSE_A, "turn_p2"),
        ],
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": CLI_MESSAGES,
                "tools": CLI_TOOLS,
            },
        )

    assert response.status_code == 502
    payload = response.json()["error"]
    assert payload["code"] == "malformed_model_tool_call"
    assert "FALSE_COMPLETION" in payload["message"]
    # Initial response + exactly WEBGPT_MAX_CORRECTIONS (default 2) rounds.
    assert session.send.await_count == 3


# ---------------------------------------------------------------------------
# Runtime-level parity: the streaming path corrects prose too
# ---------------------------------------------------------------------------


def _runtime_with_session(session: MagicMock) -> CompletionRuntime:
    @asynccontextmanager
    async def lease(*_args, **_kwargs):
        yield session

    return CompletionRuntime(conversations=MagicMock(), lease_session=lease)


@pytest.mark.asyncio
async def test_stream_path_also_corrects_prose_before_finishing():
    """stream_callback (live SSE) responses go through the same correction loop."""
    session = _fake_session()
    turns = [_turn(T3_PROSE_B, "turn_stream_prose"), _turn(VALID_WRITE_BLOCK, "turn_stream_fixed")]
    sends: list[str] = []

    async def send(prompt, timeout_seconds=None):
        sends.append(prompt)
        # Yield control so the live-delta forwarder can observe this turn.
        await asyncio.sleep(0)
        return turns[len(sends) - 1]

    session.send = send

    def events():
        async def gen():
            # Mirror the real browser transport: stream each committed turn's
            # text as deltas while it becomes available.
            seen = 0
            while True:
                for turn in turns[seen : len(sends)]:
                    yield ResponseDelta(text=turn.text)
                    seen += 1
                await asyncio.sleep(0.01)

        return gen()

    session.events = events
    runtime = _runtime_with_session(session)
    record = ConversationRecord()

    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    result, _prompt = await runtime.execute_raw_on_session(
        session,
        record,
        tail=CLI_MESSAGES,
        messages=CLI_MESSAGES,
        model="claude-3-5-sonnet",
        ui_model=None,
        tools=CLI_TOOLS,
        tool_choice=None,
        stream_callback=on_delta,
    )

    # Both sends happened: prose corrected, then the real tool block returned.
    assert len(sends) == 2
    correction_prompt = sends[1]
    assert "WEBGPT CONTROLLER CORRECTION" in correction_prompt
    assert "does NOT count" in correction_prompt
    assert "<tool_calls>" in result.text
    # Live SSE deltas of the rejected prose were forwarded while streaming, but
    # the committed turn is the corrected tool call.
    assert T3_PROSE_B in "".join(deltas)


@pytest.mark.asyncio
async def test_stream_path_exhausted_budget_raises_malformed():
    session = _fake_session()
    session.send = AsyncMock(side_effect=lambda *a, **k: None)  # replaced below
    session.send = AsyncMock(
        side_effect=[
            _turn(T3_PROSE_A, "s0"),
            _turn(T3_PROSE_B, "s1"),
            _turn(T3_PROSE_A, "s2"),
        ]
    )
    runtime = _runtime_with_session(session)
    record = ConversationRecord()

    with pytest.raises(MalformedToolCall) as excinfo:
        await runtime.execute_raw_on_session(
            session,
            record,
            tail=CLI_MESSAGES,
            messages=CLI_MESSAGES,
            model="claude-3-5-sonnet",
            ui_model=None,
            tools=CLI_TOOLS,
            tool_choice=None,
            stream_callback=None,
        )

    assert "FALSE_COMPLETION" in str(excinfo.value)
    assert session.send.await_count == 3
