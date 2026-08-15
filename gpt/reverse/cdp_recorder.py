from __future__ import annotations

from typing import Any

from playwright.async_api import CDPSession, Page

from gpt.types import ProbeEvent


class CDPRecorder:
    """Introspects network and runtime events directly via Chrome DevTools Protocol."""

    def __init__(self, page: Page, max_body_bytes: int = 500_000):
        self.page = page
        self.max_body_bytes = max_body_bytes
        self.events: list[ProbeEvent] = []
        self._session: CDPSession | None = None
        self._seq = 0
        self._active_experiment_id: str | None = None

    def set_experiment_id(self, experiment_id: str | None) -> None:
        self._active_experiment_id = experiment_id

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def attach(self) -> None:
        if self._session is not None:
            return

        try:
            self._session = await self.page.context.new_cdp_session(self.page)
            await self._session.send("Network.enable")
            await self._session.send("Runtime.enable")

            self._session.on("Network.requestWillBeSent", self._on_request_will_be_sent)
            self._session.on("Network.responseReceived", self._on_response_received)
            self._session.on("Network.webSocketFrameSent", self._on_ws_frame_sent)
            self._session.on("Network.webSocketFrameReceived", self._on_ws_frame_received)
        except Exception:
            # Non-Chromium or CDP attachment unsupported
            self._session = None

    def _on_request_will_be_sent(self, params: dict[str, Any]) -> None:
        req = params.get("request", {})
        event = ProbeEvent.create(
            source="cdp",
            kind="request_will_be_sent",
            sequence=self._next_seq(),
            experiment_id=self._active_experiment_id,
            url=req.get("url"),
            method=req.get("method"),
            metadata={
                "request_id": params.get("requestId"),
                "headers": req.get("headers"),
                "post_data": req.get("postData"),
                "has_post_data": req.get("hasPostData"),
                "initiator": params.get("initiator"),
            },
        )
        self.events.append(event)

    def _on_response_received(self, params: dict[str, Any]) -> None:
        resp = params.get("response", {})
        event = ProbeEvent.create(
            source="cdp",
            kind="response_received",
            sequence=self._next_seq(),
            experiment_id=self._active_experiment_id,
            url=resp.get("url"),
            status=resp.get("status"),
            metadata={
                "request_id": params.get("requestId"),
                "status_text": resp.get("statusText"),
                "headers": resp.get("headers"),
                "mime_type": resp.get("mimeType"),
                "protocol": resp.get("protocol"),
            },
        )
        self.events.append(event)

    def _on_ws_frame_sent(self, params: dict[str, Any]) -> None:
        frame = params.get("response", {})
        payload = frame.get("payloadData", "")
        if len(payload) > self.max_body_bytes:
            payload = payload[: self.max_body_bytes] + "...<TRUNCATED>"
        event = ProbeEvent.create(
            source="cdp",
            kind="ws_frame_sent",
            sequence=self._next_seq(),
            experiment_id=self._active_experiment_id,
            metadata={
                "request_id": params.get("requestId"),
                "opcode": frame.get("opcode"),
                "payload": payload,
            },
        )
        self.events.append(event)

    def _on_ws_frame_received(self, params: dict[str, Any]) -> None:
        frame = params.get("response", {})
        payload = frame.get("payloadData", "")
        if len(payload) > self.max_body_bytes:
            payload = payload[: self.max_body_bytes] + "...<TRUNCATED>"
        event = ProbeEvent.create(
            source="cdp",
            kind="ws_frame_received",
            sequence=self._next_seq(),
            experiment_id=self._active_experiment_id,
            metadata={
                "request_id": params.get("requestId"),
                "opcode": frame.get("opcode"),
                "payload": payload,
            },
        )
        self.events.append(event)

    async def detach(self) -> None:
        if self._session is not None:
            try:
                await self._session.detach()
            except Exception:
                pass
            self._session = None
