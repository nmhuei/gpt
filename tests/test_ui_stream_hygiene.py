"""LIVE-R3 stream hygiene on the browser/UI driver path.

Evidence 2026-08-24 (/tmp/cc-live-test2/t1.stdout): the CLI received the
literal assistant text "ThinkingAY OKGATEWAY OK" — the reasoning-channel
label glued onto the answer with no separator. The curl_transport F4 fix
does not cover the browser transport, so UIDriver.send must apply the same
anchored strip (same WEBGPT_STREAM_STRIP_PREFIX kill switch).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gpt.drivers.ui import (
    UIDriver,
    _strip_leading_noise,
    _strip_prefix_flag_enabled,
)
from gpt.types import ResponseCompleted, ResponseDelta

# ---------------------------------------------------------------------------
# (a) Unit: _strip_leading_noise semantics on cumulative DOM snapshots
# ---------------------------------------------------------------------------


def test_newline_variant_is_cut():
    stripped, decided = _strip_leading_noise("Thinking\nGATEWAY OK")
    assert stripped == "GATEWAY OK"
    assert decided


def test_glued_uppercase_variant_from_live_t1_evidence_is_cut():
    # Verbatim T1 artifact: label glued directly to answer text.
    stripped, decided = _strip_leading_noise("ThinkingAY OKGATEWAY OK")
    assert stripped == "AY OKGATEWAY OK"
    assert decided


def test_thought_colon_variant_is_cut():
    stripped, decided = _strip_leading_noise("Thought: the answer\nnext")
    assert stripped == "the answer\nnext"
    assert decided


def test_partial_head_is_held_back_not_fed():
    stripped, decided = _strip_leading_noise("Thi")
    assert stripped == ""
    assert not decided


def test_exact_noise_word_is_held_back_not_decided():
    # A DOM snapshot of exactly "Thinking" may still grow into either noise
    # or prose ("Thinking about..."), so it must be held, never committed.
    stripped, decided = _strip_leading_noise("Thinking")
    assert stripped == ""
    assert not decided


def test_lowercase_prose_starting_with_thinking_passes_intact():
    stripped, decided = _strip_leading_noise("Thinking about it carefully, the answer is 4.")
    assert stripped == "Thinking about it carefully, the answer is 4."
    assert decided


def test_ordinary_text_passes_intact():
    stripped, decided = _strip_leading_noise("GATEWAY OK")
    assert stripped == "GATEWAY OK"
    assert decided


def test_kill_switch_flag_defaults_on(monkeypatch):
    monkeypatch.delenv("WEBGPT_STREAM_STRIP_PREFIX", raising=False)
    assert _strip_prefix_flag_enabled() is True
    monkeypatch.setenv("WEBGPT_STREAM_STRIP_PREFIX", "0")
    assert _strip_prefix_flag_enabled() is False
    monkeypatch.setenv("WEBGPT_STREAM_STRIP_PREFIX", "1")
    assert _strip_prefix_flag_enabled() is True


# ---------------------------------------------------------------------------
# (b) Integration: UIDriver.send strips across snapshots, deltas and final text
# ---------------------------------------------------------------------------


def _harnessed_driver(snapshots: list[str], *, poll_interval: float = 0.005,
                      stable_grace: float = 0.02) -> tuple[UIDriver, MagicMock]:
    """UIDriver with all page interactions stubbed out; send() only sees the
    cumulative DOM snapshots returned by _extract_latest_response."""
    page = MagicMock()
    page.url = ""
    driver = UIDriver(page, poll_interval=poll_interval, stable_grace=stable_grace)

    async def _noop(*args, **kwargs):
        return None

    async def _composer():
        return AsyncMock()

    monkey_patches = {
        "dismiss_popups": _noop,
        "_raise_known_page_error": _noop,
        "get_composer": _composer,
        "get_send_button": lambda: _async_return(None),
        "get_stop_button": lambda: _async_return(None),
        "_assistant_count": lambda: _async_return(1),
        "_composer_usable": lambda: _async_return(True),
        "_first_visible": lambda *a, **k: _async_return(None),
        "conversation_id": lambda: None,
    }
    for name, fn in monkey_patches.items():
        setattr(driver, name, fn)

    iterator = iter(snapshots)

    async def extract_latest_response():
        try:
            return next(iterator)
        except StopIteration:
            return snapshots[-1]

    driver._extract_latest_response = extract_latest_response  # type: ignore[method-assign]
    return driver, page


async def _async_return(value):
    return value


@pytest.mark.anyio
async def test_ui_send_strips_thinking_prefix_from_deltas_and_final_text():
    driver, _page = _harnessed_driver(
        ["Thinking", "ThinkingAY OKGATEWAY OK", "ThinkingAY OKGATEWAY OK"]
    )
    events = []

    async def callback(event):
        events.append(event)

    result = await driver.send(text="reply with exactly: GATEWAY OK",
                               event_callback=callback, timeout_seconds=5.0)
    assert result.text == "AY OKGATEWAY OK"
    deltas = [e for e in events if isinstance(e, ResponseDelta)]
    completed = [e for e in events if isinstance(e, ResponseCompleted)]
    assert "".join(d.text or "" for d in deltas) == "AY OKGATEWAY OK"
    assert all("Thinking" not in (d.text or "") for d in deltas)
    assert completed and completed[-1].text == "AY OKGATEWAY OK"


@pytest.mark.anyio
async def test_ui_send_newline_variant_strips_cleanly():
    driver, _page = _harnessed_driver(
        ["Thinking\n42 is the answer.", "Thinking\n42 is the answer."]
    )
    result = await driver.send(text="hi", timeout_seconds=5.0)
    assert result.text == "42 is the answer."


@pytest.mark.anyio
async def test_ui_send_legit_prose_starting_with_word_thinking_untouched():
    text = "Thinking about it carefully, the answer is 4."
    driver, _page = _harnessed_driver([text, text])
    result = await driver.send(text="hi", timeout_seconds=5.0)
    assert result.text == text


@pytest.mark.anyio
async def test_ui_send_kill_switch_restores_raw_noise(monkeypatch):
    monkeypatch.setenv("WEBGPT_STREAM_STRIP_PREFIX", "0")
    raw = "ThinkingAY OKGATEWAY OK"
    driver, _page = _harnessed_driver([raw, raw])
    result = await driver.send(text="hi", timeout_seconds=5.0)
    assert result.text == raw


@pytest.mark.anyio
async def test_ui_send_pure_noise_turn_does_not_hang_and_yields_empty():
    # Whole response is just the ambiguous head: end quietly with empty text
    # instead of burning the whole timeout.
    driver, _page = _harnessed_driver(["Thinking", "Thinking"], poll_interval=0.005,
                                      stable_grace=0.05)
    result = await driver.send(text="hi", timeout_seconds=5.0)
    assert result.text == ""


# ---------------------------------------------------------------------------
# (c) LIVE-R3 correction prompt rewrite keeps its required anchors
# ---------------------------------------------------------------------------

_REPO_TOOLS = [
    {"type": "function", "function": {"name": "Bash", "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "Read", "parameters": {"type": "object"}}},
]


def _prompt_for(reason: str) -> str:
    from gpt.gateway.runtime import _correction_prompt_for

    return _correction_prompt_for(reason, _REPO_TOOLS, None, detail="detail")


def test_non_refusal_correction_prompt_has_system_requirement_framing():
    prompt = _prompt_for("FALSE_COMPLETION")
    assert "WEBGPT CONTROLLER CORRECTION" in prompt
    assert "Return ONLY one valid tool call block" in prompt
    assert "does NOT count" in prompt
    assert "exactly ONE <tool_calls> block" in prompt
    assert "automated tool-execution system" in prompt
    assert "obligated to act" not in prompt


def test_malformed_tool_correction_prompt_keeps_format_anchor():
    prompt = _prompt_for("MALFORMED_TOOL")
    assert "WEBGPT CONTROLLER CORRECTION" in prompt
    assert "exactly ONE <tool_calls> block" in prompt


def test_refusal_override_prompt_has_system_requirement_framing():
    for reason in ("TOOL_REFUSAL", "TOOL_REFUSAL_SOFT"):
        prompt = _prompt_for(reason)
        assert "REFUSAL OVERRIDE" in prompt
        assert "obligated to act" in prompt
        assert "exactly ONE valid" in prompt
        assert "automated tool-execution system" in prompt
