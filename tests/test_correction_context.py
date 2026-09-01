"""LIVE-R3 follow-up fixes (docs/reports/live-cli-verify-round3-2026-08-24.md).

Round 3 showed T2/T3 red with a diagnosed root cause:

1. Correction prompts were sent with ``tail_messages: 0`` onto a fresh web
   thread -- the model received a "continue the current user task" command
   WITHOUT the task, so its short first response (~20 chars, e.g. a
   counter-question) was reasonable behavior, not stubbornness.  Fix under
   test: every correction prompt embeds an "ORIGINAL USER TASK (for context)"
   section extracted from the transcript's last user turn.
2. Raw model responses were stored nowhere (prompt-debug only captured what
   was SENT; trace only recorded ``assistant_chars``), making MALFORMED_TOOL
   loops undiagnosable.  Fix: ``<seq>_<session_id>_response.txt`` + ``.json``
   dumps written next to the prompt dumps whenever WEBGPT_PROMPT_DEBUG_DIR /
   ``prompt_debug_dir`` is configured, fail-safe when it is not.
3. Identical adjacent hard failures (TOOL_REFUSAL -> TOOL_REFUSAL or
   MALFORMED_TOOL -> MALFORMED_TOOL) now raise MalformedToolCall immediately
   instead of draining the whole correction budget (round 3 burned 37
   generations across SDK retries while never producing a tool call).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from gpt.api.server import create_api_app
from gpt.conversations import ConversationRecord
from gpt.gateway.runtime import (
    CompletionRuntime,
    _correction_prompt_for,
    _original_task_context,
)
from gpt.state import MalformedToolCall
from gpt.types import TurnResult

CLI_TOOLS = [
    {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object"}},
    }
    for name in ("Agent", "Bash", "Edit", "Read", "Write")
]

R3_TASK_TEXT = (
    "# Task\n"
    "Tạo script python `fizzbuzz.py` in các số 1..15 theo luật FizzBuzz.\n"
    "Chạy script đó, và ghi output vào file `output.txt`.\n"
)

# Exact R3 shape: fresh conversation, single user turn, system-reminder wrapper.
R3_MESSAGES = [
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "<system-reminder>\nWorking directory: /tmp/cc-live-test3\n"
                    "</system-reminder>\n\n" + R3_TASK_TEXT
                ),
            }
        ],
    }
]

COUNTER_QUESTION_TEXT = (
    "Bạn muốn tôi tiếp tục phần nào? Could you tell me more about the task?"
)

VALID_WRITE_BLOCK = (
    "<tool_calls>\n"
    '  <invoke name="Write">\n'
    '    <parameter name="file_path"><![CDATA[fizzbuzz.py]]></parameter>\n'
    '    <parameter name="lines"><![CDATA[print("fizzbuzz")]]></parameter>\n'
    "  </invoke>\n"
    "</tool_calls>"
)

# Open/close tags present but the invoke cannot be parsed -> MALFORMED_TOOL
# (mirrors round 3's long tag-bearing-but-unparseable responses).
MALFORMED_BLOCK = (
    "<tool_calls>\n"
    '  <invoke name="Bash">\n'
    "    <parameter>oops-no-name-attribute</parameter>\n"
    "  </invoke>\n"
    "</tool_calls>"
)

HARD_REFUSAL_TEXT = "Sorry, I can't create files directly from this chat session."


def _turn(text: str, turn_id: str) -> TurnResult:
    return TurnResult(turn_id=turn_id, conversation_id="conv_correction_ctx", text=text)


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    return session


def _runtime(session: MagicMock, *, prompt_debug_dir=None) -> CompletionRuntime:
    @asynccontextmanager
    async def lease(*_args, **_kwargs):
        yield session

    return CompletionRuntime(
        conversations=MagicMock(),
        lease_session=lease,
        prompt_debug_dir=prompt_debug_dir,
    )


def _run(session: MagicMock, runtime: CompletionRuntime, **kwargs):
    record = ConversationRecord()
    return runtime.execute_raw_on_session(
        session,
        record,
        tail=R3_MESSAGES,
        messages=R3_MESSAGES,
        model="claude-3-5-sonnet",
        ui_model=None,
        tools=CLI_TOOLS,
        tool_choice=None,
        **kwargs,
    )


def _sent_prompts(session: MagicMock) -> list[str]:
    return [call.args[0] for call in session.send.call_args_list]


# ---------------------------------------------------------------------------
# (a) R3 scenario: the second send carries ORIGINAL USER TASK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r3_fresh_conversation_correction_prompt_contains_task(monkeypatch):
    """The core R3 regression: correction on a fresh thread must embed the task."""
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(COUNTER_QUESTION_TEXT, "r3_counter"),
            _turn(VALID_WRITE_BLOCK, "r3_fixed"),
        ]
    )
    runtime = _runtime(session)

    result, _prompt = await _run(session, runtime)

    assert "<tool_calls>" in result.text
    prompts = _sent_prompts(session)
    # The initial prompt renders the task normally; the CORRECTION prompt must
    # now carry it explicitly.
    assert "ORIGINAL USER TASK (for context):" not in prompts[0]
    assert "ORIGINAL USER TASK (for context):" in prompts[1]
    assert "fizzbuzz.py" in prompts[1]
    assert "output.txt" in prompts[1]
    # System-reminders are stripped from the embedded task context.
    embedded = prompts[1].split("ORIGINAL USER TASK (for context):", 1)[1]
    embedded = embedded.split("This is an automated", 1)[0].lower()
    assert "system-reminder" not in embedded


def test_original_task_context_helper():
    context = _original_task_context(R3_MESSAGES)
    assert context.startswith("# Task")
    assert "fizzbuzz.py" in context
    assert "system-reminder" not in context
    # Truncation cap ~4000 chars.
    long_messages = [{"role": "user", "content": "x" * 9000}]
    assert len(_original_task_context(long_messages)) == 4000
    # No user turn -> empty section (prompt stays unchanged).
    assert _original_task_context([{"role": "user", "content": "   "}]) == ""


def test_correction_prompt_variants_accept_task_context():
    for reason in ("FALSE_COMPLETION", "MALFORMED_TOOL", "TOOL_REFUSAL", "TOOL_REFUSAL_SOFT"):
        prompt = _correction_prompt_for(
            reason,
            CLI_TOOLS,
            None,
            detail="detail",
            task_context="write fizzbuzz.py",
        )
        assert "ORIGINAL USER TASK (for context):" in prompt, reason
        assert "write fizzbuzz.py" in prompt, reason
    # Backward-compatible default: no task context -> no section.
    bare = _correction_prompt_for("FALSE_COMPLETION", CLI_TOOLS, None, detail="d")
    assert "ORIGINAL USER TASK" not in bare


@pytest.mark.asyncio
async def test_http_path_correction_prompt_contains_task(monkeypatch):
    """HTTP parity: same guarantee through /v1/chat/completions."""
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    app = create_api_app(headless=True)
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(COUNTER_QUESTION_TEXT, "http_counter"),
            _turn(VALID_WRITE_BLOCK, "http_fixed"),
        ]
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Tạo script python fizzbuzz.py và chạy nó."}
                        ],
                    }
                ],
                "tools": CLI_TOOLS,
            },
        )

    assert response.status_code == 200
    prompts = _sent_prompts(session)
    assert "ORIGINAL USER TASK (for context):" in prompts[1]
    assert "fizzbuzz.py" in prompts[1]


# ---------------------------------------------------------------------------
# (b) Raw response dumps next to the prompt dumps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_dump_files_written_when_dir_configured(tmp_path):
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(COUNTER_QUESTION_TEXT, "dump_prose"),
            _turn(VALID_WRITE_BLOCK, "dump_fixed"),
        ]
    )
    runtime = _runtime(session, prompt_debug_dir=tmp_path)

    await _run(session, runtime)

    responses = sorted(tmp_path.glob("*_response.txt"))
    assert len(responses) >= 2
    contents = [path.read_text(encoding="utf-8") for path in responses]
    assert any(COUNTER_QUESTION_TEXT in content for content in contents)
    assert any("<tool_calls>" in content for content in contents)
    metadatas = [json.loads(p.with_suffix(".json").read_text()) for p in responses]
    {
        meta["assistant_chars"]: meta for meta in metadatas
    }
    prose_meta = next(m for m in metadatas if m["issue_reason"] is not None)
    assert prose_meta["issue_reason"] == "TOOL_REFUSAL_SOFT"
    assert "counter_question" in prose_meta["issue_detail"]
    assert isinstance(prose_meta["duration_ms"], int)
    fixed_meta = next(m for m in metadatas if m["issue_reason"] is None)
    assert fixed_meta["trace_sequence"] > 0


@pytest.mark.asyncio
async def test_no_crash_without_prompt_debug_dir(monkeypatch):
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session()
    session.send = AsyncMock(side_effect=[_turn(VALID_WRITE_BLOCK, "plain_ok")])
    runtime = _runtime(session)
    assert runtime.prompt_debug_dir is None

    result, _prompt = await _run(session, runtime)
    assert "<tool_calls>" in result.text


# ---------------------------------------------------------------------------
# (c) Identical adjacent hard failures raise early
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_malformed_tool_raises_after_two_sends():
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(MALFORMED_BLOCK, "mt_0"),
            _turn(MALFORMED_BLOCK, "mt_1"),
            _turn(MALFORMED_BLOCK, "mt_2"),
        ]
    )
    runtime = _runtime(session)

    with pytest.raises(MalformedToolCall) as excinfo:
        await _run(session, runtime)

    # Early raise on the first identical repeat: initial + ONE correction,
    # not the full default budget of 2 (which would be 3 sends).
    assert session.send.await_count == 2
    assert "Persistent MALFORMED_TOOL after correction" in str(excinfo.value)


@pytest.mark.asyncio
async def test_repeated_hard_refusal_raises_after_two_sends():
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(HARD_REFUSAL_TEXT, "hr_0"),
            _turn(HARD_REFUSAL_TEXT, "hr_1"),
            _turn(HARD_REFUSAL_TEXT, "hr_2"),
        ]
    )
    runtime = _runtime(session)

    with pytest.raises(MalformedToolCall) as excinfo:
        await _run(session, runtime)

    assert session.send.await_count == 2
    assert "Persistent TOOL_REFUSAL after correction" in str(excinfo.value)


@pytest.mark.asyncio
async def test_malformed_then_fixed_still_succeeds_normally():
    """One malformed response followed by a valid block must NOT raise early."""
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[_turn(MALFORMED_BLOCK, "mt_once"), _turn(VALID_WRITE_BLOCK, "ok")]
    )
    runtime = _runtime(session)

    result, _prompt = await _run(session, runtime)
    assert "<tool_calls>" in result.text
    assert session.send.await_count == 2


# ---------------------------------------------------------------------------
# (d) Trace metadata sanity for the new fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_records_task_context_chars(monkeypatch):
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[_turn(COUNTER_QUESTION_TEXT, "tc_0"), _turn(VALID_WRITE_BLOCK, "tc_1")]
    )
    runtime = _runtime(session)
    await _run(session, runtime)

    events = runtime.trace.snapshot()
    built = [
        event
        for event in events
        if event.component == "promptcompat"
        and event.kind == "correction_prompt_built"
    ]
    assert len(built) == 1
    assert built[0].metadata.get("task_context_chars", 0) > 0
