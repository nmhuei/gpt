"""Protocol variant tests: WEBGPT_TOOL_PROTOCOL = xml (default) | json-fn | both.

Covers the json-fn OpenAI function-calling JSON shape (fenced or bare), the
fail-closed behavior for broken JSON, both-mode acceptance, byte-identical
xml-default regression, and protocol-dependent build_tool_instructions.
"""

import json

import pytest

from gpt.assistantturn import AssistantTurnBuilder
from gpt.state import MalformedToolCall
from gpt.toolcall import ToolTranspiler
from gpt.utils.toolcall import ToolTranspiler as ToolTranspilerFromUtils
from gpt.utils.toolcall import resolve_tool_protocol

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
TOOLS = [WEATHER_TOOL]


def parse(text, *, protocol=None, allowed=None):
    return ToolTranspiler.parse_tool_calls(
        text,
        allowed_tools=allowed if allowed is not None else {"get_weather"},
        tool_definitions=TOOLS,
        protocol=protocol,
    )


def call_names(calls):
    return [c["function"]["name"] for c in calls]


# ---------------------------------------------------------------------------
# (a) json-fn parses arrays / single objects / fences to canonical calls
# ---------------------------------------------------------------------------


def test_json_fn_fenced_array_parses_canonical():
    text = '```json\n[{"name":"get_weather","arguments":{"location":"Hanoi"}}]\n```'
    clean, calls = parse(text, protocol="json-fn")
    assert clean is None
    assert call_names(calls) == ["get_weather"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"location": "Hanoi"}
    assert calls[0]["type"] == "function"
    assert calls[0]["id"].startswith("call_")


def test_json_fn_fenced_single_object_parses():
    text = '```json\n{"name":"get_weather","arguments":{"location":"Hanoi"}}\n```'
    _, calls = parse(text, protocol="json-fn")
    assert call_names(calls) == ["get_weather"]


def test_json_fn_bare_array_without_fence_parses():
    text = '[{"name":"get_weather","arguments":{"location":"Hanoi"}}]'
    clean, calls = parse(text, protocol="json-fn")
    assert clean is None
    assert call_names(calls) == ["get_weather"]


def test_json_fn_bare_single_object_without_fence_parses():
    text = '{"name":"get_weather","arguments":{"location":"Hanoi"}}'
    _, calls = parse(text, protocol="json-fn")
    assert call_names(calls) == ["get_weather"]


def test_json_fn_stripped_fence_artifact_json_label_parses():
    # Live evidence: inner_text() DOM scraping strips ``` backticks, leaving
    # the artifact ``JSON\n[...]``.
    text = 'JSON\n[{"name":"get_weather","arguments":{"location":"Hanoi"}}]'
    clean, calls = parse(text, protocol="json-fn")
    assert clean is None
    assert call_names(calls) == ["get_weather"]
    object_form = 'JSON\n{"name":"get_weather","arguments":{"location":"Hanoi"}}'
    _, calls = parse(object_form, protocol="both")
    assert call_names(calls) == ["get_weather"]


def test_json_fn_prose_starting_with_word_json_is_not_a_call():
    text = "JSON is a data format, not a tool call."
    clean, calls = parse(text, protocol="json-fn")
    assert calls == []
    assert clean == text


def test_json_fn_json_label_with_broken_body_fails_closed():
    text = 'JSON\n[{"name":"get_weather","arguments":{"location":}}]'
    with pytest.raises(MalformedToolCall):
        parse(text, protocol="json-fn")


def test_json_fn_openai_style_arguments_string_accepted():
    text = (
        '```json\n[{"name":"get_weather",'
        '"arguments":"{\\"location\\":\\"Hanoi\\"}"}]\n```'
    )
    _, calls = parse(text, protocol="json-fn")
    assert json.loads(calls[0]["function"]["arguments"]) == {"location": "Hanoi"}


def test_json_fn_multiple_entries_in_one_array():
    text = (
        "```json\n"
        '[{"name":"get_weather","arguments":{"location":"Hanoi"}},'
        '{"name":"get_weather","arguments":{"location":"Paris"}}]\n'
        "```"
    )
    _, calls = parse(text, protocol="json-fn")
    assert len(calls) == 2


