import json

import pytest

from gpt.api.openai_types import format_openai_chat_response
from gpt.api.tool_transpiler import ToolTranspiler
from gpt.state import MalformedToolCall

TOOLS = [
    {
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
]


def tool_block(payload):
    return (
        "<WEBGPT_TOOL_CALL>\n"
        + json.dumps(payload)
        + "\n</WEBGPT_TOOL_CALL>"
    )


def test_build_tool_instructions_uses_explicit_sentinel():
    instructions = ToolTranspiler.build_tool_instructions(TOOLS)
    assert "get_weather" in instructions
    assert "WEBGPT_TOOL_CALL" in instructions
    assert "Ordinary prose" in instructions
    forced = ToolTranspiler.build_tool_instructions(
        TOOLS, {"type": "function", "function": {"name": "get_weather"}}
    )
    assert "must call exactly the tool named get_weather" in forced

    with pytest.raises(ValueError):
        ToolTranspiler.build_tool_instructions(TOOLS, "sometimes")


def test_parse_explicit_tool_call_and_openai_response():
    clean, calls = ToolTranspiler.parse_tool_calls(
        tool_block({"name": "get_weather", "arguments": {"location": "Hanoi"}}),
        allowed_tools={"get_weather"},
    )
    assert clean is None
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"location": "Hanoi"}
    response = format_openai_chat_response(clean, calls)
    assert response["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.parametrize(
    "output",
    [
        "<WEBGPT_TOOL_CALL>{bad}</WEBGPT_TOOL_CALL>",
        tool_block({"name": "get_weather"}),
        tool_block({"name": "unknown", "arguments": {}}),
        "prose " + tool_block({"name": "get_weather", "arguments": {}}),
        "<WEBGPT_TOOL_CALL>{\"name\":\"get_weather\",\"arguments\":{}}",
        tool_block({"name": "get_weather", "arguments": []}),
        tool_block({"name": "get_weather", "arguments": {}})
        + tool_block({"name": "get_weather", "arguments": {}}),
    ],
)
def test_malformed_or_ambiguous_tool_output_fails_closed(output):
    with pytest.raises(MalformedToolCall):
        ToolTranspiler.parse_tool_calls(output, allowed_tools={"get_weather"})


def test_ordinary_json_and_prose_are_not_tool_calls():
    text = 'Here is JSON: {"name":"get_weather","arguments":{}}'
    clean, calls = ToolTranspiler.parse_tool_calls(text, allowed_tools={"get_weather"})
    assert clean == text
    assert calls == []


def test_multiple_distinct_explicit_calls_are_supported():
    output = tool_block({"name": "get_weather", "arguments": {"location": "Hanoi"}})
    output += tool_block({"name": "get_weather", "arguments": {"location": "Saigon"}})
    clean, calls = ToolTranspiler.parse_tool_calls(
        output, allowed_tools={"get_weather"}
    )
    assert clean is None
    assert len(calls) == 2
