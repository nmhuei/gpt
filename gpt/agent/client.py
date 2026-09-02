from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .events import AgentEvent

ANTHROPIC_VERSION = "2023-06-01"


@dataclass(slots=True)
class GatewayClient:
    base_url: str
    api_key: str
    timeout_seconds: float
    client: httpx.Client | None = None
    event_callback: Callable[[AgentEvent], None] | None = None
    session_id: str | None = None
    _owns_client: bool = field(init=False, repr=False)
    _first_round_done: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._owns_client = self.client is None
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout_seconds)
        self._first_round_done = False

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "authorization": f"Bearer {self.api_key}",
            "anthropic-version": ANTHROPIC_VERSION,
            "x-client-name": "webgpt-agent",
        }
        if self.session_id:
            headers["x-webgpt-session-id"] = self.session_id
        return headers

    def complete(
        self,
        *,
        model: str,
        max_tokens: int,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        round_index: int,
    ) -> dict[str, Any]:
        assert self.client is not None
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "auto"},
        }
        started = time.monotonic()
        response = self.client.post(
            self.base_url.rstrip("/") + "/v1/messages",
            headers=self._headers(),
            json=body,
            timeout=self.timeout_seconds,
        )
        # Sau round đầu tiên, KHÔNG gửi session_id ở các round sau
        self._first_round_done = True
        elapsed = time.monotonic() - started
        returned_session = response.headers.get("x-webgpt-session-id")
        if returned_session:
            self.session_id = returned_session
        if self.event_callback is not None:
            self.event_callback(
                AgentEvent(
                    kind="response",
                    round_index=round_index,
                    data={
                        "status_code": response.status_code,
                        "request_id": response.headers.get("request-id"),
                        "session_id": self.session_id,
                        "elapsed_seconds": round(elapsed, 3),
                    },
                )
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Gateway returned a non-object response.")
        return payload


__all__ = ["ANTHROPIC_VERSION", "GatewayClient"]
