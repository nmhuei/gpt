"""PARITY-P0-2: layered tool-refusal detection in the gateway correction loop.

Covers:
- layer 1: hard refusal markers still classify as TOOL_REFUSAL;
- layer 2: soft refusals (counter-questions, apologies without action,
  alternative offers, deferrals, hedged inability) inside a tool-directed
  context classify as TOOL_REFUSAL_SOFT and get the refusal-strength prompt;
- neutral prose (answers to questions that need no tool) is never flagged;
- a second consecutive prose refusal after one correction raises early instead
  of burning the whole correction budget (CLI retries fresh from scratch);
- the normal tool-call path and WEBGPT_MAX_CORRECTIONS behavior.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from gpt.api.server import create_api_app
from gpt.gateway.runtime import (
    CompletionRuntime,
    _correction_prompt_for,
    _looks_like_cyber_refusal,
    _looks_like_soft_tool_refusal,
    _soft_refusal_signal_categories,
    _tool_correction_issue,
)
from gpt.types import TurnResult

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "Bash",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
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

CONCEPTUAL_MESSAGES = [
    {"role": "user", "content": "explain what dependency injection is"}
]


def _issue(text, *, messages=DIRECTED_MESSAGES, tools=REPO_TOOLS, tail=None):
    return _tool_correction_issue(
        text,
        tail=[] if tail is None else tail,
        messages=messages,
        tools=tools,
        tool_choice=None,
    )


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    return session


def _turn(text: str, turn_id: str = "turn_x") -> TurnResult:
    return TurnResult(turn_id=turn_id, conversation_id="conv_refusal_detection", text=text)


# ---------------------------------------------------------------------------
# (a) Layer 1: hard refusal markers keep working
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I cannot execute that because tools are unavailable in this chat.",
        "Sorry, I can't create files directly from this chat session.",
        "I'm unable to run shell commands on your machine.",
        "I won't fabricate tool results for you.",
    ],
)
def test_hard_refusal_markers_still_classified_as_tool_refusal(text):
    assert _issue(text)[0] == "TOOL_REFUSAL"


# ---------------------------------------------------------------------------
# (b) Layer 2: soft refusals in a tool-directed context
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Happy to help! Could you tell me more about the project layout before we begin?",
        "I'd love to, but could you clarify which framework you want to use first?",
        "Unfortunately, I can't be sure this is what you need — alternatively, I could sketch the design here.",
        "Before I can write the files, please provide the following details about your environment.",
        "I'm not able to do that kind of work directly; instead you could copy these snippets yourself.",
    ],
)
def test_soft_refusals_in_tool_directed_context_are_classified(text):
    issue = _issue(text)
    assert issue is not None
    reason, detail = issue
    assert reason == "TOOL_REFUSAL_SOFT"
    assert "soft-refusal signals" in detail
    assert _looks_like_soft_tool_refusal(text)


def test_soft_refusal_signal_categories_are_reported():
    categories = _soft_refusal_signal_categories(
        "Could you tell me more? Once you share that, I will get started."
    )
    assert "counter_question" in categories
    assert "conditional_deferral" in categories


@pytest.mark.parametrize(
    ("reason", "expected_marker"),
    [
        ("TOOL_REFUSAL", "REFUSAL OVERRIDE"),
        ("TOOL_REFUSAL_SOFT", "REFUSAL OVERRIDE"),
        ("FALSE_COMPLETION", "Return ONLY one valid tool call block"),
        ("MULTI_TOOL", "Return ONLY one valid tool call block"),
    ],
)
def test_correction_prompt_variant_matches_reason(reason, expected_marker):
    prompt = _correction_prompt_for(reason, REPO_TOOLS, None, detail="detail")
    assert expected_marker in prompt
    if reason.startswith("TOOL_REFUSAL"):
        assert "obligated to act" in prompt
    else:
        assert "obligated to act" not in prompt


# ---------------------------------------------------------------------------
# (c) Neutral prose is never flagged as refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Sure! Could you tell me more about your use case? Meanwhile: dependency injection is a "
        "technique where an object receives its collaborators from outside rather than building them.",
        "A TypeError occurs when an operation is applied to an object of inappropriate type. "
        "Would you like me to go deeper into common causes?",
        "Here is a short overview of the pattern with a small code example.",
    ],
)
def test_neutral_prose_without_tool_need_is_not_flagged(text):
    assert _issue(text, messages=CONCEPTUAL_MESSAGES) is None


def test_prose_answer_after_real_tool_results_is_not_flagged():
    # Realistic shape: the assistant already ran Bash, the tool result is in
    # the tail, and the model answers in prose.  That is a legitimate final
    # answer -- even with soft-refusal phrasing it must not be reclassified.
    tail = [{"role": "tool", "content": "all tests passed"}]
    messages = [*DIRECTED_MESSAGES, {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "pytest"}'}}]}, {"role": "tool", "content": "all tests passed"}]
    text = (
        "Could you tell me more about what to improve? Based on the run above, all tests pass."
    )
    assert _issue(text, messages=messages, tail=tail) is None


# ---------------------------------------------------------------------------
# Integration: correction loop behavior over the HTTP API
# ---------------------------------------------------------------------------


SOFT_REFUSAL_TEXT = (
    "Happy to help! Could you tell me more about the project structure first?"
)

VALID_TOOL_BLOCK = (
    "<tool_calls>\n"
    '  <invoke name="Bash">\n'
    '    <parameter name="command"><![CDATA[pwd]]></parameter>\n'
    "  </invoke>\n"
    "</tool_calls>"
)


def _post_completion(monkeypatch, side_effect):
    app = create_api_app(headless=True)
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(side_effect=list(side_effect))
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))
    return app, server, session


def test_persistent_soft_refusal_raises_early_after_one_correction(monkeypatch):
    app, _server, session = _post_completion(
        monkeypatch,
        [_turn(SOFT_REFUSAL_TEXT, "turn_soft_1"), _turn(SOFT_REFUSAL_TEXT, "turn_soft_2")],
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

    assert response.status_code == 502
    payload = response.json()["error"]
    assert payload["code"] == "malformed_model_tool_call"
    message = payload["message"]
    assert "persistent tool refusal after correction" in message.lower()
    assert "TOOL_REFUSAL_SOFT" in message
    # Early raise: initial response + exactly one correction, no third send.
    assert session.send.await_count == 2
    corrections = [
        event
        for event in _server.trace.snapshot()
        if event.component == "completionruntime" and event.kind == "tool_correction"
    ]
    assert [event.metadata.get("reason") for event in corrections] == ["TOOL_REFUSAL_SOFT"]
    assert any(
        event.component == "completionruntime"
        and event.kind == "persistent_tool_refusal"
        for event in _server.trace.snapshot()
    )


def test_soft_refusal_then_valid_tool_call_succeeds_with_refusal_correction(monkeypatch):
    app, _server, session = _post_completion(
        monkeypatch,
        [_turn(SOFT_REFUSAL_TEXT, "turn_soft"), _turn(VALID_TOOL_BLOCK, "turn_fixed")],
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
    calls = response.json()["choices"][0]["message"]["tool_calls"]
    assert [call["function"]["name"] for call in calls] == ["Bash"]
    assert session.send.await_count == 2
    corrections = [
        event
        for event in _server.trace.snapshot()
        if event.component == "completionruntime" and event.kind == "tool_correction"
    ]
    assert [event.metadata.get("reason") for event in corrections] == ["TOOL_REFUSAL_SOFT"]


def test_normal_tool_call_path_is_untouched(monkeypatch):
    app, _server, session = _post_completion(monkeypatch, [_turn(VALID_TOOL_BLOCK)])

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
    assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert session.send.await_count == 1
    assert not [
        event
        for event in _server.trace.snapshot()
        if event.component == "completionruntime" and event.kind == "tool_correction"
    ]


# ---------------------------------------------------------------------------
# WEBGPT_MAX_CORRECTIONS env knob
# ---------------------------------------------------------------------------


def _runtime(**kwargs) -> CompletionRuntime:
    return CompletionRuntime(
        conversations=MagicMock(),
        lease_session=MagicMock(),
        **kwargs,
    )


def test_max_corrections_default_is_two(monkeypatch):
    monkeypatch.delenv("WEBGPT_MAX_CORRECTIONS", raising=False)
    assert _runtime().max_corrections == 2


def test_max_corrections_can_be_lowered_via_env(monkeypatch):
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "1")
    assert _runtime().max_corrections == 1


def test_max_corrections_rejects_negative_env(monkeypatch):
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "-1")
    with pytest.raises(ValueError):
        _runtime()


def test_webgpt_max_corrections_one_still_corrects_once_before_exhaustion(monkeypatch):
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "1")
    app, _server, session = _post_completion(
        monkeypatch,
        [_turn(SOFT_REFUSAL_TEXT, "turn_soft_a"), _turn(SOFT_REFUSAL_TEXT, "turn_soft_b")],
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

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "malformed_model_tool_call"
    assert session.send.await_count == 2


def test_cyber_classifier_refusal_is_narrowly_detected():
    assert _looks_like_cyber_refusal("This content can't be shown because of cybersecurity requests.")
    assert not _looks_like_cyber_refusal("I cannot run Bash from this chat.")
    assert not _looks_like_cyber_refusal("Let's review cybersecurity requests in general.")
