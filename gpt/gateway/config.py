from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int | None = None,
) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _float(
    env: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum_exclusive: float | None = None,
) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if minimum_exclusive is not None and value <= minimum_exclusive:
        raise ValueError(f"{name} must be > {minimum_exclusive:g}")
    return value


@dataclass(frozen=True, slots=True)
class GatewayTuning:
    """Construction-time gateway/runtime tuning snapshot.

    These values intentionally snapshot once when a server/runtime is created.
    Runtime kill switches and debugging toggles stay as direct env reads so
    operators/tests can hot-toggle them without rebuilding the process.
    """

    generation_timeout_seconds: float = 600.0
    max_corrections: int = 2
    max_prompt_chars: int = 200_000
    response_session_cap: int = 512
    prompt_debug_dir: Path | None = None

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> GatewayTuning:
        env = os.environ if environ is None else environ
        prompt_raw = env.get("WEBGPT_PROMPT_DEBUG_DIR", "").strip()
        return cls(
            generation_timeout_seconds=_float(
                env,
                "WEBGPT_GENERATION_TIMEOUT",
                600.0,
                minimum_exclusive=0.0,
            ),
            max_corrections=_int(
                env,
                "WEBGPT_MAX_CORRECTIONS",
                2,
                minimum=0,
            ),
            max_prompt_chars=_int(
                env,
                "WEBGPT_MAX_PROMPT_CHARS",
                200_000,
                minimum=4_000,
            ),
            response_session_cap=_int(
                env,
                "WEBGPT_RESPONSE_SESSION_CAP",
                512,
                minimum=1,
            ),
            prompt_debug_dir=Path(prompt_raw).expanduser() if prompt_raw else None,
        )


__all__ = ["GatewayTuning"]