def test_json_fn_virtual_write_transpiles_like_xml_path():
    bash_tool = {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "run a command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
    text = (
        "```json\n"
        '[{"name":"Write","arguments":{"file_path":"example.py",'
        '"lines":["0|def main():","4|return 0","0|"]}}]\n'
        "```"
    )
    _, calls = ToolTranspiler.parse_tool_calls(
        text,
        allowed_tools={"bash"},
        tool_definitions=[bash_tool],
        protocol="json-fn",
    )
    assert call_names(calls) == ["bash"]
    assert "python" in calls[0]["function"]["arguments"]
    assert "example.py" in calls[0]["function"]["arguments"]


def test_resolve_tool_protocol_env_and_explicit(monkeypatch):
    monkeypatch.delenv("WEBGPT_TOOL_PROTOCOL", raising=False)
    assert resolve_tool_protocol() == "xml"
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "json-fn")
    assert resolve_tool_protocol() == "json-fn"
    assert resolve_tool_protocol("both") == "both"
    with pytest.raises(ValueError):
        resolve_tool_protocol("bogus")


def test_reexport_identity():
    assert ToolTranspilerFromUtils is ToolTranspiler


# ---------------------------------------------------------------------------
# (b) broken JSON fails closed with MalformedToolCall
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '```json\n[{"name":"get_weather","arguments":{"location":}}]\n```',
        "[{'name':'get_weather','arguments':{'location':'Hanoi'}}]",  # single quotes
        '[{"name":"get_weather"',  # truncated bare array
        '```json\n[{"name":"get_weather","arguments":{"location":"Hanoi"}}',  # unterminated fence
        '{"oops":1}',  # wrong shape
        '[{"name":"get_weather"}]',  # missing arguments
        '[{"name":"get_weather","arguments":[1,2]}]',  # arguments not object
        '[{"name":"unknown_tool","arguments":{}}]',  # not in allowed_tools
        '[{"name":"get_weather","arguments":{"city":"Hanoi"}}]',  # schema violation
    ],
)
def test_json_fn_fail_closed(text):
    with pytest.raises(MalformedToolCall):
        parse(text, protocol="json-fn")


def test_json_fn_plain_prose_untouched():
    text = "Just answer the question in prose."
    clean, calls = parse(text, protocol="json-fn")
    assert calls == []
    assert clean == text


def test_json_fn_prose_with_non_tool_json_fence_is_not_a_call():
    text = "Here is data:\n```json\n{\"temp\": 30}\n```\nDone."
    # A fenced JSON object that does not look like tool calls at all stays prose
    # unless it starts the message; mid-prose fences without name/arguments are
    # rejected fail-closed under json-fn only when they carry tool-call shape.
    try:
        clean, calls = parse(text, protocol="json-fn")
        assert calls == [] and clean == text
    except MalformedToolCall:
        pass  # fail-closed is acceptable; no silent misparse either way


# ---------------------------------------------------------------------------
# (c) both mode accepts whichever matches first
# ---------------------------------------------------------------------------


def test_both_mode_accepts_json_fence():
    text = '```json\n[{"name":"get_weather","arguments":{"location":"Hanoi"}}]\n```'
    _, calls = parse(text, protocol="both")
    assert call_names(calls) == ["get_weather"]


def test_both_mode_accepts_bare_json():
    text = '{"name":"get_weather","arguments":{"location":"Hanoi"}}'
    _, calls = parse(text, protocol="both")
    assert call_names(calls) == ["get_weather"]


def test_both_mode_accepts_legacy_webgpt_tool_call():
    payload = {"name": "get_weather", "arguments": {"location": "Hanoi"}}
    text = "<WEBGPT_TOOL_CALL>\n" + json.dumps(payload) + "\n</WEBGPT_TOOL_CALL>"
    _, calls = parse(text, protocol="both")
    assert call_names(calls) == ["get_weather"]


XML_BLOCK = (
    "<tool_calls>\n"
    '  <invoke name="get_weather">\n'
    '    <parameter name="location">Hanoi</parameter>\n'
    "  </invoke>\n"
    "</tool_calls>"
)


def test_both_mode_accepts_xml_block():
    _, calls = parse(XML_BLOCK, protocol="both")
    assert call_names(calls) == ["get_weather"]


def test_both_mode_markup_wins_over_json_when_present():
    # Markup blocks are parsed first; a trailing JSON fence is outside-prose and
    # keeps the existing fail-closed prose-mixing rule (markup always wins).
    text = XML_BLOCK + '\n```json\n[{"name":"get_weather","arguments":{"location":"Hanoi"}}]\n```'
    with pytest.raises(MalformedToolCall):
        parse(text, protocol="both")


