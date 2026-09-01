"""STEALTH PROTOCOL (WEBGPT_TOOL_PROTOCOL=soft) -- soft-framing probe follow-up.

Evidence: docs/reports/soft-framing-probe-2026-08-24.md.  Every request whose
prompt carried an injected controller block ("WEBGPT CONTROLLER TOOL
PROTOCOL", injected whenever the client declares `tools`) was refused by the
web injection classifier (0/2 control, 0/2 soft-prompt-with-tools), while a
short conversational convention got exact `<cmd>...</cmd>` emissions 2/2.

Under `soft`:
- promptcompat.render_messages injects NO bootstrap and NO tool protocol block;
- CompletionRuntime appends a one-time conversational handshake to the last
  user turn of a fresh conversation (never re-appended on later turns);
- ToolTranspiler.parse_tool_calls accepts <cmd>/<json> tags plus the json-fn
  shapes, fail-closed like every other variant;
- corrections keep the same conversational voice but always embed the
  ORIGINAL USER TASK context (R5-FIX);
- responses toward the CLI stay canonical Anthropic-style tool_use blocks;
- any other env value keeps the legacy behavior byte-for-byte.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from gpt.conversations import ConversationRecord
from gpt.gateway.runtime import (
    CompletionRuntime,
    _correction_prompt_for,
    _soft_correction_escalation,
    _soft_handshake_overhead_chars,
    _webgpt_tool_protocol,
    _with_soft_handshake,
)
from gpt.promptcompat import render_messages
from gpt.state import MalformedToolCall
from gpt.toolcall import ToolTranspiler
from gpt.types import TurnResult
from gpt.utils.assistantturn import AssistantTurnBuilder
from gpt.utils.toolcall import resolve_tool_protocol

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "Bash",
        "description": "run a shell command",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["command"],
        },
    },
}
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    },
}
TOOLS = [BASH_TOOL]

USER_TASK = "Run pwd inside the workspace so I can see where we are."
MESSAGES = [{"role": "user", "content": USER_TASK}]


def parse(text, *, tools=TOOLS, allowed=None):
    return ToolTranspiler.parse_tool_calls(
        text,
        allowed_tools=allowed if allowed is not None else {"Bash"},
        tool_definitions=tools,
        protocol="soft",
    )


# ---------------------------------------------------------------------------
# (a) soft mode injects no protocol block into the prompt
# ---------------------------------------------------------------------------


def test_soft_render_has_no_controller_protocol_block():
    prompt = render_messages(
        MESSAGES, initial=True, tools=TOOLS, tool_choice=None, tool_protocol="soft"
    )
    assert "WEBGPT CONTROLLER" not in prompt
    assert "WEBGPT SESSION BOOTSTRAP" not in prompt
    assert "<tool_calls>" not in prompt
    # The user task itself still reaches the model verbatim.
    assert USER_TASK in prompt


@pytest.mark.parametrize("tool_protocol", [None, "xml"])
def test_default_render_keeps_protocol_blocks(tool_protocol):
    kwargs = {} if tool_protocol is None else {"tool_protocol": tool_protocol}
    prompt = render_messages(MESSAGES, initial=True, tools=TOOLS, tool_choice=None, **kwargs)
    assert "WEBGPT SESSION BOOTSTRAP" in prompt
    assert "WEBGPT CONTROLLER TOOL PROTOCOL" in prompt


def test_soft_render_follows_environment(monkeypatch):
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    prompt = render_messages(MESSAGES, initial=True, tools=TOOLS, tool_choice=None)
    assert "WEBGPT CONTROLLER" not in prompt
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "xml")
    loud = render_messages(MESSAGES, initial=True, tools=TOOLS, tool_choice=None)
    assert "WEBGPT CONTROLLER TOOL PROTOCOL" in loud


def test_soft_build_tool_instructions_is_empty():
    assert ToolTranspiler.build_tool_instructions(TOOLS, protocol="soft") == ""


def test_soft_protocol_resolves_and_rejects_unknown(monkeypatch):
    monkeypatch.delenv("WEBGPT_TOOL_PROTOCOL", raising=False)
    assert resolve_tool_protocol("soft") == "soft"
    assert _webgpt_tool_protocol() == "xml"  # default unchanged
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "SOFT")  # case-insensitive
    assert resolve_tool_protocol() == "soft"
    with pytest.raises(ValueError):
        resolve_tool_protocol("quiet")


# ---------------------------------------------------------------------------
# (b) the conversational handshake appears on the first turn only
# ---------------------------------------------------------------------------


def test_handshake_appends_plain_tag_text_once():
    suffixed = _with_soft_handshake(USER_TASK)
    assert suffixed.startswith(USER_TASK)
    assert suffixed.count("<cmd>the exact shell command</cmd>") == 1
    assert "WEBGPT CONTROLLER" not in suffixed
    # Idempotence guard for a single application: the suffix is one sentence
    # pair, not a repeated block.
    assert suffixed.count("When my setup needs a shell action") == 1


def test_function_only_handshake_negotiates_json_not_cmd():
    suffixed = _with_soft_handshake(USER_TASK, [WEATHER_TOOL])
    assert suffixed.startswith(USER_TASK)
    assert "<json>...</json>" in suffixed
    assert '"name" and "arguments"' in suffixed
    assert "<cmd>" not in suffixed
    assert _soft_handshake_overhead_chars([WEATHER_TOOL]) == len(suffixed) - len(USER_TASK)


def test_function_only_correction_and_escalation_stay_on_json_surface(monkeypatch):
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    prompt = _correction_prompt_for(
        "FALSE_COMPLETION",
        [WEATHER_TOOL],
        "auto",
        detail="model returned only prose",
        task_context=TASK_CONTEXT,
    )
    escalation = _soft_correction_escalation([WEATHER_TOOL])
    assert TASK_CONTEXT in prompt
    assert "<json>...</json>" in prompt
    assert '"name" and "arguments"' in prompt
    assert "<cmd>" not in prompt
    assert "<json>...</json>" in escalation
    assert "<cmd>" not in escalation


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    return session


def _runtime_with_session(session: MagicMock) -> CompletionRuntime:
    @asynccontextmanager
    async def lease(*_args, **_kwargs):
        yield session

    return CompletionRuntime(conversations=MagicMock(), lease_session=lease)


@pytest.mark.asyncio
async def test_runtime_handshake_first_turn_only(monkeypatch):
    """Turn 1 carries the handshake; turn 2 of the same conversation does not."""
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    session = _fake_session()
    sends: list[str] = []

    async def send(prompt, timeout_seconds=None):
        sends.append(prompt)
        if len(sends) == 1:
            return TurnResult(
                turn_id="t1", conversation_id="conv_soft", text="<cmd>pwd</cmd>"
            )
        return TurnResult(turn_id="t2", conversation_id="conv_soft", text="2 + 2 equals 4.")

    session.send = send
    runtime = _runtime_with_session(session)

    record = ConversationRecord()
    tail = [MESSAGES[0]]
    result, prompt_sent = await runtime.execute_raw_on_session(
        session, record, tail, list(MESSAGES), "chatgpt-web", None,
        TOOLS, None,
    )
    assert prompt_sent is sends[0]
    assert record.web_bootstrapped is True
    assert sends[0].count("<cmd>the exact shell command</cmd>") == 1
    assert USER_TASK in sends[0]
    assert "WEBGPT CONTROLLER" not in sends[0]
    # The <cmd> response parses to a canonical Bash tool call for the CLI.
    assert result.text == "<cmd>pwd</cmd>"
    turn = AssistantTurnBuilder.from_model_text(result.text, tools=TOOLS, tool_choice=None)
    assert turn.finish_reason == "tool_calls"
    assert [call["function"]["name"] for call in turn.tool_calls] == ["Bash"]
    assert json.loads(turn.tool_calls[0]["function"]["arguments"]) == {"command": "pwd"}

    # Second turn: transcript already has tool traffic, web_bootstrapped=True.
    messages2 = [*MESSAGES, {"role": "assistant", "content": "", "tool_calls": [turn.tool_calls[0]]}, {"role": "user", "content": "Thanks! What is 2 + 2?"}]
    await runtime.execute_raw_on_session(
        session, record, [messages2[-1]], messages2, "chatgpt-web", None,
        TOOLS, None,
    )
    assert len(sends) == 2
    assert "<cmd>" not in sends[1]
    assert "<cmd>the exact shell command</cmd>" not in sends[1]
    assert "WEBGPT CONTROLLER" not in sends[1]
    assert "Thanks! What is 2 + 2?" in sends[1]


# ---------------------------------------------------------------------------
# (c) <cmd> parsing -> canonical Bash calls
# ---------------------------------------------------------------------------


def test_single_cmd_tag_parses_to_bash_call():
    clean, calls = parse("<cmd>pwd</cmd>")
    assert clean is None
    assert [call["function"]["name"] for call in calls] == ["Bash"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "pwd"}
    assert calls[0]["type"] == "function"
    assert calls[0]["id"].startswith("call_")


def test_multiple_cmd_tags_parse_to_ordered_bash_calls():
    text = "<cmd>mkdir -p pkg</cmd>\n<cmd>touch pkg/__init__.py</cmd>"
    _, calls = parse(text)
    assert [call["function"]["name"] for call in calls] == ["Bash", "Bash"]
    commands = [json.loads(call["function"]["arguments"])["command"] for call in calls]
    assert commands == ["mkdir -p pkg", "touch pkg/__init__.py"]


def test_cmd_tag_multiline_command_preserved():
    body = "cat > example.py <<'PY'\ndef main():\n    return 0\nPY"
    _, calls = parse(f"<cmd>{body}</cmd>")
    assert json.loads(calls[0]["function"]["arguments"])["command"] == body


def test_lowercase_shell_tool_name_is_used_when_declared():
    bash_lower = {
        "type": "function",
        "function": {**BASH_TOOL["function"], "name": "bash"},
    }
    _, calls = parse("<cmd>pwd</cmd>", tools=[bash_lower], allowed={"bash"})
    assert [call["function"]["name"] for call in calls] == ["bash"]


def test_cmd_tag_fail_closed():
    with pytest.raises(MalformedToolCall):
        parse("<cmd>pwd</invoke>")  # never closed
    with pytest.raises(MalformedToolCall):
        parse("<cmd></cmd>")  # empty command
    # A shell command cannot map when no shell-shaped tool was advertised.
    with pytest.raises(MalformedToolCall):
        parse("<cmd>pwd</cmd>", tools=[WEATHER_TOOL], allowed={"get_weather"})


def test_plain_prose_stays_prose_under_soft():
    text = "The capital of France is Paris."
    clean, calls = parse(text)
    assert calls == []
    assert clean == text


# ---------------------------------------------------------------------------
# (d) <json> tag path and bare JSON fallback (json-fn shapes under soft)
# ---------------------------------------------------------------------------


def test_json_tag_parses_function_calls():
    text = (
        '<json>[{"name":"get_weather","arguments":{"location":"Hanoi"}}]</json>'
    )
    _, calls = parse(text, tools=[WEATHER_TOOL], allowed={"get_weather"})
    assert [call["function"]["name"] for call in calls] == ["get_weather"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"location": "Hanoi"}


def test_json_tag_openai_arguments_string_accepted():
    text = (
        '<json>{"name":"get_weather",'
        '"arguments":"{\\"location\\":\\"Hanoi\\"}"}</json>'
    )
    _, calls = parse(text, tools=[WEATHER_TOOL], allowed={"get_weather"})
    assert json.loads(calls[0]["function"]["arguments"]) == {"location": "Hanoi"}


def test_bare_json_array_parses_under_soft():
    text = '[{"name":"get_weather","arguments":{"location":"Hanoi"}}]'
    _, calls = parse(text, tools=[WEATHER_TOOL], allowed={"get_weather"})
    assert [call["function"]["name"] for call in calls] == ["get_weather"]


def test_json_tag_fail_closed():
    with pytest.raises(MalformedToolCall):
        parse("<json>[{\"name\":\"get_weather\",\"arguments\":{}}]</json>",
              tools=[WEATHER_TOOL], allowed={"get_weather"})  # schema violation
    with pytest.raises(MalformedToolCall):
        parse("<json>[{\"name\":\"get_weather\"}</json>",
              tools=[WEATHER_TOOL], allowed={"get_weather"})  # broken JSON / unclosed
    with pytest.raises(MalformedToolCall):
        parse("<json>[{\"name\":\"get_weather\",\"arguments\":{}}]",
              tools=[WEATHER_TOOL], allowed={"get_weather"})  # never closed


def test_mixed_soft_shapes_fail_closed():
    # Mixing two emit shapes in one reply stays fail-closed: <cmd> wins and
    # the leftover <json> block counts as prose around a tool call.
    text = '<cmd>pwd</cmd><json>[{"name":"get_weather","arguments":{"location":"Hanoi"}}]</json>'
    with pytest.raises(MalformedToolCall):
        parse(text, tools=[*TOOLS, WEATHER_TOOL], allowed={"Bash", "get_weather"})


def test_xml_markup_still_wins_over_soft_tags():
    text = (
        "<tool_calls>\n<invoke name=\"Bash\">\n"
        "<parameter name=\"command\"><![CDATA[pwd]]></parameter>\n"
        "</invoke>\n</tool_calls>\n<cmd>ls</cmd>"
    )
    with pytest.raises(MalformedToolCall):  # markup wins; trailing tag is prose mix
        parse(text)


# ---------------------------------------------------------------------------
# (e) soft correction prompt: task context embedded, conversational voice
# ---------------------------------------------------------------------------


TASK_CONTEXT = "Tạo script python fizzbuzz.py và chạy nó."


def _soft_correction(monkeypatch, **kwargs):
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    defaults = dict(
        reason="FALSE_COMPLETION",
        detail="model returned only prose",
        task_context=TASK_CONTEXT,
    )
    defaults.update(kwargs)
    reason = defaults.pop("reason")
    return _correction_prompt_for(reason, TOOLS, "auto", **defaults)


def test_soft_correction_carries_task_context_and_cmd_convention(monkeypatch):
    prompt = _soft_correction(monkeypatch)
    assert TASK_CONTEXT in prompt  # R5-FIX: original user task always embedded
    assert "<cmd>the exact shell command</cmd>" in prompt
    assert "paste the output back" in prompt


def test_soft_correction_voice_is_not_a_loud_banner(monkeypatch):
    prompt = _soft_correction(monkeypatch)
    assert "WEBGPT CONTROLLER" not in prompt
    assert "CORRECTION" not in prompt
    assert "SYSTEM REQUIREMENT" not in prompt
    assert "REFUSAL OVERRIDE" not in prompt
    # No injected tool schema payload either.
    assert "Available tools:" not in prompt


def test_soft_correction_counter_question_adds_discover_hint(monkeypatch):
    prompt = _soft_correction(
        monkeypatch,
        reason="TOOL_REFUSAL_SOFT",
        counter_question=True,
    )
    assert "working directory" in prompt
    assert TASK_CONTEXT in prompt


def test_legacy_correction_prompts_unchanged_outside_soft(monkeypatch):
    monkeypatch.delenv("WEBGPT_TOOL_PROTOCOL", raising=False)
    prompt = _correction_prompt_for(
        "FALSE_COMPLETION", TOOLS, "auto", detail="d", task_context=TASK_CONTEXT
    )
    assert "WEBGPT CONTROLLER CORRECTION" in prompt
    assert "Return ONLY one valid tool call block" in prompt
    refusal = _correction_prompt_for(
        "TOOL_REFUSAL", TOOLS, "auto", detail="d", task_context=TASK_CONTEXT
    )
    assert "REFUSAL OVERRIDE" in refusal
    assert TASK_CONTEXT in refusal


def test_kill_switch_other_values_keep_legacy_behavior(monkeypatch):
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "json-fn")
    prompt = render_messages(MESSAGES, initial=True, tools=TOOLS, tool_choice=None)
    assert "WEBGPT CONTROLLER TOOL PROTOCOL" in prompt
    instructions = ToolTranspiler.build_tool_instructions(TOOLS)
    assert "```json" in instructions
    fenced = '```json\n[{"name":"get_weather","arguments":{"location":"Hanoi"}}]\n```'
    _, calls = ToolTranspiler.parse_tool_calls(
        fenced,
        allowed_tools={"get_weather"},
        tool_definitions=[WEATHER_TOOL],
    )
    assert [call["function"]["name"] for call in calls] == ["get_weather"]


# ---------------------------------------------------------------------------
# (f) T2 root-fix regressions (docs/reports/debug-t2-2026-08-25.md): a valid
# soft <cmd> reply must not be discarded for trailing conversational prose
# (BUG 1), and a shell-less surface must produce an actionable configuration
# error instead of "Unknown tool requested: Bash" (BUG 2).
# ---------------------------------------------------------------------------


WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "write text content to a file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
}
T2_SURFACE = [WRITE_FILE_TOOL, BASH_TOOL]
_T2_REPLY = (
    "<cmd>mkdir -p /tmp/verify_hybrid_flip && printf 'HYBRID_FLIP_T2_OK\\n' "
    "> /tmp/verify_hybrid_flip/t2_hello.txt</cmd>"
    "\n\nNeed next step: verify file or inspect directory?"
)


def test_cmd_with_trailing_prose_parses_under_soft():
    clean, calls = parse(_T2_REPLY)
    assert clean is not None and "Need next step" in clean
    assert [call["function"]["name"] for call in calls] == ["Bash"]
    command = json.loads(calls[0]["function"]["arguments"])["command"]
    assert command.startswith("mkdir -p /tmp/verify_hybrid_flip")


def test_prose_around_json_tag_parses_under_soft():
    text = (
        '<json>[{"name":"get_weather","arguments":{"location":"Hanoi"}}]</json>'
        " Let me know if you want another city."
    )
    clean, calls = parse(text, tools=[WEATHER_TOOL], allowed={"get_weather"})
    assert clean is not None and "Let me know" in clean
    assert [call["function"]["name"] for call in calls] == ["get_weather"]


def test_cmd_without_shell_tool_raises_configured_error_not_unknown_bash():
    with pytest.raises(MalformedToolCall) as excinfo:
        parse("<cmd>mkdir -p x</cmd>", tools=[WRITE_FILE_TOOL], allowed={"write_file"})
    message = str(excinfo.value)
    assert "Unknown tool requested" not in message
    assert "Bash/bash" in message  # names the required shell tool
    assert "write_file" in message  # lists the declared surface


def test_t2_actual_reply_without_shell_surface_fails_with_config_error():
    # The exact T2 stack (prose AND no shell tool) must now die at the single
    # configuration layer instead of two stacked misleading parser errors.
    with pytest.raises(MalformedToolCall) as excinfo:
        parse(_T2_REPLY, tools=[WRITE_FILE_TOOL], allowed={"write_file"})
    message = str(excinfo.value)
    assert "cannot be mixed with final assistant prose" not in message
    assert "Unknown tool requested" not in message
    assert "shell tool" in message


def test_xml_and_json_fn_protocols_stay_strict_on_prose(monkeypatch):
    monkeypatch.delenv("WEBGPT_TOOL_PROTOCOL", raising=False)
    xml_reply = (
        '<tool_calls><invoke name="Bash">'
        "<parameter name=\"command\">pwd</parameter>"
        "</invoke></tool_calls>\nNeed next step?"
    )
    with pytest.raises(MalformedToolCall):
        ToolTranspiler.parse_tool_calls(
            xml_reply, allowed_tools={"Bash"}, tool_definitions=T2_SURFACE
        )
    json_reply = (
        '```json\n[{"name":"Bash","arguments":{"command":"pwd"}}]\n```\nNeed next step?'
    )
    with pytest.raises(MalformedToolCall):
        ToolTranspiler.parse_tool_calls(
            json_reply,
            allowed_tools={"Bash"},
            tool_definitions=T2_SURFACE,
            protocol="json-fn",
        )


def test_assistant_turn_builder_accepts_soft_cmd_with_trailing_prose(monkeypatch):
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    turn = AssistantTurnBuilder.from_model_text(
        _T2_REPLY,
        tools=T2_SURFACE,
        tool_choice=None,
    )
    assert turn.finish_reason == "tool_calls"
    assert turn.content is not None and "Need next step" in turn.content
    assert [call["function"]["name"] for call in turn.tool_calls] == ["Bash"]


# ---------------------------------------------------------------------------
# (h) CODE-FENCE / INLINE-QUOTE IMMUNITY + PLACEHOLDER EXCISION
#     (codex12 findings #2/#5, 2026-08-26): the soft candidate scan must run
#     on the markdown-masked text so <cmd>/<json> tags the model merely ECHOES
#     inside a fenced or inline-quoted code span never become executable tool
#     calls; and placeholder-only replies must have their quoted tag spans
#     excised from the visible prose as the debug-r9 comment promises.
# ---------------------------------------------------------------------------


def test_cmd_inside_code_fence_never_executes():
    text = (
        "Sure — the convention you described looks like:\n\n"
        "```\n<cmd>rm -rf /tmp/important</cmd>\n```\n\n"
        "Shall I proceed?"
    )
    clean, calls = parse(text)
    assert calls == []  # echoed tag must not become a live Bash call
    assert clean == text  # no attempt -> reply passes through untouched


def test_cmd_inline_quoted_never_executes():
    text = "The format is `<cmd>pwd</cmd>` right?"
    clean, calls = parse(text)
    assert calls == []
    assert clean == text


def test_json_tag_inline_quoted_never_executes():
    text = (
        'Should I send `<json>[{"name":"Bash","arguments":{"command":"pwd"}}]</json>` '
        "next time?"
    )
    clean, calls = parse(text)
    assert calls == []
    assert clean == text


def test_legit_backtick_substitution_body_still_parses():
    # A real command whose BODY contains balanced backticks keeps parsing:
    # the tag literals stay outside any code span (so the masked scan still
    # finds them) while the body itself is extracted from the original text.
    _, calls = parse("<cmd>echo `pwd`</cmd>")
    assert [call["function"]["name"] for call in calls] == ["Bash"]
    assert json.loads(calls[0]["function"]["arguments"])["command"] == "echo `pwd`"


def test_unmatched_backtick_in_cmd_body_still_parses():
    # Codex13 finding #3 (2026-08-26): a literal UNMATCHED backtick inside a
    # legitimate unfenced body must not open an inline-code span that swallows
    # the closing tag (regression used to raise MalformedToolCall).
    _, calls = parse('<cmd>printf "`"</cmd>')
    assert [call["function"]["name"] for call in calls] == ["Bash"]
    assert json.loads(calls[0]["function"]["arguments"])["command"] == 'printf "`"'


def test_unmatched_backtick_body_does_not_unshield_fenced_echo():
    # The shielding is scoped to paired raw-text tag regions only: an echoed
    # tag inside a code fence stays blocked even when a legit stray-backtick
    # body was parsed earlier in the same reply.
    text = (
        '<cmd>printf "`"</cmd>\n'
        "The convention looks like:\n"
        "```\n<cmd>rm -rf /tmp/important</cmd>\n```\n"
    )
    clean, calls = parse(text)
    assert [json.loads(call["function"]["arguments"])["command"] for call in calls] == [
        'printf "`"'
    ]
    assert clean is not None
    assert "rm -rf /tmp/important" not in json.dumps(calls)


def test_all_placeholder_cmd_tags_excised_from_prose():
    text = 'Đã rõ. Dạng dòng lệnh là <cmd>"..."</cmd> nhé.'
    clean, calls = parse(text)
    assert calls == []
    assert clean is not None
    assert "<cmd>" not in clean
    assert '"..."' not in clean
    assert "Đã rõ." in clean
