from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import ConsoleMessage, Page, Request, Response, WebSocket

from gpt.types import ProbeEvent


class NetworkRecorder:
    """Passively records Playwright page requests, responses, websockets, and console logs."""

    def __init__(self, page: Page, max_body_bytes: int = 500_000):
        self.page = page
        self.max_body_bytes = max_body_bytes
        self.events: list[ProbeEvent] = []
        self._seq = 0
        self._active_experiment_id: str | None = None
        self._attached = False
        self._pending: set[asyncio.Task[None]] = set()

    def set_experiment_id(self, experiment_id: str | None) -> None:
        self._active_experiment_id = experiment_id

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def attach(self) -> None:
        if self._attached:
            return
        self._attached = True

        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_request_failed)
        self.page.on("websocket", self._on_websocket)
        self.page.on("console", self._on_console)
        self.page.on("pageerror", self._on_page_error)

    def _on_request(self, request: Request) -> None:
        post_data = None
        try:
            post_data = request.post_data
            if post_data and len(post_data) > self.max_body_bytes:
                post_data = post_data[: self.max_body_bytes] + "...<TRUNCATED>"
        except Exception:
            pass

        event = ProbeEvent.create(
            source="playwright",
            kind="request",
            sequence=self._next_seq(),
            experiment_id=self._active_experiment_id,
            url=request.url,
            method=request.method,
            metadata={
                "resource_type": request.resource_type,
                "headers": request.headers,
                "post_data": post_data,
            },
        )
        self.events.append(event)

    def _on_response(self, response: Response) -> None:
        task = asyncio.create_task(self._process_response(response))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def flush(self) -> None:
        """Wait for response-body tasks so an experiment cannot lose tail events."""
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)

    async def _process_response(self, response: Response) -> None:
        body_snippet = None
        try:
            # Only read small text/json bodies to prevent stream interference
            content_type = response.headers.get("content-type", "").lower()
            if "json" in content_type or "text" in content_type:
                text = await response.text()
                if len(text) > self.max_body_bytes:
                    body_snippet = text[: self.max_body_bytes] + "...<TRUNCATED>"
                else:
                    body_snippet = text
        except Exception:
            pass

        event = ProbeEvent.create(
            source="playwright",
            kind="response",
            sequence=self._next_seq(),
            experiment_id=self._active_experiment_id,
            url=response.url,
            status=response.status,
            metadata={
                "status_text": response.status_text,
                "headers": response.headers,
                "body_snippet": body_snippet,
            },
        )
        self.events.append(event)

    def _on_request_failed(self, request: Request) -> None:
        event = ProbeEvent.create(
            source="playwright",
            kind="request_failed",
            sequence=self._next_seq(),
            experiment_id=self._active_experiment_id,
            url=request.url,
            method=request.method,
            metadata={"failure": request.failure},
        )
        self.events.append(event)

    def _on_websocket(self, ws: WebSocket) -> None:
        event = ProbeEvent.create(
            source="websocket",
            kind="ws_created",
            sequence=self._next_seq(),
            experiment_id=self._active_experiment_id,
            url=ws.url,
        )
        self.events.append(event)

        ws.on("framesent", lambda payload: self._on_ws_frame("sent", ws.url, payload))
        ws.on("framereceived", lambda payload: self._on_ws_frame("received", ws.url, payload))
        ws.on("close", lambda _ws: self._on_ws_close(ws.url))

    def _on_ws_frame(self, direction: str, url: str, payload: Any) -> None:
        data_str = str(payload)
        if len(data_str) > self.max_body_bytes:
            data_str = data_str[: self.max_body_bytes] + "...<TRUNCATED>"
        event = ProbeEvent.create(
            source="websocket",
            kind=f"ws_frame_{direction}",
            sequence=self._next_seq(),
            experiment_id=self._active_experiment_id,
            url=url,
            metadata={"payload": data_str},
        )
        self.events.append(event)

    def _on_ws_close(self, url: str) -> None:
        event = ProbeEvent.create(
            source="websocket",
            kind="ws_closed",
            sequence=self._next_seq(),
            experiment_id=self._active_experiment_id,
            url=url,
        )
        self.events.append(event)

    def _on_console(self, msg: ConsoleMessage) -> None:
        event = ProbeEvent.create(
            source="console",
            kind="console_message",
            sequence=self._next_seq(),
            experiment_id=self._active_experiment_id,
            metadata={
                "type": msg.type,
                "text": msg.text,
                "location": msg.location,
            },
        )
        self.events.append(event)

    def _on_page_error(self, error: Exception) -> None:
        event = ProbeEvent.create(
            source="console",
            kind="page_error",
            sequence=self._next_seq(),
            experiment_id=self._active_experiment_id,
            metadata={"error": str(error)},
        )
        self.events.append(event)
