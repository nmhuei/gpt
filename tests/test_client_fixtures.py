import json
from pathlib import Path

from gpt.requests import parse_chat_completion_request

FIXTURES = Path(__file__).parent / "fixtures" / "clients"


def test_current_opencode_title_request_fixture_is_accepted():
    body = json.loads(
        (FIXTURES / "opencode" / "title-request.json").read_text(encoding="utf-8")
    )
    request = parse_chat_completion_request(
        body, protocol="openai_chat", client="opencode"
    )
    assert request.client == "opencode"
    assert request.stream is True
    assert request.stream_include_usage is True
    assert request.max_tokens_advisory == 32000


def test_current_opencode_coding_request_fixture_is_accepted_with_tools():
    body = json.loads(
        (FIXTURES / "opencode" / "coding-request.json").read_text(encoding="utf-8")
    )
    request = parse_chat_completion_request(
        body, protocol="openai_chat", client="opencode"
    )
    assert request.client == "opencode"
    assert request.stream is True
    assert request.stream_include_usage is True
    assert request.max_tokens_advisory == 32000
    assert len(request.tools) == 9
    assert {tool["function"]["name"] for tool in request.tools} >= {
        "bash",
        "read",
        "apply_patch",
    }


def test_current_claude_code_simple_stream_fixture_is_accepted():
    from gpt.api.protocol_adapters import parse_anthropic_request

    body = json.loads(
        (FIXTURES / "claude-code" / "simple.json").read_text(encoding="utf-8")
    )
    adapted = parse_anthropic_request(body)
    request = adapted.request
    assert request.protocol == "anthropic_messages"
    assert request.stream is True
    assert request.max_tokens_advisory == 64000
    assert request.tools == []


def test_current_claude_code_tool_and_tool_result_fixtures_are_accepted():
    from gpt.api.protocol_adapters import parse_anthropic_request

    tool_body = json.loads(
        (FIXTURES / "claude-code" / "tools.json").read_text(encoding="utf-8")
    )
    result_body = json.loads(
        (FIXTURES / "claude-code" / "tool-result.json").read_text(encoding="utf-8")
    )
    first = parse_anthropic_request(tool_body).request
    second = parse_anthropic_request(result_body).request
    assert {tool["function"]["name"] for tool in first.tools} == {"Bash", "Edit", "Read"}
    assert first.stream is True
    assert any(message.get("role") == "tool" for message in second.messages)
    tool_results = [message for message in second.messages if message.get("role") == "tool"]
    assert tool_results[-1]["tool_call_id"] == "toolu_capture_bash"


def test_opencode_lowercase_bash_supports_gateway_virtual_write():
    from gpt.toolcall import ToolTranspiler

    body = json.loads(
        (FIXTURES / "opencode" / "coding-request.json").read_text(encoding="utf-8")
    )
    request = parse_chat_completion_request(
        body, protocol="openai_chat", client="opencode"
    )
    names = {tool["function"]["name"] for tool in request.tools}
    model_names = {
        tool["function"]["name"]
        for tool in ToolTranspiler.effective_model_tools(request.tools)
    }
    assert "bash" in names
    assert "Write" in model_names
    text = """<tool_calls>
  <invoke name="Write">
    <parameter name="file_path"><![CDATA[pkg/lower.py]]></parameter>
    <parameter name="lines"><![CDATA[0|def answer():
4|return 42
0|]]></parameter>
  </invoke>
</tool_calls>"""
    _clean, calls = ToolTranspiler.parse_tool_calls(
        text,
        allowed_tools=names,
        tool_definitions=request.tools,
    )
    assert calls[0]["function"]["name"] == "bash"
    arguments = json.loads(calls[0]["function"]["arguments"])
    assert "pkg/lower.py" in arguments["command"]

