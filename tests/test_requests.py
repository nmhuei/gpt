import json

import pytest

from gpt.api.model_registry import ModelRegistry, load_model_aliases
from gpt.api.requests import RequestValidationError, parse_chat_completion_request

TOOLS = [
    {
        "type": "function",
        "function": {"name": "lookup", "parameters": {"type": "object"}},
    }
]


def test_request_normalizer_accepts_openai_chat_shape():
    request = parse_chat_completion_request(
        {
            "model": "fast",
            "messages": [{"role": "developer", "content": "Be concise"}],
            "tools": TOOLS,
            "tool_choice": {"type": "function", "function": {"name": "lookup"}},
            "stream": True,
            "temperature": 0.3,
            "reasoning": {"effort": "HIGH"},
        }
    )
    assert request.requested_model == "fast"
    assert request.stream is True
    assert request.temperature == 0.3
    assert request.reasoning_effort == "high"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"messages": [{"role": "user", "content": "x"}], "stream": "yes"}, "stream"),
        ({"messages": [{"role": "user", "content": "x"}], "tool_choice": "bad"}, "tool_choice"),
        ({"messages": [{"role": "user", "content": "x"}], "tool_choice": "required"}, "requires"),
        ({"messages": [{"role": "user", "content": "x"}], "reasoning_effort": "extreme"}, "reasoning_effort"),
    ],
)
def test_request_normalizer_rejects_unsupported_values(payload, message):
    with pytest.raises(RequestValidationError, match=message):
        parse_chat_completion_request(payload)


def test_model_registry_resolves_only_explicit_aliases():
    registry = ModelRegistry({"coding": "GPT Coding"})
    default = registry.resolve("default")
    explicit = registry.resolve("coding")
    unknown = registry.resolve("A UI label")
    anthropic = registry.resolve("claude-fable-5")
    assert (default.ui_label, default.response_model) == (None, "chatgpt-web")
    assert (explicit.ui_label, explicit.response_model) == ("GPT Coding", "coding")
    assert (unknown.ui_label, unknown.response_model) == ("A UI label", "A UI label")
    assert (anthropic.ui_label, anthropic.response_model) == (None, "claude-fable-5")


def test_model_registry_loads_only_string_aliases(tmp_path):
    alias_file = tmp_path / "models.json"
    alias_file.write_text(
        json.dumps({"model_aliases": {"coding": "GPT Coding"}}), encoding="utf-8"
    )
    assert load_model_aliases(alias_file) == {"coding": "GPT Coding"}

    alias_file.write_text(json.dumps({"coding": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a string"):
        load_model_aliases(alias_file)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"top_p": 0.5}, "top_p"),
        ({"n": 2}, "n is unsupported"),
        ({"seed": 42}, "seed"),
        ({"logprobs": True}, "logprobs"),
        ({"response_format": {"type": "json_object"}}, "response_format"),
        ({"parallel_tool_calls": False}, "parallel_tool_calls"),
        ({"stream_options": {"include_usage": "yes"}}, "include_usage"),
        ({"stop": ["END"]}, "stop sequences"),
        ({"max_completion_tokens": 0}, "max_completion_tokens"),
        ({"unknown_future_field": 1}, "Unsupported request field"),
    ],
)
def test_request_contract_rejects_semantics_it_cannot_honor(extra, message):
    payload = {
        "model": "chatgpt-web",
        "messages": [{"role": "user", "content": "x"}],
        **extra,
    }
    with pytest.raises(RequestValidationError, match=message):
        parse_chat_completion_request(payload)


def test_request_contract_accepts_only_neutral_compatibility_values():
    request = parse_chat_completion_request(
        {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": "x"}],
            "top_p": 1,
            "n": 1,
            "presence_penalty": 0,
            "frequency_penalty": 0,
            "logprobs": False,
            "parallel_tool_calls": True,
            "stream_options": {"include_usage": True},
            "max_tokens": 32000,
            "max_completion_tokens": 32000,
            "response_format": {"type": "text"},
            "user": "local-agent",
        }
    )
    assert request.requested_model == "chatgpt-web"


def test_request_contract_accepts_opencode_openai_compatible_stream_shape():
    request = parse_chat_completion_request(
        {
            "model": "webgpt-opencode-fake",
            "messages": [
                {"role": "system", "content": "You are OpenCode."},
                {"role": "user", "content": "Reply exactly OK"},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": 32000,
            "tool_choice": "auto",
            "tools": TOOLS,
        }
    )
    assert request.requested_model == "webgpt-opencode-fake"
    assert request.stream is True
    assert request.tool_choice == "auto"
    assert request.tools == TOOLS
