"""CORRECTION-TIGHTEN regression tests (trace-forensics-2026-08-25 Q4/Q5.3/Q5.4).

Three behaviors are pinned here:

1. Layered correction budget: protocol-shaped failures (MALFORMED_TOOL,
   INVALID_WRITE, MULTI_TOOL -- unparseable or structurally invalid tool
   blocks) stop after at most TWO corrections even when the operator raised
   WEBGPT_MAX_CORRECTIONS to 4; prose/refusal-shaped reasons keep the full
   configured budget.  Forensics: corr=4 turns averaged 77.9s (~10x a clean
   turn) and never converged past round two.
2. Anti-repeat: resending a byte-identical correction prompt is a proven quota
   sink.  The first content-identical repeat gets an escalation hint appended
   (so the prompt actually changes); if the loop still produces the identical
   base correction after that, it fails fast with "Correction loop not
   converging" instead of draining the budget.
3. Instrumentation: ``submit_completed`` must report the real correction spend
   and the non-null turn id of the final committed web response (forensics:
   request_completed.correction_count was always 0 and turn_id was null on
   92% of completed events), and every failure terminal event carries the same.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from gpt.conversations import ConversationRecord
from gpt.gateway.runtime import CompletionRuntime, _fresh_tool_conversation
from gpt.state import MalformedToolCall
from gpt.types import TurnResult

CLI_TOOLS = [
    {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object"}},
    }
    for name in ("Agent", "Bash", "Edit", "Read", "Write")
]

TASK_MESSAGES = [
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "<system-reminder>\nWorking directory: /tmp/cc-tighten\n"
                    "</system-reminder>\n\n"
                    "# Task\n"
                    "Tạo script python `fizzbuzz.py` in các số 1..15.\n"
                    "Chạy script đó, và ghi output vào file `output.txt`.\n"
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

# Tag-bearing but unparseable -> MALFORMED_TOOL.
MALFORMED_BLOCK_A = (
    "<tool_calls>\n"
    '  <invoke name="Bash">\n'
    "    <parameter>oops-no-name-attribute</parameter>\n"
    "  </invoke>\n"
    "</tool_calls>"
)

# Second distinct malformed shape; still MALFORMED_TOOL.
MALFORMED_BLOCK_B = (
    "<tool_calls>\n"
    '  <invoke name="Bash">\n'
    "    <parameter>totally-unparseable-garbage-xyz</parameter>\n"
    "  </invoke>\n"
    "</tool_calls>"
)

# Two parseable invokes in one turn -> MULTI_TOOL (no fan-out requested).
DOUBLE_INVOKE_BLOCK = (
    "<tool_calls>\n"
    '  <invoke name="Write">\n'
    '    <parameter name="file_path"><![CDATA[a.txt]]></parameter>\n'
    '    <parameter name="lines"><![CDATA[hi]]></parameter>\n'
    "  </invoke>\n"
    '  <invoke name="Write">\n'
    '    <parameter name="file_path"><![CDATA[b.txt]]></parameter>\n'
    '    <parameter name="lines"><![CDATA[yo]]></parameter>\n'
    "  </invoke>\n"
    "</tool_calls>"
)

HARD_REFUSAL_TEXT = "Sorry, I can't create files directly from this chat session."

# Prose that claims nothing and asks nothing -> plain FALSE_COMPLETION on this
# tool-directed task (no soft-refusal signals, no action-claim markers), so its
# correction prompt is deterministic across rounds.
FALSE_COMPLETION_TEXT = "The fizzbuzz script works exactly as requested."

ESCALATION_MARKER = "ESCALATION (final retry)"


def _turn(text: str, turn_id: str) -> TurnResult:
    return TurnResult(turn_id=turn_id, conversation_id="conv_tighten", text=text)


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    return session


def _runtime(session: MagicMock) -> CompletionRuntime:
    @asynccontextmanager
    async def lease(*_args, **_kwargs):
        yield session

    return CompletionRuntime(
        conversations=MagicMock(),
        lease_session=lease,
    )


def _run(session: MagicMock, runtime: CompletionRuntime):
    return runtime.execute_raw_on_session(
        session,
        ConversationRecord(),
        tail=TASK_MESSAGES,
        messages=TASK_MESSAGES,
        model="claude-3-5-sonnet",
        ui_model=None,
        tools=CLI_TOOLS,
        tool_choice=None,
    )


def _sent_prompts(session: MagicMock) -> list[str]:
    return [call.args[0] for call in session.send.call_args_list]


def _events(runtime: CompletionRuntime, kind: str) -> list:
    return [
        event
        for event in runtime.trace.snapshot()
        if event.component == "completionruntime" and event.kind == kind
    ]


# ---------------------------------------------------------------------------
# (1) Protocol-shaped sub-budget: stops at 2 corrections despite budget 4
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_protocol_shaped_budget_caps_at_two_despite_budget_four(
    monkeypatch,
):
    """MALFORMED_TOOL/MULTI_TOOL rounds stop at the 2-correction protocol cap.

    The alternating shapes keep both the persistent-reason guard and the
    anti-repeat guard out of the way so the layered budget itself is what
    terminates the loop.
    """
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "4")
    # P1-3: DOUBLE_INVOKE_BLOCK only classifies as MULTI_TOOL when the cap is
    # one invoke per turn; pin it so the protocol-shaped budget stays exercised.
    monkeypatch.setenv("WEBGPT_MAX_TOOL_CALLS_PER_TURN", "1")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(MALFORMED_BLOCK_A, "tighten_m0"),
            _turn(DOUBLE_INVOKE_BLOCK, "tighten_mt1"),
            _turn(MALFORMED_BLOCK_B, "tighten_m2"),
            _turn(MALFORMED_BLOCK_B, "tighten_m3_never_sent"),
        ]
    )
    runtime = _runtime(session)

    with pytest.raises(MalformedToolCall) as excinfo:
        await _run(session, runtime)

    # Initial send + exactly TWO corrections; the fourth response is consumed
    # by nothing -- the loop raises before a third correction can be sent.
    assert session.send.await_count == 3
    assert "protocol_shaped 2/2" in str(excinfo.value)

    exhausted = _events(runtime, "correction_budget_exhausted")
    assert len(exhausted) == 1
    meta = exhausted[0].metadata
    assert meta["correction_class"] == "protocol_shaped"
    assert meta["used"] == 2
    assert meta["cap"] == 2
    assert meta["max_corrections"] == 4
    assert meta["correction_count"] == 2


@pytest.mark.asyncio
async def test_general_class_keeps_full_configured_budget(monkeypatch):
    """Prose-shaped reasons still get the whole WEBGPT_MAX_CORRECTIONS budget."""
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "4")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session()
    # Alternate FALSE_COMPLETION / TOOL_REFUSAL so prompts differ each round
    # (no anti-repeat trigger) while every round stays in the general class.
    session.send = AsyncMock(
        side_effect=[
            _turn(FALSE_COMPLETION_TEXT, "gen_0"),
            _turn(HARD_REFUSAL_TEXT, "gen_1"),
            _turn(FALSE_COMPLETION_TEXT, "gen_2"),
            _turn(HARD_REFUSAL_TEXT, "gen_3"),
            _turn(FALSE_COMPLETION_TEXT, "gen_4"),
        ]
    )
    runtime = _runtime(session)

    with pytest.raises(MalformedToolCall) as excinfo:
        await _run(session, runtime)

    # Initial + FOUR corrections: the general class was not clipped to 2.
    assert session.send.await_count == 5
    assert "general 4/4" in str(excinfo.value)
    meta = _events(runtime, "correction_budget_exhausted")[0].metadata
    assert meta["correction_class"] == "general"
    assert meta["cap"] == 4


# ---------------------------------------------------------------------------
# (2) Anti-repeat: escalate hint once, then fail fast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_correction_prompt_gets_escalation_hint_then_raises(
    monkeypatch,
):
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "3")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(FALSE_COMPLETION_TEXT, "rep_0"),
            _turn(FALSE_COMPLETION_TEXT, "rep_1"),
            _turn(FALSE_COMPLETION_TEXT, "rep_2"),
        ]
    )
    runtime = _runtime(session)

    with pytest.raises(MalformedToolCall) as excinfo:
        await _run(session, runtime)

    prompts = _sent_prompts(session)
    # First correction: plain prompt.
    assert ESCALATION_MARKER not in prompts[1]
    # Second correction is content-identical to the first base prompt ->
    # escalated hint appended instead of a byte-identical resend.
    assert ESCALATION_MARKER in prompts[2]
    # Third identical occurrence: fail fast BEFORE another send.
    assert "Correction loop not converging" in str(excinfo.value)
    assert session.send.await_count == 3

    repeats = _events(runtime, "persistent_correction_repeat")
    assert len(repeats) == 1
    # Codex13 finding #4 (2026-08-26): exactly TWO corrections were actually
    # sent (plain + escalated); the aborted third attempt must not inflate
    # terminal telemetry.
    assert repeats[0].metadata["correction_count"] == 2

    failed = _events(runtime, "submit_failed_before_commit_unknown")
    assert len(failed) == 1
    assert failed[0].metadata["correction_count"] == 2


@pytest.mark.asyncio
async def test_distinct_correction_prompts_do_not_trigger_anti_repeat(monkeypatch):
    """Different reasons produce different prompts: no false repeat trip."""
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "3")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(FALSE_COMPLETION_TEXT, "mix_0"),
            _turn(HARD_REFUSAL_TEXT, "mix_1"),
            _turn(VALID_WRITE_BLOCK, "mix_ok"),
        ]
    )
    runtime = _runtime(session)

    result, _prompt = await _run(session, runtime)
    assert "<tool_calls>" in result.text
    assert _events(runtime, "persistent_correction_repeat") == []


# ---------------------------------------------------------------------------
# (3) Instrumentation: real correction_count and non-null turn_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_metadata_reports_real_correction_count_and_turn_id(
    monkeypatch,
):
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(COUNTER_QUESTION_TEXT, "done_prose"),
            _turn(VALID_WRITE_BLOCK, "done_final_turn"),
        ]
    )
    runtime = _runtime(session)

    result, _prompt = await _run(session, runtime)

    assert result.turn_id == "done_final_turn"
    completed = [
        event for event in _events(runtime, "submit_completed")
    ]
    assert len(completed) == 1
    meta = completed[0].metadata
    # One correction round actually happened -> not the stale constant 0.
    assert meta["correction_count"] == 1
    assert meta["turn_id"] == "done_final_turn"


@pytest.mark.asyncio
async def test_failure_events_report_real_spend_and_last_turn_id(monkeypatch):
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "4")
    # P1-3: same as above -- MULTI_TOOL classification needs the strict cap.
    monkeypatch.setenv("WEBGPT_MAX_TOOL_CALLS_PER_TURN", "1")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(MALFORMED_BLOCK_A, "fail_t0"),
            _turn(DOUBLE_INVOKE_BLOCK, "fail_t1"),
            _turn(MALFORMED_BLOCK_B, "fail_t2"),
        ]
    )
    runtime = _runtime(session)

    with pytest.raises(MalformedToolCall):
        await _run(session, runtime)

    failed = _events(runtime, "submit_failed_before_commit_unknown")
    assert len(failed) == 1
    meta = failed[0].metadata
    # Two corrections were spent before the protocol cap fired, and the last
    # committed web turn was the second response -- neither may be lost.
    assert meta["correction_count"] == 2
    assert meta["turn_id"] == "fail_t2"
    assert meta["error_type"] == "MalformedToolCall"

    exhausted = _events(runtime, "correction_budget_exhausted")[0]
    assert exhausted.metadata["turn_id"] == "fail_t2"


# ---------------------------------------------------------------------------
# (4) Codex12 #1 (2026-08-26): content-based freshness -- the FALSE_COMPLETION
#     guard must stay live after no-op <cmd>true</cmd> commits, or the armed
#     metronome skip in the correction loop becomes dead code.
# ---------------------------------------------------------------------------

NOOP_TRUE_BLOCK = (
    "<tool_calls>\n"
    '  <invoke name="Bash">\n'
    '    <parameter name="command"><![CDATA[true]]></parameter>\n'
    "  </invoke>\n"
    "</tool_calls>"
)


def _assistant_tool_call(name: str, arguments: str, call_id: str):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


def test_fresh_guard_counts_noop_and_placeholder_activity_as_fresh():
    """No-op ``true`` / quoted-placeholder commits keep the conversation fresh.

    Codex12 #1: the old never-any-tool-call rule stayed stale forever after
    the first commit; only REAL work may retire the FALSE_COMPLETION guard.
    """
    metronome = [*TASK_MESSAGES, _assistant_tool_call("Bash", '{"command": "true"}', "call_true"), {"role": "tool", "tool_call_id": "call_true", "content": ""}]
    placeholder = [*TASK_MESSAGES, _assistant_tool_call("Bash", '"..."', "call_ph"), {"role": "tool", "tool_call_id": "call_ph", "content": "exit 127"}]
    real = [*TASK_MESSAGES, _assistant_tool_call("Write", '{"file_path": "hello.txt", "lines": "hi\\n"}', "call_write"), {"role": "tool", "tool_call_id": "call_write", "content": "File created"}]
    assert _fresh_tool_conversation(TASK_MESSAGES, []) is True
    assert _fresh_tool_conversation(metronome, []) is True
    assert _fresh_tool_conversation(placeholder, []) is True
    assert _fresh_tool_conversation(real, []) is False
    # A result that cannot be matched to an in-transcript no-op call is
    # conservative evidence of real work (truncated history).
    orphan = [*TASK_MESSAGES, {"role": "tool", "tool_call_id": "x", "content": "out"}]
    assert _fresh_tool_conversation(orphan, []) is False


@pytest.mark.asyncio
async def test_noop_metronome_still_reaches_correction_skip(monkeypatch):
    """End-to-end RC3 metronome: FALSE_COMPLETION corrections continue after
    no-op commits and the armed-noop skip fires instead of a dead loop.

    The real metronome spans several controller requests (one per CLI agent
    round); the breaker state persists across them via the conversation-keyed
    module state, so this drives three requests over one shared record.
    """
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "6")
    monkeypatch.setenv("WEBGPT_NOOP_REPEAT_SKIP", "2")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(FALSE_COMPLETION_TEXT, "mt_r1_claim"),   # FC -> correction 1
            _turn(NOOP_TRUE_BLOCK, "mt_r1_true"),           # noop commit streak=1
            _turn(FALSE_COMPLETION_TEXT, "mt_r2_claim"),    # FC -> correction 2
            _turn(NOOP_TRUE_BLOCK, "mt_r2_true"),           # noop commit streak=2
            _turn(FALSE_COMPLETION_TEXT, "mt_r3_claim"),    # FC -> skip branch
        ]
    )
    runtime = _runtime(session)
    # Pin the breaker key: the fake commits report conversation_id, which
    # would otherwise flip record.conversation_id after request 1 and split
    # the cross-request breaker state in half.
    record = ConversationRecord(conversation_id="conv_tighten")

    for _request in range(2):
        await runtime.execute_raw_on_session(
            session,
            record,
            tail=TASK_MESSAGES,
            messages=TASK_MESSAGES,
            model="claude-3-5-sonnet",
            ui_model=None,
            tools=CLI_TOOLS,
            tool_choice=None,
        )

    result, _prompt = await runtime.execute_raw_on_session(
        session,
        record,
        tail=TASK_MESSAGES,
        messages=TASK_MESSAGES,
        model="claude-3-5-sonnet",
        ui_model=None,
        tools=CLI_TOOLS,
        tool_choice=None,
    )

    assert "[webgpt] correction skipped" in result.text
    assert session.send.await_count == 5
    skips = _events(runtime, "correction_skipped_noop_repeat")
    assert len(skips) == 1
    assert skips[0].metadata["noop_streak"] == 2
    assert skips[0].metadata["reason"] == "FALSE_COMPLETION"