def test_both_mode_still_fail_closed_on_broken_json_attempt():
    with pytest.raises(MalformedToolCall):
        parse('{"name":"get_weather","arguments":{"location":}}', protocol="both")


# ---------------------------------------------------------------------------
# (d) xml default behavior unchanged (byte-identical)
# ---------------------------------------------------------------------------


def test_default_protocol_is_xml_and_ignores_json_shapes(monkeypatch):
    monkeypatch.delenv("WEBGPT_TOOL_PROTOCOL", raising=False)
    fenced = '```json\n[{"name":"get_weather","arguments":{"location":"Hanoi"}}]\n```'
    clean, calls = parse(fenced)
    assert calls == []
    assert clean == fenced  # returned untouched as plain text
    bare = '{"name":"get_weather","arguments":{"location":"Hanoi"}}'
    clean, calls = parse(bare)
    assert calls == []
    assert clean == bare


def test_xml_block_parses_identically_under_every_protocol(monkeypatch):
    baseline_clean, baseline_calls = parse(XML_BLOCK, protocol="xml")
    for protocol in (None, "json-fn", "both"):
        monkeypatch.delenv("WEBGPT_TOOL_PROTOCOL", raising=False)
        clean, calls = parse(XML_BLOCK, protocol=protocol)
        assert clean == baseline_clean
        assert [
            {k: v for k, v in c.items() if k != "id"} for c in calls
        ] == [{k: v for k, v in c.items() if k != "id"} for c in baseline_calls]


def test_build_instructions_byte_identical_default_vs_explicit_xml(monkeypatch):
    monkeypatch.delenv("WEBGPT_TOOL_PROTOCOL", raising=False)
    default_text = ToolTranspiler.build_tool_instructions(TOOLS)
    explicit_text = ToolTranspiler.build_tool_instructions(TOOLS, protocol="xml")
    env_text = None
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "xml")
    env_text = ToolTranspiler.build_tool_instructions(TOOLS)
    assert default_text == explicit_text == env_text
    assert "<tool_calls>" in default_text
    assert "Ordinary prose" in default_text


def test_parse_via_environment_variable(monkeypatch):
    fenced = '```json\n[{"name":"get_weather","arguments":{"location":"Hanoi"}}]\n```'
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "json-fn")
    _, calls = ToolTranspiler.parse_tool_calls(
        fenced,
        allowed_tools={"get_weather"},
        tool_definitions=TOOLS,
    )
    assert call_names(calls) == ["get_weather"]
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "bogus")
    with pytest.raises(ValueError):
        ToolTranspiler.parse_tool_calls(fenced)


# ---------------------------------------------------------------------------
# (e) build_tool_instructions follows the configured protocol
# ---------------------------------------------------------------------------


def test_build_instructions_json_fn_teaches_json_emit(monkeypatch):
    monkeypatch.delenv("WEBGPT_TOOL_PROTOCOL", raising=False)
    instructions = ToolTranspiler.build_tool_instructions(TOOLS, protocol="json-fn")
    assert "WEBGPT CONTROLLER TOOL PROTOCOL" in instructions
    assert "Available tools:" in instructions
    assert "```json" in instructions
    assert '"name":"TOOL_NAME"' in instructions
    assert "<tool_calls>" not in instructions
    assert "<invoke" not in instructions
    assert "CDATA" not in instructions
    assert "Never invent tool results" in instructions


def test_build_instructions_json_fn_via_env(monkeypatch):
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "json-fn")
    from_env = ToolTranspiler.build_tool_instructions(TOOLS)
    explicit = ToolTranspiler.build_tool_instructions(TOOLS, protocol="json-fn")
    assert from_env == explicit


def test_build_instructions_json_fn_forced_choice_keeps_anchor():
    forced = ToolTranspiler.build_tool_instructions(
        TOOLS,
        {"type": "function", "function": {"name": "get_weather"}},
        protocol="json-fn",
    )
    assert "must call exactly the tool named get_weather" in forced
    assert "```json" in forced


def test_build_instructions_invalid_protocol_raises(monkeypatch):
    monkeypatch.delenv("WEBGPT_TOOL_PROTOCOL", raising=False)
    with pytest.raises(ValueError):
        ToolTranspiler.build_tool_instructions(TOOLS, protocol="bogus")


