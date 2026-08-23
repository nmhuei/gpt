import json

import pytest

from gpt.state import MalformedToolCall
from gpt.toolstream import ToolStreamSieve

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }
]


def block(path: str = "a.txt") -> str:
    return (
        "<WEBGPT_TOOL_CALL>\n"
        + json.dumps({"name": "read_file", "arguments": {"path": path}})
        + "\n</WEBGPT_TOOL_CALL>"
    )


def test_stream_sieve_never_leaks_split_tool_markup():
    raw = block()
    sieve = ToolStreamSieve(tools=TOOLS)
    emitted: list[str] = []
    for index in range(0, len(raw), 3):
        emitted.extend(sieve.feed(raw[index : index + 3]).text_deltas)
    final = sieve.finalize()
    emitted.extend(final.text_deltas)
    assert emitted == []
    assert len(final.tool_calls) == 1
    assert final.tool_calls[0]["function"]["name"] == "read_file"


def test_stream_sieve_text_round_trips_exactly():
    raw = "ordinary assistant text with <harmless xml> and markdown"
    sieve = ToolStreamSieve(tools=TOOLS)
    emitted: list[str] = []
    for index in range(0, len(raw), 4):
        emitted.extend(sieve.feed(raw[index : index + 4]).text_deltas)
    emitted.extend(sieve.finalize().text_deltas)
    assert "".join(emitted) == raw


def test_stream_sieve_mixed_prose_and_tool_sentinel_fails_closed():
    raw = "I will do this. " + block()
    sieve = ToolStreamSieve(tools=TOOLS)
    for index in range(0, len(raw), 5):
        sieve.feed(raw[index : index + 5])
    with pytest.raises(MalformedToolCall, match="ordinary assistant prose"):
        sieve.finalize()


def test_stream_sieve_schema_validation_is_fail_closed():
    raw = (
        "<WEBGPT_TOOL_CALL>\n"
        + json.dumps({"name": "read_file", "arguments": {"path": 123}})
        + "\n</WEBGPT_TOOL_CALL>"
    )
    sieve = ToolStreamSieve(tools=TOOLS)
    sieve.feed(raw)
    with pytest.raises(MalformedToolCall, match="must be of type string"):
        sieve.finalize()


def test_stream_sieve_required_tool_rejects_plain_text():
    sieve = ToolStreamSieve(tools=TOOLS, tool_choice="required")
    sieve.feed("plain final answer")
    with pytest.raises(MalformedToolCall, match="required tool"):
        sieve.finalize()
