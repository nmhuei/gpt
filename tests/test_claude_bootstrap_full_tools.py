"""Full Claude Code bootstrap tool-catalog compatibility coverage.

The catalog mirrors the 25 controller tools declared in
``WEBGPT SESSION BOOTSTRAP.txt``.  Each realistic payload travels through the
same four boundaries a Claude Code request uses: prompt transpilation, strict
tool-call parsing, streamed tool-call filtering, and Anthropic adaptation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from gpt.api.protocol_adapters import parse_anthropic_request, response_to_anthropic
from gpt.state import MalformedToolCall
from gpt.toolcall import ToolTranspiler
from gpt.toolstream import ToolStreamSieve


@dataclass(frozen=True)
class ToolCase:
    name: str
    arguments: dict[str, Any]
    properties: dict[str, dict[str, Any]]
    required: tuple[str, ...] = ()


def _field(kind: str, **constraints: Any) -> dict[str, Any]:
    return {"type": kind, **constraints}


CASES = (
    ToolCase(
        "Agent",
        {
            "description": "Inspect gateway tests",
            "prompt": "Find the relevant tests and report their names.",
            "subagent_type": "Explore",
            "model": "sonnet",
            "run_in_background": False,
            "isolation": "worktree",
        },
        {
            "description": _field("string"),
            "prompt": _field("string"),
            "subagent_type": _field("string"),
            "model": _field("string", enum=["sonnet", "opus", "haiku", "fable"]),
            "run_in_background": _field("boolean"),
            "isolation": _field("string", enum=["worktree", "remote"]),
        },
        ("description", "prompt"),
    ),
    ToolCase(
        "Bash",
        {
            "command": "pytest -q tests/test_toolstream.py",
            "timeout": 120000,
            "description": "Run stream sieve tests",
            "run_in_background": False,
            "dangerouslyDisableSandbox": False,
        },
        {
            "command": _field("string"),
            "timeout": _field("number"),
            "description": _field("string"),
            "run_in_background": _field("boolean"),
            "dangerouslyDisableSandbox": _field("boolean"),
        },
        ("command",),
    ),
    ToolCase(
        "CronCreate",
        {"cron": "7 9 * * 1-5", "prompt": "Run the smoke test.", "recurring": True, "durable": False},
        {
            "cron": _field("string"),
            "prompt": _field("string"),
            "recurring": _field("boolean"),
            "durable": _field("boolean"),
        },
        ("cron", "prompt"),
    ),
    ToolCase("CronDelete", {"id": "cron_123"}, {"id": _field("string")}, ("id",)),
    ToolCase("CronList", {}, {}),
    ToolCase(
        "DesignSync",
        {
            "method": "write_files",
            "projectId": "project_123",
            "path": "components/button.html",
            "writes": ["components/button.html"],
            "deletes": ["components/old-button.html"],
            "planId": "plan_123",
            "files": [{"path": "components/button.html", "data": "<button>Save</button>"}],
            "paths": ["components/old-button.html"],
            "name": "Acme design system",
            "assets": [{"name": "Primary button", "path": "components/button.html"}],
            "localDir": "/workspace/design",
            "counts": {
                "total": 1,
                "bad": 0,
                "thin": 0,
                "variantsIdentical": 0,
                "iterations": 1,
            },
        },
        {
            "method": _field("string", enum=["write_files", "finalize_plan", "report_validate"]),
            "projectId": _field("string"),
            "path": _field("string"),
            "writes": _field("array", items=_field("string")),
            "deletes": _field("array", items=_field("string")),
            "planId": _field("string"),
            "files": _field("array", items=_field("object")),
            "paths": _field("array", items=_field("string")),
            "name": _field("string"),
            "assets": _field("array", items=_field("object")),
            "localDir": _field("string"),
            "counts": _field("object"),
        },
        ("method",),
    ),
    ToolCase(
        "Edit",
        {
            "file_path": "/workspace/app.py",
            "old_string": "return False",
            "new_string": "return True",
            "replace_all": False,
        },
        {
            "file_path": _field("string"),
            "old_string": _field("string"),
            "new_string": _field("string"),
            "replace_all": _field("boolean"),
        },
        ("file_path", "old_string", "new_string"),
    ),
    ToolCase(
        "EnterWorktree",
        {"name": "tool-catalog-tests", "path": "/workspace/.claude/worktrees/tool-catalog-tests"},
        {"name": _field("string"), "path": _field("string")},
    ),
    ToolCase(
        "ExitWorktree",
        {"action": "keep", "discard_changes": False},
        {"action": _field("string", enum=["keep", "remove"]), "discard_changes": _field("boolean")},
        ("action",),
    ),
    ToolCase("ListAgents", {"channel": "local", "q": "reviewer"}, {"channel": _field("string"), "q": _field("string")}),
    ToolCase(
        "LSP",
        {"operation": "hover", "filePath": "gpt/toolcall.py", "line": 572, "character": 7, "query": "ToolTranspiler"},
        {
            "operation": _field("string", enum=["hover", "workspaceSymbol"]),
            "filePath": _field("string"),
            "line": _field("integer"),
            "character": _field("integer"),
            "query": _field("string"),
        },
        ("operation", "filePath", "line", "character"),
    ),
    ToolCase(
        "Monitor",
        {
            "description": "Watch test output",
            "timeout_ms": 60000,
            "persistent": False,
            "command": "pytest -q",
            "ws": False,
        },
        {
            "description": _field("string"),
            "timeout_ms": _field("integer"),
            "persistent": _field("boolean"),
            "command": _field("string"),
            "ws": _field("boolean"),
        },
        ("description", "command"),
    ),
    ToolCase(
        "NotebookEdit",
        {
            "notebook_path": "/workspace/analysis.ipynb",
            "cell_id": "cell-1",
            "new_source": "print('updated')",
            "cell_type": "code",
            "edit_mode": "replace",
        },
        {
            "notebook_path": _field("string"),
            "cell_id": _field("string"),
            "new_source": _field("string"),
            "cell_type": _field("string", enum=["code", "markdown"]),
            "edit_mode": _field("string", enum=["replace", "insert", "delete"]),
        },
        ("notebook_path", "new_source"),
    ),
    ToolCase(
        "PushNotification",
        {"message": "The full tool catalog tests passed.", "status": "proactive"},
        {"message": _field("string"), "status": _field("string", enum=["proactive"])},
        ("message", "status"),
    ),
    ToolCase(
        "Read",
        {"file_path": "/workspace/README.md", "offset": 0, "limit": 80, "pages": "1-2"},
        {"file_path": _field("string"), "offset": _field("integer"), "limit": _field("integer"), "pages": _field("string")},
        ("file_path",),
    ),
    ToolCase(
        "ReportFindings",
        {
            "level": "high",
            "findings": [
                {
                    "file": "gpt/toolcall.py",
                    "line": 600,
                    "summary": "Tool name is not validated.",
                    "short_summary": "Unvalidated tool name",
                }
            ],
        },
        {
            "level": _field("string", enum=["low", "medium", "high", "xhigh", "max"]),
            "findings": _field("array", items=_field("object")),
        },
        ("level", "findings"),
    ),
    ToolCase(
        "ScheduleWakeup",
        {"time": "2026-08-22T10:00:00+07:00", "prompt": "Check test completion."},
        {"time": _field("string"), "prompt": _field("string")},
        ("time", "prompt"),
    ),
    ToolCase("SendMessage", {"to": "reviewer", "message": "Please inspect the catalog test."}, {"to": _field("string"), "message": _field("string")}, ("to", "message")),
    ToolCase("Skill", {"skill": "code-review", "args": {"level": "high"}}, {"skill": _field("string"), "args": _field("object")}, ("skill",)),
    ToolCase("TaskOutput", {"task_id": "task_123", "block": False}, {"task_id": _field("string"), "block": _field("boolean")}, ("task_id",)),
    ToolCase("TaskStop", {"task_id": "task_123"}, {"task_id": _field("string")}, ("task_id",)),
    ToolCase(
        "WebFetch",
        {"url": "https://example.com/docs", "headers": {"Accept": "text/html"}},
        {"url": _field("string"), "headers": _field("object", additionalProperties=_field("string"))},
        ("url",),
    ),
    ToolCase("WebSearch", {"query": "Claude Code tool protocol"}, {"query": _field("string")}, ("query",)),
    ToolCase("Workflow", {"workflow": "review-changes", "inputs": {"path": "gpt/toolcall.py"}}, {"workflow": _field("string"), "inputs": _field("object")}, ("workflow",)),
    ToolCase("Write", {"file_path": "notes/catalog.txt", "content": "catalog verified\n"}, {"file_path": _field("string"), "content": _field("string")}, ("file_path", "content")),
)


def _openai_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": case.name,
                "description": f"Claude Code {case.name} controller tool.",
                "parameters": {
                    "type": "object",
                    "properties": case.properties,
                    "required": list(case.required),
                    "additionalProperties": False,
                },
            },
        }
        for case in CASES
    ]


TOOLS = _openai_tools()
TOOL_NAMES = {case.name for case in CASES}


def _tool_block(case: ToolCase) -> str:
    return "<WEBGPT_TOOL_CALL>\n" + json.dumps(
        {"name": case.name, "arguments": case.arguments}
    ) + "\n</WEBGPT_TOOL_CALL>"


def _anthropic_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": tool["function"]["name"],
            "description": tool["function"]["description"],
            "input_schema": tool["function"]["parameters"],
        }
        for tool in TOOLS
    ]


def _assert_parsed_call(call: dict[str, Any], case: ToolCase) -> None:
    function = call["function"]
    if case.name == "Write":
        # ToolTranspiler deliberately maps virtual Write to a workspace-safe
        # Bash command whenever Claude Code has supplied Bash.
        assert function["name"] == "Bash"
        arguments = json.loads(function["arguments"])
        assert arguments["command"].startswith("python -c ")
        assert "notes/catalog.txt" in arguments["command"]
        return
    assert function["name"] == case.name
    assert json.loads(function["arguments"]) == case.arguments


def test_bootstrap_catalog_has_all_25_declared_tools_and_prompt_fields():
    assert len(CASES) == 25
    assert len(TOOL_NAMES) == 25

    instructions = ToolTranspiler.build_tool_instructions(TOOLS)
    for case in CASES:
        assert f'"name":"{case.name}"' in instructions
        for field in case.properties:
            assert f'"{field}"' in instructions


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_bootstrap_tool_payloads_parse_and_validate(case: ToolCase):
    clean, calls = ToolTranspiler.parse_tool_calls(
        _tool_block(case),
        allowed_tools=TOOL_NAMES,
        tool_definitions=TOOLS,
    )

    assert clean is None
    assert len(calls) == 1
    _assert_parsed_call(calls[0], case)

    invalid = ToolCase(case.name, {**case.arguments, "unexpected": True}, case.properties)
    with pytest.raises(MalformedToolCall, match="Unexpected tool argument field"):
        ToolTranspiler.parse_tool_calls(
            _tool_block(invalid),
            allowed_tools=TOOL_NAMES,
            tool_definitions=TOOLS,
        )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_bootstrap_tool_calls_are_filtered_by_streaming_sieve(case: ToolCase):
    sieve = ToolStreamSieve(tools=TOOLS)
    streamed_text: list[str] = []
    call = _tool_block(case)
    for offset in range(0, len(call), 11):
        streamed_text.extend(sieve.feed(call[offset : offset + 11]).text_deltas)
    result = sieve.finalize()

    assert streamed_text == []
    assert result.text_deltas == []
    assert len(result.tool_calls) == 1
    _assert_parsed_call(result.tool_calls[0], case)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_anthropic_tool_use_and_result_roundtrip_for_bootstrap_tool(case: ToolCase):
    call_id = f"call_{case.name.lower()}"
    response = response_to_anthropic(
        {
            "model": "claude-code-local",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": case.name,
                                    "arguments": json.dumps(case.arguments),
                                },
                            }
                        ],
                    }
                }
            ],
        }
    )
    tool_use = response["content"][0]

    assert response["stop_reason"] == "tool_use"
    assert tool_use == {"type": "tool_use", "id": call_id, "name": case.name, "input": case.arguments}

    adapted = parse_anthropic_request(
        {
            "model": "claude-code-local",
            "max_tokens": 64,
            "tools": _anthropic_tools(),
            "messages": [
                {"role": "assistant", "content": [tool_use]},
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": call_id, "content": "ok"}
                    ],
                },
            ],
        }
    )

    assistant, result = adapted.request.messages
    assert assistant["tool_calls"][0]["id"] == call_id
    assert assistant["tool_calls"][0]["function"]["name"] == case.name
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == case.arguments
    assert result == {"role": "tool", "tool_call_id": call_id, "content": "ok"}
