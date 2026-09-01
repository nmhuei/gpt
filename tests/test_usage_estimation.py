"""PARITY-P0-1 gate tests: usage estimation in Anthropic/OpenAI envelopes.

ChatGPT Web exposes no tokenizer, so the gateway estimates usage locally as
chars/4 (rounded up).  These tests pin that contract so Claude Code's
auto-compact heuristic receives non-zero, roughly-correct token counts.
"""

from __future__ import annotations

from gpt.api.openai_types import format_openai_usage_chunk
from gpt.api.protocol_adapters import (
    StreamUsageEstimator,
    anthropic_usage,
    estimate_text_tokens,
    estimate_tokens_from_chars,
    response_to_anthropic,
)

ANTHROPIC_USAGE_KEYS = {
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
}


def _completion_response(text: str) -> dict:
    return {
        "model": "claude-code-local",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }


def _assert_close(actual: int, expected: int, tolerance: float = 0.10) -> None:
    assert abs(actual - expected) <= expected * tolerance, (
        f"estimated {actual}, expected ~{expected} (+/-10%)"
    )


# ---------------------------------------------------------------------------
# (a) Streaming path: 400 chars of response text -> ~100 output tokens
# ---------------------------------------------------------------------------


def test_stream_message_start_usage_estimates_output_tokens():
    # The buffered SSE stream builds message_start from the envelope produced
    # by response_to_anthropic; its usage must already carry the full-text
    # output estimate.
    payload = response_to_anthropic(_completion_response("x" * 400), prompt_text="y" * 8000)
    _assert_close(payload["usage"]["output_tokens"], 100)


def test_stream_estimator_accumulates_deltas_to_full_estimate():
    estimator = StreamUsageEstimator(prompt_text="p" * 8000)
    start = estimator.snapshot()
    _assert_close(start["input_tokens"], 2000)
    assert start["output_tokens"] == 0

    for _ in range(40):
        estimator.add_delta("t" * 10)  # 400 chars total across deltas
    final = estimator.snapshot()
    _assert_close(final["output_tokens"], 100)
    _assert_close(final["input_tokens"], 2000)


def test_stream_estimator_output_tokens_grow_monotonically():
    estimator = StreamUsageEstimator()
    seen = [estimator.output_tokens]
    for _ in range(20):
        estimator.add_delta("abcdefgh")  # 8 chars each
        seen.append(estimator.output_tokens)
    assert seen == sorted(seen)
    _assert_close(seen[-1], 40)


# ---------------------------------------------------------------------------
# (b) Prompt estimation: 8000 chars -> ~2000 input tokens
# ---------------------------------------------------------------------------


def test_prompt_chars_estimate_input_tokens():
    assert estimate_tokens_from_chars(len("q" * 8000)) == 2000


def test_envelope_input_tokens_from_prompt_text():
    payload = response_to_anthropic(_completion_response("ok"), prompt_text="z" * 8000)
    _assert_close(payload["usage"]["input_tokens"], 2000)


def test_input_estimate_floors_at_one_token():
    assert estimate_tokens_from_chars(0) == 1
    assert estimate_tokens_from_chars(1) == 1


def test_empty_output_estimates_zero_tokens():
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens(None) == 0


# ---------------------------------------------------------------------------
# (c) Non-stream path mirrors the stream estimates
# ---------------------------------------------------------------------------


def test_nonstream_response_usage_matches_stream_estimates():
    text = "a" * 400
    nonstream = response_to_anthropic(_completion_response(text), prompt_text="s" * 8000)
    streamed = response_to_anthropic(_completion_response(text), prompt_text="s" * 8000)
    _assert_close(nonstream["usage"]["input_tokens"], 2000)
    _assert_close(nonstream["usage"]["output_tokens"], 100)
    assert nonstream["usage"] == streamed["usage"]


def test_tool_call_arguments_count_as_output_chars():
    import json

    response = {
        "model": "claude-code-local",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "Read", "arguments": json.dumps({"p": "x" * 396})},
                        }
                    ],
                }
            }
        ],
    }
    payload = response_to_anthropic(response)
    # 400 serialized chars of tool arguments -> ~100 estimated output tokens.
    _assert_close(payload["usage"]["output_tokens"], 100)


# ---------------------------------------------------------------------------
# (d) Schema: usage objects carry the full standard Anthropic key set
# ---------------------------------------------------------------------------


def test_anthropic_usage_schema_keys_are_complete():
    usage = anthropic_usage(7, 3)
    assert set(usage) == ANTHROPIC_USAGE_KEYS
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0


def test_response_envelope_usage_schema_is_complete():
    payload = response_to_anthropic(_completion_response("hello"), prompt_text="hi")
    assert set(payload["usage"]) == ANTHROPIC_USAGE_KEYS
    assert isinstance(payload["usage"]["input_tokens"], int)
    assert isinstance(payload["usage"]["output_tokens"], int)


def test_missing_prompt_still_reports_output_estimate():
    # Call sites not yet threading the prompt report input_tokens=0 but must
    # still emit a real output estimate and the full schema.
    payload = response_to_anthropic(_completion_response("b" * 400))
    assert payload["usage"]["input_tokens"] == 0
    _assert_close(payload["usage"]["output_tokens"], 100)
    assert set(payload["usage"]) == ANTHROPIC_USAGE_KEYS


# ---------------------------------------------------------------------------
# OpenAI usage chunk shares the same chars/4 formula
# ---------------------------------------------------------------------------


def test_openai_usage_chunk_estimates_from_text():
    import json

    chunk = json.loads(
        format_openai_usage_chunk(
            prompt_text="o" * 8000,
            completion_text="c" * 400,
        )
        .removeprefix("data: ")
        .strip()
    )
    assert chunk["usage"]["prompt_tokens"] == 2000
    assert chunk["usage"]["completion_tokens"] == 100
    assert chunk["usage"]["total_tokens"] == 2100


def test_openai_usage_chunk_defaults_unchanged():
    import json

    chunk = json.loads(format_openai_usage_chunk().removeprefix("data: ").strip())
    assert chunk["usage"]["prompt_tokens"] == 0
    assert chunk["usage"]["completion_tokens"] == 0
