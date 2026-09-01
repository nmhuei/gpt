"""DISCOVER-FIRST POLICY (owner decision, T3 behavior follow-up).

Covers:
- (a) build_tool_instructions() carries the WORKSPACE POLICY rules block under
  every protocol value (xml default, json-fn, both), numbered after the
  existing rules;
- (b) the runtime correction prompt for counter-question deflections contains
  the discover-first line ("workspace files are ALREADY available...");
- (c) correction prompts for every other issue type are unchanged by the new
  branch (no discover-first injection);
- regression parity with tests/test_refusal_detection.py classification.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from gpt.api.server import create_api_app
from gpt.gateway.runtime import (
    _correction_prompt_for,
    _tool_correction_issue,
)
from gpt.toolcall import ToolTranspiler
from gpt.types import TurnResult

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

REPO_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object"},
        },
    }
    for name in ("Bash", "Read", "Edit", "Write")
]

DIRECTED_MESSAGES = [
    {"role": "user", "content": "create the project files and run pytest in this repo"}
]

DISCOVERY_LINE = (
    "The workspace files are ALREADY available in your current working "
    "directory; discover them yourself with ls/find instead of asking."
)

COUNTER_QUESTION_TEXT = (
    "Happy to help! Could you tell me more about where the project files live?"
)

APOLOGY_DECLINE_TEXT = (
    "I'm sorry, but I won't be able to work on local repositories."
)


# ---------------------------------------------------------------------------
# (a) WORKSPACE POLICY present in every protocol variant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("protocol", [None, "xml", "json-fn", "both"])
def test_instructions_contain_workspace_policy_for_every_protocol(protocol):
    kwargs = {} if protocol is None else {"protocol": protocol}
    instructions = ToolTranspiler.build_tool_instructions(REPO_TOOLS, **kwargs)
    assert "WORKSPACE POLICY" in instructions
    assert "Inspect the current directory BEFORE asking the controller" in instructions
    assert "Use filesystem tools (ls/find/cat)" in instructions
    assert "Ask ONLY if discovery fails or targets are genuinely ambiguous" in instructions
    assert "Workflow: DISCOVER (pwd/ls/find) -> INSPECT (file/strings/README)" in instructions


def test_policy_numbering_continues_after_existing_rules():
    # xml (and "both") use the markup RULES list ending at 12; json-fn ends at 10.
    xml_instructions = ToolTranspiler.build_tool_instructions(REPO_TOOLS, protocol="xml")
    assert "13) WORKSPACE POLICY (mandatory):" in xml_instructions
    json_instructions = ToolTranspiler.build_tool_instructions(
        REPO_TOOLS, protocol="json-fn"
    )
    assert "11) WORKSPACE POLICY (mandatory):" in json_instructions
    # The pre-existing last rule keeps its number in each variant.
    assert "12) DSML" in xml_instructions
    assert "10) Never invent tool results" in json_instructions


def test_json_fn_variant_keeps_protocol_anchors_with_policy():
    instructions = ToolTranspiler.build_tool_instructions(
        [{"type": "function", "function": WEATHER_TOOL["function"]}], protocol="json-fn"
    )
    assert "```json" in instructions
    assert "WORKSPACE POLICY" in instructions


# ---------------------------------------------------------------------------
# (b) counter-question corrections carry the discover-first line
# ---------------------------------------------------------------------------


def test_counter_question_issue_classified_as_soft_refusal():
    issue = _tool_correction_issue(
        COUNTER_QUESTION_TEXT,
        tail=[],
        messages=DIRECTED_MESSAGES,
        tools=REPO_TOOLS,
        tool_choice=None,
    )
    assert issue is not None
    reason, detail = issue
    assert reason == "TOOL_REFUSAL_SOFT"
    assert "counter_question" in detail


def test_correction_prompt_for_counter_question_contains_discover_first_line():
    prompt = _correction_prompt_for(
        "TOOL_REFUSAL_SOFT",
        REPO_TOOLS,
        None,
        detail="model deflected instead of calling controller tools "
        "(soft-refusal signals: counter_question)",
        counter_question=True,
    )
    assert DISCOVERY_LINE in prompt
    assert "REFUSAL OVERRIDE" in prompt


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    return session


def _turn(text: str, turn_id: str) -> TurnResult:
    return TurnResult(turn_id=turn_id, conversation_id="conv_discover_policy", text=text)


VALID_TOOL_BLOCK = (
    "<tool_calls>\n"
    '  <invoke name="Bash">\n'
    '    <parameter name="command"><![CDATA[pwd]]></parameter>\n'
    "  </invoke>\n"
    "</tool_calls>"
)


def _sent_prompts(session: MagicMock) -> list[str]:
    return [call.args[0] for call in session.send.call_args_list]


def test_runtime_loop_injects_discover_first_into_counter_question_correction(monkeypatch):
    app = create_api_app(headless=True)
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(COUNTER_QUESTION_TEXT, "turn_cq"),
            _turn(VALID_TOOL_BLOCK, "turn_fixed"),
        ]
    )
    monkeypatch.setattr(
        server, "get_or_create_session", AsyncMock(return_value=session)
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": DIRECTED_MESSAGES,
                "tools": REPO_TOOLS,
            },
        )

    assert response.status_code == 200
    assert session.send.await_count == 2
    prompts = _sent_prompts(session)
    assert DISCOVERY_LINE in prompts[1]
    # The initial task prompt is untouched.
    assert DISCOVERY_LINE not in prompts[0]


# ---------------------------------------------------------------------------
# (c) other issue types are NOT changed by the counter-question branch
# ---------------------------------------------------------------------------


def test_other_issue_prompts_do_not_get_discover_first_line():
    cases = {
        "TOOL_REFUSAL": "model denied access to controller-provided tools",
        "TOOL_REFUSAL_SOFT": "soft-refusal signals: apology_decline, hedged_inability",
        "FALSE_COMPLETION": "task requires a controller tool but model returned only prose",
        "MULTI_TOOL": "model returned 2 tool calls; exactly one is allowed",
        "MALFORMED_TOOL": "Tool XML/DSML blocks are incomplete or nested.",
        "MISSING_REQUIRED_TOOL": "tool_choice requires a tool call",
    }
    for reason, detail in cases.items():
        prompt = _correction_prompt_for(reason, REPO_TOOLS, None, detail=detail)
        assert DISCOVERY_LINE not in prompt, reason
        assert "discover them yourself" not in prompt, reason


def test_non_counter_question_soft_refusal_correction_is_unchanged(monkeypatch):
    issue = _tool_correction_issue(
        APOLOGY_DECLINE_TEXT,
        tail=[],
        messages=DIRECTED_MESSAGES,
        tools=REPO_TOOLS,
        tool_choice=None,
    )
    assert issue is not None
    reason, detail = issue
    assert reason == "TOOL_REFUSAL_SOFT"
    assert "counter_question" not in detail

    app = create_api_app(headless=True)
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(
        side_effect=[
            _turn(APOLOGY_DECLINE_TEXT, "turn_apology"),
            _turn(VALID_TOOL_BLOCK, "turn_fixed"),
        ]
    )
    monkeypatch.setattr(
        server, "get_or_create_session", AsyncMock(return_value=session)
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": DIRECTED_MESSAGES,
                "tools": REPO_TOOLS,
            },
        )

    assert response.status_code == 200
    prompts = _sent_prompts(session)
    assert DISCOVERY_LINE not in prompts[1]
    # Refusal-strength anchors from tests/test_refusal_detection.py survive.
    assert "REFUSAL OVERRIDE" in prompts[1]
    assert "obligated to act" in prompts[1]
