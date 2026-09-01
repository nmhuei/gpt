from __future__ import annotations

import json
import time
from typing import Any

import httpx

from gpt.core.settings import Settings
from gpt.tools.process import ProcessRunner


def get_json(url: str, *, timeout: float = 4.0) -> tuple[int | None, dict[str, Any] | None]:
    try:
        response = httpx.get(url, timeout=timeout)
    except httpx.HTTPError:
        return None, None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload if isinstance(payload, dict) else None


def gateway_health(settings: Settings) -> tuple[int | None, dict[str, Any] | None]:
    return get_json(settings.base_url.rstrip("/") + "/health")


def ensure_gateway(settings: Settings, *, wait_seconds: float = 12.0) -> bool:
    status, payload = gateway_health(settings)
    if status == 200 and payload and payload.get("ok") is True:
        return True
    runner = ProcessRunner(default_timeout_seconds=10)
    result = runner.run(
        "systemctl --user start webgpt-gateway.service",
        cwd=settings.workspace,
    )
    if result.is_error:
        return False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        status, payload = gateway_health(settings)
        if status == 200 and payload and payload.get("ok") is True:
            return True
        time.sleep(0.25)
    return False


def restart_gateway(settings: Settings) -> bool:
    runner = ProcessRunner(default_timeout_seconds=15)
    result = runner.run(
        "systemctl --user restart webgpt-gateway.service",
        cwd=settings.workspace,
    )
    return not result.is_error and ensure_gateway(settings)


def json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def human_duration(seconds: Any) -> str:
    try:
        value = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


__all__ = [
    "ensure_gateway",
    "gateway_health",
    "get_json",
    "human_duration",
    "json_print",
    "restart_gateway",
]
