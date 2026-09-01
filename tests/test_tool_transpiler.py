import json
import subprocess

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


def test_agent_tool_instructions_allow_explicit_fanout_only_for_agent_calls():
    agent_tools = [
        {
            "type": "function",
            "function": {
                "name": "Agent",
                "description": "Launch a subagent",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "prompt": {"type": "string"},
                    },
                    "required": ["description", "prompt"],
                },
            },
        }
    ]

    instructions = ToolTranspiler.build_tool_instructions(agent_tools)

    assert "explicit fan-out" in instructions
    assert "multiple Agent invokes" in instructions
    assert "Normally use exactly one <invoke>" in instructions


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


def test_tool_arguments_are_validated_against_declared_schema():
    valid = tool_block(
        {"name": "get_weather", "arguments": {"location": "Hanoi"}}
    )
    _, calls = ToolTranspiler.parse_tool_calls(
        valid,
        allowed_tools={"get_weather"},
        tool_definitions=TOOLS,
    )
    assert len(calls) == 1

    missing_required = tool_block(
        {"name": "get_weather", "arguments": {}}
    )
    with pytest.raises(MalformedToolCall, match="missing required"):
        ToolTranspiler.parse_tool_calls(
            missing_required,
            allowed_tools={"get_weather"},
            tool_definitions=TOOLS,
        )

    wrong_type = tool_block(
        {"name": "get_weather", "arguments": {"location": 123}}
    )
    with pytest.raises(MalformedToolCall, match="must be of type string"):
        ToolTranspiler.parse_tool_calls(
            wrong_type,
            allowed_tools={"get_weather"},
            tool_definitions=TOOLS,
        )


def test_tool_sentinel_inside_markdown_code_is_plain_text():
    fenced = (
        "```text\n"
        "<WEBGPT_TOOL_CALL>\n"
        '{"name":"get_weather","arguments":{"location":"Hanoi"}}\n'
        "</WEBGPT_TOOL_CALL>\n"
        "```"
    )
    clean, calls = ToolTranspiler.parse_tool_calls(
        fenced,
        allowed_tools={"get_weather"},
        tool_definitions=TOOLS,
    )
    assert clean == fenced
    assert calls == []

    inline = '`<WEBGPT_TOOL_CALL>{"name":"get_weather","arguments":{}}</WEBGPT_TOOL_CALL>`'
    clean, calls = ToolTranspiler.parse_tool_calls(
        inline,
        allowed_tools={"get_weather"},
        tool_definitions=TOOLS,
    )
    assert clean == inline
    assert calls == []


def test_tool_payload_repairs_raw_newline_inside_json_string():
    from gpt.toolcall import ToolTranspiler

    text = '<WEBGPT_TOOL_CALL>\n{"name":"Bash","arguments":{"command":"cat > file <<\'EOF\'\nhello\nEOF"}}\n</WEBGPT_TOOL_CALL>'
    _content, calls = ToolTranspiler.parse_tool_calls(
        text,
        allowed_tools={"Bash"},
        tool_definitions=[
            {
                "type": "function",
                "function": {
                    "name": "Bash",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                },
            }
        ],
    )
    assert calls[0]["function"]["name"] == "Bash"
    assert "hello" in calls[0]["function"]["arguments"]


BASH_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "execute shell commands",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    }
]


def test_virtual_write_is_advertised_when_only_bash_is_available():
    instructions = ToolTranspiler.build_tool_instructions(BASH_TOOL)
    assert '"name":"Bash"' in instructions
    assert '"name":"Write"' in instructions
    assert "gateway translates" in instructions


def test_virtual_write_rejects_python_content_without_indent_safe_lines():
    text = """<tool_calls>
  <invoke name="Write">
    <parameter name="file_path"><![CDATA[pkg/example.py]]></parameter>
    <parameter name="content"><![CDATA[def main():
    return "OK"
]]></parameter>
  </invoke>
</tool_calls>"""
    with pytest.raises(MalformedToolCall, match=r"requires indentation-safe Write\.lines"):
        ToolTranspiler.parse_tool_calls(
            text,
            allowed_tools={"Bash"},
            tool_definitions=BASH_TOOL,
        )


