"""P1-5-COUNT-TOKENS-ALIGN: count_tokens must agree with streaming usage.

``estimate_anthropic_input_tokens`` previously hashed the JSON-normalized
request while ``usage.input_tokens`` was estimated from the rendered prompt
(chars/4), so the two endpoints self-contradicted.  These property-style
tests lock both paths onto the same formula: render_messages(initial=True)
followed by ceil(chars/4), i.e. exactly what :class:`StreamUsageEstimator`
reports for input tokens on a fresh turn.
"""

from __future__ import annotations

import pytest

from gpt.api.protocol_adapters import (
    StreamUsageEstimator,
    estimate_anthropic_input_tokens,
    estimate_tokens_from_chars,
    parse_anthropic_request,
)
from gpt.api.requests import RequestValidationError
from gpt.promptcompat import render_messages


def _reference_input_tokens(body: dict) -> int:
    """The usage-path estimate for the same body (StreamUsageEstimator input)."""
    adapted = parse_anthropic_request(body)
    rendered = render_messages(
        adapted.request.messages,
        initial=True,
        tools=adapted.request.tools,
        tool_choice=adapted.request.tool_choice,
    )
    return StreamUsageEstimator(rendered).snapshot()["input_tokens"]


_PLAIN_TEXT_BODY = {
    "model": "claude-fable-5",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Summarize the parity audit report."}],
}

_SYSTEM_AND_TOOLS_BODY = {
    "model": "claude-fable-5",
    "max_tokens": 128,
    "system": "You are a careful controller. Follow tool protocol strictly.",
    "tools": [
        {
            "name": "browser_click",
            "description": "Click an element on the current page.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Element uid."},
                },
                "required": ["uid"],
            },
        },
        {
            "name": "browser_snapshot",
            "description": "Take an accessibility snapshot.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Open example.com."}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_01", "name": "browser_snapshot", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "page loaded"}
            ],
        },
    ],
}

_TOOL_CHOICE_FORCED_BODY = {
    "model": "claude-fable-5",
    "max_tokens": 64,
    "system": [{"type": "text", "text": "System prompt supplied as block array."}],
    "tool_choice": {"type": "tool", "name": "browser_click"},
    "tools": _SYSTEM_AND_TOOLS_BODY["tools"],
    "messages": [
        {
            "role": "user",
            "content": "x" * 500,  # long enough that ceil rounding is exercised
        }
    ],
}

_LONG_SYSTEM_BODY = {
    "model": "claude-fable-5",
    "max_tokens": 64,
    "system": ("Operational constraints paragraph. " * 80).strip(),
    "messages": [{"role": "user", "content": "Acknowledge."}],
}

_LONG_HISTORY_BODY = {
    "model": "claude-fable-5",
    "max_tokens": 256,
    "messages": [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}: " + "y" * 120}
        for i in range(24)
    ],
}

# Valid request whose only message content is empty: both estimators must
# still agree (and both floor at one input token).
_EMPTY_CONTENT_BODY = {
    "model": "claude-fable-5",
    "max_tokens": 16,
    "messages": [{"role": "user", "content": ""}],
}


@pytest.mark.parametrize(
    "body",
    [
        _PLAIN_TEXT_BODY,
        _SYSTEM_AND_TOOLS_BODY,
        _TOOL_CHOICE_FORCED_BODY,
        _LONG_SYSTEM_BODY,
        _LONG_HISTORY_BODY,
        _EMPTY_CONTENT_BODY,
    ],
    ids=[
        "plain-text",
        "system+tools+toolloop",
        "forced-tool-choice",
        "long-system",
        "long-history",
        "empty-content",
    ],
)
def test_count_tokens_matches_streaming_usage_estimate(body):
    """|count_tokens - estimator(render_messages)| must stay within 1 token."""
    counted = estimate_anthropic_input_tokens(body)
    reference = _reference_input_tokens(body)
    assert isinstance(counted, int) and counted >= 1
    assert abs(counted - reference) <= 1


def test_empty_content_yields_identical_estimates_on_both_paths():
    """A valid request whose only message content is the empty string still
    gets IDENTICAL estimates on count_tokens and the streaming usage path.
    The rendered prompt is never truly empty (session bootstrap plus role
    framing are injected), so this asserts path agreement rather than a
    specific floor value."""
    counted = estimate_anthropic_input_tokens(_EMPTY_CONTENT_BODY)
    reference = _reference_input_tokens(_EMPTY_CONTENT_BODY)
    assert counted >= 1
    assert counted == reference


def test_count_tokens_scales_with_prompt_size():
    small = estimate_anthropic_input_tokens(_PLAIN_TEXT_BODY)
    big_body = dict(
        _PLAIN_TEXT_BODY,
        messages=[{"role": "user", "content": _PLAIN_TEXT_BODY["messages"][0]["content"] * 20}],
    )
    big = estimate_anthropic_input_tokens(big_body)
    assert big > small


def test_count_tokens_empty_body_is_validation_error():
    with pytest.raises(RequestValidationError):
        estimate_anthropic_input_tokens({})


def test_count_tokens_formula_is_ceil_chars_over_four():
    adapted = parse_anthropic_request(_TOOL_CHOICE_FORCED_BODY)
    rendered = render_messages(
        adapted.request.messages,
        initial=True,
        tools=adapted.request.tools,
        tool_choice=adapted.request.tool_choice,
    )
    expected = estimate_tokens_from_chars(len(rendered))
    assert estimate_anthropic_input_tokens(_TOOL_CHOICE_FORCED_BODY) == max(1, -(-len(rendered) // 4)) == expected
