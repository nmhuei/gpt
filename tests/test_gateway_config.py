from __future__ import annotations

import pytest

from gpt.gateway.config import GatewayTuning


def test_gateway_tuning_defaults_are_stable() -> None:
    tuning = GatewayTuning.from_environ({})
    assert tuning.generation_timeout_seconds == 600.0
    assert tuning.max_corrections == 2
    assert tuning.max_prompt_chars == 200_000
    assert tuning.response_session_cap == 512
    assert tuning.prompt_debug_dir is None


def test_gateway_tuning_reads_construction_time_env(tmp_path) -> None:
    tuning = GatewayTuning.from_environ(
        {
            "WEBGPT_GENERATION_TIMEOUT": "42.5",
            "WEBGPT_MAX_CORRECTIONS": "4",
            "WEBGPT_MAX_PROMPT_CHARS": "250000",
            "WEBGPT_RESPONSE_SESSION_CAP": "64",
            "WEBGPT_PROMPT_DEBUG_DIR": str(tmp_path),
        }
    )
    assert tuning.generation_timeout_seconds == 42.5
    assert tuning.max_corrections == 4
    assert tuning.max_prompt_chars == 250_000
    assert tuning.response_session_cap == 64
    assert tuning.prompt_debug_dir == tmp_path


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WEBGPT_GENERATION_TIMEOUT", "0"),
        ("WEBGPT_MAX_CORRECTIONS", "-1"),
        ("WEBGPT_MAX_PROMPT_CHARS", "3999"),
        ("WEBGPT_RESPONSE_SESSION_CAP", "0"),
    ],
)
def test_gateway_tuning_rejects_out_of_range_values(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        GatewayTuning.from_environ({name: value})


def test_gateway_tuning_garbled_values_keep_defaults() -> None:
    tuning = GatewayTuning.from_environ(
        {
            "WEBGPT_GENERATION_TIMEOUT": "nope",
            "WEBGPT_MAX_CORRECTIONS": "nope",
            "WEBGPT_MAX_PROMPT_CHARS": "nope",
            "WEBGPT_RESPONSE_SESSION_CAP": "nope",
        }
    )
    assert tuning == GatewayTuning()