def test_runtime_helper_reads_env(monkeypatch):
    from gpt.gateway.runtime import _webgpt_tool_protocol

    monkeypatch.delenv("WEBGPT_TOOL_PROTOCOL", raising=False)
    assert _webgpt_tool_protocol() == "xml"
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "both")
    assert _webgpt_tool_protocol() == "both"
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "JSON-FN")  # case-insensitive
    assert _webgpt_tool_protocol() == "json-fn"
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "nope")
    with pytest.raises(ValueError):
        _webgpt_tool_protocol()


def test_correction_prompt_follows_protocol(monkeypatch):
    from gpt.gateway.runtime import _correction_prompt_for

    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "json-fn")
    prompt = _correction_prompt_for(
        "MALFORMED_TOOL",
        TOOLS,
        "auto",
        detail="test",
    )
    assert "```json" in prompt
    assert "<tool_calls>" not in prompt


# ---------------------------------------------------------------------------
# (e) MARKUP-ALLOW-PROSE (2026-08-25): soft mode tolerates prose mixed with
#     markup blocks (<tool_calls>/<cmd>); strict xml/json-fn stay fail-closed.
# ---------------------------------------------------------------------------


SOFT_MARKUP_MIXED = (
    "Sure, checking the weather now — one moment.\n"
    + XML_BLOCK
    + "\nLet me know if you need another city."
)


def test_soft_mode_markup_with_prose_parses():
    # Markup branch takes allow_prose from the caller (unlike the soft
    # non-markup path where parse_tool_calls() hardcodes True), so production
    # call sites pass it when protocol resolves to soft.
    clean, calls = ToolTranspiler.parse_tool_calls(
        SOFT_MARKUP_MIXED,
        allowed_tools={"get_weather"},
        tool_definitions=TOOLS,
        protocol="soft",
        allow_prose=True,
    )
    assert call_names(calls) == ["get_weather"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"location": "Hanoi"}
    assert clean is not None and "checking the weather" in clean


def test_soft_markup_default_still_fails_closed_without_opt_in():
    # Guard the strict-by-default contract: the parser itself never relaxes
    # the markup branch on its own.
    with pytest.raises(MalformedToolCall):
        ToolTranspiler.parse_tool_calls(
            SOFT_MARKUP_MIXED,
            allowed_tools={"get_weather"},
            tool_definitions=TOOLS,
            protocol="soft",
        )


def test_soft_mode_env_markup_with_prose_parses(monkeypatch):
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    _, calls = ToolTranspiler.parse_tool_calls(
        SOFT_MARKUP_MIXED,
        allowed_tools={"get_weather"},
        tool_definitions=TOOLS,
        allow_prose=True,
    )
    assert call_names(calls) == ["get_weather"]


def test_strict_xml_mode_markup_with_prose_still_fails_closed(monkeypatch):
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "xml")
    with pytest.raises(MalformedToolCall):
        parse(SOFT_MARKUP_MIXED, protocol="xml")


def test_assistant_turn_builder_allows_prose_markup_under_soft(monkeypatch):
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    turn = AssistantTurnBuilder.from_model_text(
        SOFT_MARKUP_MIXED, tools=TOOLS, tool_choice="auto"
    )
    assert [c["function"]["name"] for c in turn.tool_calls] == ["get_weather"]
    assert turn.finish_reason == "tool_calls"


def test_assistant_turn_builder_fail_closed_prose_markup_outside_soft(monkeypatch):
    for env in ("xml", "json-fn", "both"):
        monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", env)
        with pytest.raises(MalformedToolCall):
            AssistantTurnBuilder.from_model_text(
                SOFT_MARKUP_MIXED, tools=TOOLS, tool_choice="auto"
            )


def test_correction_issue_tolerates_prose_markup_only_under_soft(monkeypatch):
    from gpt.gateway.runtime import _tool_correction_issue

    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    accepted: list = []
    issue = _tool_correction_issue(
        SOFT_MARKUP_MIXED,
        tail=[],
        messages=[],
        tools=TOOLS,
        tool_choice="auto",
        accepted_calls_out=accepted,
    )
    assert issue is None
    assert [c["function"]["name"] for c in accepted] == ["get_weather"]

    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "xml")
    issue = _tool_correction_issue(
        SOFT_MARKUP_MIXED,
        tail=[],
        messages=[],
        tools=TOOLS,
        tool_choice="auto",
    )
    assert issue is not None and issue[0] == "MALFORMED_TOOL"