def test_virtual_write_lines_json_preserves_indentation(tmp_path):
    text = """<tool_calls>
  <invoke name="Write">
    <parameter name="file_path"><![CDATA[pkg/lines.py]]></parameter>
    <parameter name="lines"><![CDATA[["def main():", "    return 'LINES_OK'", ""]]]></parameter>
  </invoke>
</tool_calls>"""
    _clean, calls = ToolTranspiler.parse_tool_calls(
        text,
        allowed_tools={"Bash"},
        tool_definitions=BASH_TOOL,
    )
    arguments = json.loads(calls[0]["function"]["arguments"])
    assert calls[0]["function"]["name"] == "Bash"
    completed = subprocess.run(
        arguments["command"], shell=True, cwd=tmp_path, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "WEBGPT_WRITE_OK pkg/lines.py"
    assert (tmp_path / "pkg/lines.py").read_text() == (
        "def main():\n    return 'LINES_OK'\n"
    )



def test_virtual_write_indent_coded_lines_preserves_indentation(tmp_path):
    text = """<tool_calls>
  <invoke name="Write">
    <parameter name="file_path"><![CDATA[pkg/indentcoded.py]]></parameter>
    <parameter name="lines"><![CDATA[0|def answer():
4|return "INDENT_CODED_OK"
0|]]></parameter>
  </invoke>
</tool_calls>"""
    _clean, calls = ToolTranspiler.parse_tool_calls(
        text,
        allowed_tools={"Bash"},
        tool_definitions=BASH_TOOL,
    )
    arguments = json.loads(calls[0]["function"]["arguments"])
    completed = subprocess.run(
        arguments["command"], shell=True, cwd=tmp_path, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "pkg/indentcoded.py").read_text() == (
        'def answer():\n    return "INDENT_CODED_OK"\n'
    )


def test_virtual_write_indent_coded_lines_survives_collapsed_newlines(tmp_path):
    text = """<tool_calls>
  <invoke name="Write">
    <parameter name="file_path"><![CDATA[pkg/collapsed.py]]></parameter>
    <parameter name="lines"><![CDATA[0|def answer(): 4|return "COLLAPSED_OK" 0|]]></parameter>
  </invoke>
</tool_calls>"""
    _clean, calls = ToolTranspiler.parse_tool_calls(
        text,
        allowed_tools={"Bash"},
        tool_definitions=BASH_TOOL,
    )
    arguments = json.loads(calls[0]["function"]["arguments"])
    completed = subprocess.run(
        arguments["command"], shell=True, cwd=tmp_path, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "pkg/collapsed.py").read_text() == (
        'def answer():\n    return "COLLAPSED_OK"\n'
    )


def test_virtual_write_emits_success_confirmation_without_hiding_compile_status(tmp_path):
    text = """<tool_calls>
  <invoke name="Write">
    <parameter name="file_path"><![CDATA[pkg/status.py]]></parameter>
    <parameter name="lines"><![CDATA[0|def status(): 4|return "OK"]]></parameter>
  </invoke>
</tool_calls>"""
    _clean, calls = ToolTranspiler.parse_tool_calls(
        text,
        allowed_tools={"Bash"},
        tool_definitions=BASH_TOOL,
    )
    arguments = json.loads(calls[0]["function"]["arguments"])
    completed = subprocess.run(
        arguments["command"], shell=True, cwd=tmp_path, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "WEBGPT_WRITE_OK pkg/status.py"
    assert (tmp_path / "pkg/status.py").read_text() == (
        'def status():\n    return "OK"\n'
    )


def test_virtual_write_indent_coded_json_array_decodes_markers(tmp_path):
    text = """<tool_calls>
  <invoke name="Write">
    <parameter name="file_path"><![CDATA[pkg/array_markers.py]]></parameter>
    <parameter name="lines"><![CDATA[["0|def answer():", "4|return 42", "0|"]]]></parameter>
  </invoke>
</tool_calls>"""
    _clean, calls = ToolTranspiler.parse_tool_calls(
        text,
        allowed_tools={"Bash"},
        tool_definitions=BASH_TOOL,
    )
    arguments = json.loads(calls[0]["function"]["arguments"])
    completed = subprocess.run(
        arguments["command"], shell=True, cwd=tmp_path, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "pkg/array_markers.py").read_text() == (
        "def answer():\n    return 42\n"
    )


def test_virtual_write_rejects_invalid_python_before_client_execution():
    text = """<tool_calls>
  <invoke name="Write">
    <parameter name="file_path"><![CDATA[pkg/broken.py]]></parameter>
    <parameter name="lines"><![CDATA[0|def broken(:
4|return 1]]></parameter>
  </invoke>
</tool_calls>"""
    with pytest.raises(MalformedToolCall, match="Write content validation failed"):
        ToolTranspiler.parse_tool_calls(
            text,
            allowed_tools={"Bash"},
            tool_definitions=BASH_TOOL,
        )


@pytest.mark.parametrize("file_path", ["../escape.py", "/tmp/escape.py", "pkg/../../escape.py"])
def test_virtual_write_rejects_workspace_escape(file_path):
    text = f"""<tool_calls>
  <invoke name="Write">
    <parameter name="file_path"><![CDATA[{file_path}]]></parameter>
    <parameter name="lines"><![CDATA[0|value = 1]]></parameter>
  </invoke>
</tool_calls>"""
    with pytest.raises(MalformedToolCall, match="inside the client workspace"):
        ToolTranspiler.parse_tool_calls(
            text,
            allowed_tools={"Bash"},
            tool_definitions=BASH_TOOL,
        )


def test_virtual_write_rejects_symlink_escape_at_client_execution(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    text = """<tool_calls>
  <invoke name="Write">
    <parameter name="file_path"><![CDATA[escape/owned.py]]></parameter>
    <parameter name="lines"><![CDATA[0|value = 1]]></parameter>
  </invoke>
</tool_calls>"""
    _clean, calls = ToolTranspiler.parse_tool_calls(
        text,
        allowed_tools={"Bash"},
        tool_definitions=BASH_TOOL,
    )
    arguments = json.loads(calls[0]["function"]["arguments"])
    completed = subprocess.run(
        arguments["command"], shell=True, cwd=tmp_path, capture_output=True, text=True
    )
    assert completed.returncode != 0
    assert "workspace_escape" in completed.stderr
    assert not (outside / "owned.py").exists()


def test_virtual_write_rejects_invalid_json_before_client_execution():
    text = """<tool_calls>
  <invoke name="Write">
    <parameter name="file_path"><![CDATA[pkg/config.json]]></parameter>
    <parameter name="content"><![CDATA[{"broken": }]]></parameter>
  </invoke>
</tool_calls>"""
    with pytest.raises(MalformedToolCall, match="Write content validation failed"):
        ToolTranspiler.parse_tool_calls(
            text,
            allowed_tools={"Bash"},
            tool_definitions=BASH_TOOL,
        )
