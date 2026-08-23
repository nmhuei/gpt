"""curl_cffi implementation of the ChatGPT conversation transport."""

from __future__ import annotations

import inspect
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

try:  # Keep browser-only installations importable and easy to unit-test.
    from curl_cffi.requests import AsyncSession
except ImportError:  # pragma: no cover - exercised in minimal installations.
    AsyncSession = None  # type: ignore[assignment,misc]

from gpt.reverse.stream_parser import SSEDecoder
from gpt.state import AuthRequired, ProtocolChanged, RateLimited
from gpt.transport.token_manager import SentinelTokens, TokenManager
from gpt.types import SendRequest, TurnResult

CONVERSATION_URL = "https://chatgpt.com/backend-api/f/conversation"
_CF_CLEARANCE_COOKIE = "cf_clearance"
_COMPLETION_STATUSES = frozenset({"finished_successfully", "finished", "complete"})


class CurlCffiTransport:
    """Send ChatGPT turns with Chrome TLS impersonation and streamed SSE."""

    def __init__(
        self,
        token_manager: TokenManager,
        *,
        session: Any | None = None,
        conversation_url: str = CONVERSATION_URL,
    ) -> None:
        if session is None:
            if AsyncSession is None:
                raise RuntimeError(
                    "Hybrid transport requires curl_cffi; install the project dependencies."
                )
            session = AsyncSession(impersonate="chrome")
        self.token_manager = token_manager
        self._session = session
        self.conversation_url = conversation_url

    async def send(
        self,
        request: SendRequest,
        *,
        on_delta: Callable[[str, str], Awaitable[None] | None] | None = None,
    ) -> TurnResult:
        """POST directly to ChatGPT's backend and consume its SSE response."""
        started = time.monotonic()
        bundle = await self.token_manager.refresh_if_needed()
        if bundle.is_local_mock:
            return await self._send_local_mock(request, on_delta=on_delta, started=started)
        sentinel = await self.token_manager.get_sentinel_tokens(request.conversation_id)
        headers = self._build_headers(bundle, sentinel)
        payload = self._build_conversation_payload(request)
        response = await self._session.post(
            self.conversation_url,
            headers=headers,
            json=payload,
            stream=True,
            timeout=request.timeout_seconds,
        )
        try:
            await self._raise_for_status(response)
            result = await self._stream_sse(response, request, on_delta=on_delta)
            result.duration_ms = int((time.monotonic() - started) * 1_000)
            return result
        finally:
            close = getattr(response, "aclose", None)
            if close is not None:
                closed = close()
                if inspect.isawaitable(closed):
                    await closed

    async def _send_local_mock(
        self,
        request: SendRequest,
        *,
        on_delta: Callable[[str, str], Awaitable[None] | None] | None,
        started: float,
    ) -> TurnResult:
        """Serve the dev/test fallback without ever contacting ChatGPT.

        The runtime sees normal deltas and the usual tool-call sentinel, so
        Anthropic/Claude Code SSE and tool handling exercise the production
        protocol adapters rather than a separate mock endpoint.
        """
        text = self._local_mock_text(request.text)
        turn_id = f"turn_local_{uuid.uuid4().hex[:12]}"
        if on_delta is not None:
            for offset in range(0, len(text), 24):
                result = on_delta(text[offset : offset + 24], turn_id)
                if inspect.isawaitable(result):
                    await result
        return TurnResult(
            turn_id=turn_id,
            conversation_id=request.conversation_id or f"local_{uuid.uuid4().hex[:12]}",
            text=text,
            model=request.model.label if request.model else "local-mock",
            status="completed",
            duration_ms=int((time.monotonic() - started) * 1_000),
        )

    @staticmethod
    def _local_mock_text(prompt: str) -> str:
        """Produce deterministic text or one schema-compatible tool call."""
        user_messages = re.findall(
            r'<WEBGPT_MESSAGE role="user">\n(.+?)\n</WEBGPT_MESSAGE>', prompt, re.DOTALL
        )
        user_text = "request"
        if user_messages:
            for chunk in reversed(user_messages):
                raw_chunk = chunk.strip()
                extracted = ""
                try:
                    payload = json.loads(raw_chunk)
                    if isinstance(payload, dict):
                        raw_content = payload.get("content")
                        if isinstance(raw_content, str):
                            extracted = raw_content.strip()
                        elif isinstance(raw_content, list):
                            parts = [
                                block.get("text", "")
                                for block in raw_content
                                if isinstance(block, dict) and isinstance(block.get("text"), str)
                            ]
                            extracted = " ".join(parts).strip()
                    elif isinstance(payload, str):
                        extracted = payload.strip()
                except json.JSONDecodeError:
                    extracted = raw_chunk

                clean = re.sub(r"<system-reminder>.*?</system-reminder>", "", extracted, flags=re.DOTALL).strip()
                if clean:
                    user_text = clean
                    break

        declarations = re.search(r"Available tools: (\[.+?\])\n", prompt)
        has_tool_result = "<WEBGPT_TOOL_RESULT>" in prompt
        is_correction = "WEBGPT CONTROLLER CORRECTION:" in prompt
        requests_tool = bool(
            is_correction
            or re.match(r"^\s*(?:read|bash)\s", user_text, re.IGNORECASE)
            or re.search(r"\b(?:tool|command|execute|inspect|status|pyproject)\b|\brun\s", user_text, re.IGNORECASE)
        )
        if declarations and requests_tool and not has_tool_result:
            try:
                tools = json.loads(declarations.group(1))
            except json.JSONDecodeError:
                tools = []
            if isinstance(tools, list) and tools:
                selected = next(
                    (
                        tool
                        for tool in tools
                        if isinstance(tool, dict)
                        and tool.get("name") in {"Read", "read", "Bash", "bash"}
                    ),
                    tools[0],
                )
                if isinstance(selected, dict) and isinstance(selected.get("name"), str):
                    arguments = CurlCffiTransport._local_mock_arguments(selected)
                    payload = {
                        "name": selected["name"],
                        "arguments": arguments,
                    }
                    return "<WEBGPT_TOOL_CALL>\n" + json.dumps(payload) + "\n</WEBGPT_TOOL_CALL>"
        return CurlCffiTransport._format_conversational_reply(user_text)

    @staticmethod
    def _format_conversational_reply(user_text: str) -> str:
        cleaned = user_text.strip().casefold()
        if cleaned in {"hi", "hello", "hey", "chao", "chào", "xin chao", "xin chào"}:
            return "Xin chào! Tôi có thể giúp gì cho bạn hôm nay?"
        if any(q in cleaned for q in ["toio laf ai", "tôi là ai", "toi la ai", "who am i"]):
            return "Bạn là lập trình viên / người dùng trên hệ thống, và tôi là trợ lý AI (Claude Code) đồng hành cùng bạn để phân tích, lập trình và giải quyết tác vụ."
        if any(q in cleaned for q in ["ban la ai", "bạn là ai", "who are you"]):
            return "Tôi là Claude Code - trợ lý AI lập trình được kết nối trực tiếp qua WebGPT Gateway."
        if any(q in cleaned for q in ["giup gi", "giúp gì", "help", "can you help"]):
            return "Tôi có thể hỗ trợ bạn đọc/viết mã nguồn, chạy lệnh shell, kiểm thử phần mềm, debug và xây dựng toàn bộ dự án."
        return f"Tôi đã tiếp nhận yêu cầu: {user_text.strip()}. Bạn cần tôi thực hiện bước nào tiếp theo?"

    @staticmethod
    def _local_mock_arguments(tool: dict[str, Any]) -> dict[str, Any]:
        parameters = tool.get("parameters")
        if not isinstance(parameters, dict):
            return {}
        properties = parameters.get("properties")
        required = parameters.get("required")
        if not isinstance(properties, dict) or not isinstance(required, list):
            return {}
        arguments: dict[str, Any] = {}
        for name in required:
            if not isinstance(name, str):
                continue
            schema = properties.get(name)
            schema = schema if isinstance(schema, dict) else {}
            if name in {"file_path", "path", "filename"}:
                arguments[name] = "README.md"
            elif name in {"command", "cmd"}:
                arguments[name] = "pwd"
            elif schema.get("type") == "boolean":
                arguments[name] = False
            elif schema.get("type") in {"integer", "number"}:
                arguments[name] = 1
            elif schema.get("type") == "array":
                arguments[name] = []
            elif schema.get("type") == "object":
                arguments[name] = {}
            else:
                arguments[name] = "mock"
        return arguments

    def _build_conversation_payload(self, request: SendRequest) -> dict[str, Any]:
        """Build the browser-compatible subset of the conversation payload."""
        model = request.model.id if request.model and request.model.id else None
        model = model or (request.model.label if request.model else "auto")
        payload: dict[str, Any] = {
            "action": "next",
            "messages": [
                {
                    "id": str(uuid.uuid4()),
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [request.text]},
                }
            ],
            "model": model,
            "parent_message_id": str(uuid.uuid4()),
            "conversation_mode": {"kind": "primary_assistant"},
        }
        if request.conversation_id:
            payload["conversation_id"] = request.conversation_id
        if request.reasoning_effort:
            payload["thinking_effort"] = request.reasoning_effort
        return payload

    @staticmethod
    def _build_headers(bundle: Any, sentinel: SentinelTokens) -> dict[str, str]:
        """Build the complete browser credential envelope for direct SSE.

        Generation traffic never passes through Playwright or reads page DOM.
        The browser is used solely to mint this credential snapshot.
        """
        cookies = dict(bundle.cookies)
        if bundle.cf_clearance:
            cookies[_CF_CLEARANCE_COOKIE] = bundle.cf_clearance
        missing = []
        if not bundle.access_token:
            missing.append("Authorization")
        if not bundle.oai_device_id:
            missing.append("oai-device-id")
        if not cookies.get(_CF_CLEARANCE_COOKIE):
            missing.append("cf_clearance cookie")
        if not sentinel.requirements_token:
            missing.append("openai-sentinel-chat-requirements-token")
        if missing:
            raise AuthRequired(
                "Direct backend generation is missing required credentials: " + ", ".join(missing)
            )
        headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {bundle.access_token}",
            "Content-Type": "application/json",
            "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "oai-language": "en-US",
            "User-Agent": "Mozilla/5.0",
        }
        headers["oai-device-id"] = bundle.oai_device_id
        headers["openai-sentinel-chat-requirements-token"] = sentinel.requirements_token
        if sentinel.proof_token:
            headers["openai-sentinel-proof-token"] = sentinel.proof_token
        if sentinel.turnstile_token:
            headers["openai-sentinel-turnstile-token"] = sentinel.turnstile_token
        return headers

    async def _raise_for_status(self, response: Any) -> None:
        status = getattr(response, "status_code", 200)
        if status < 400:
            return
        if status in {401, 403}:
            raise AuthRequired(f"ChatGPT hybrid request was rejected ({status}).")
        if status == 429:
            raise RateLimited("ChatGPT hybrid request was rate limited.")
        raise ProtocolChanged(f"ChatGPT hybrid request failed with HTTP {status}.")

    async def _stream_sse(
        self,
        response: Any,
        request: SendRequest,
        *,
        on_delta: Callable[[str, str], Awaitable[None] | None] | None = None,
    ) -> TurnResult:
        decoder = SSEDecoder()
        text = ""
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        conversation_id = request.conversation_id
        model = request.model.label if request.model else None
        complete = False
        async for chunk in self._response_chunks(response):
            for record in decoder.feed(chunk):
                text, turn_id, conversation_id, model, is_complete, delta = self._consume_record(
                    record, text, turn_id, conversation_id, model
                )
                if delta and on_delta is not None:
                    callback_result = on_delta(delta, turn_id)
                    if inspect.isawaitable(callback_result):
                        await callback_result
                complete = complete or is_complete
        for record in decoder.finish():
            text, turn_id, conversation_id, model, is_complete, delta = self._consume_record(
                record, text, turn_id, conversation_id, model
            )
            if delta and on_delta is not None:
                callback_result = on_delta(delta, turn_id)
                if inspect.isawaitable(callback_result):
                    await callback_result
            complete = complete or is_complete
        if not complete and not text:
            raise ProtocolChanged("Conversation stream ended without an assistant response.")
        return TurnResult(
            turn_id=turn_id,
            conversation_id=conversation_id,
            text=text,
            model=model,
            status="completed" if complete or text else "failed",
        )

    @staticmethod
    async def _response_chunks(response: Any) -> AsyncIterator[bytes | str]:
        for name in ("aiter_bytes", "aiter_content"):
            iterator = getattr(response, name, None)
            if iterator is not None:
                async for chunk in iterator():
                    yield chunk
                return
        lines = getattr(response, "aiter_lines", None)
        if lines is None:
            raise ProtocolChanged("curl_cffi response does not expose a streaming iterator.")
        async for line in lines():
            yield f"{line}\n\n" if isinstance(line, str) else line + b"\n\n"

    @staticmethod
    def _consume_record(
        record: str,
        text: str,
        turn_id: str,
        conversation_id: str | None,
        model: str | None,
    ) -> tuple[str, str, str | None, str | None, bool, str]:
        if record == "[DONE]":
            return text, turn_id, conversation_id, model, True, ""
        try:
            payload = json.loads(record)
        except json.JSONDecodeError as exc:
            raise ProtocolChanged("Conversation SSE contained invalid JSON.") from exc
        if not isinstance(payload, dict):
            return text, turn_id, conversation_id, model, False, ""
        if payload.get("error"):
            raise ProtocolChanged("ChatGPT returned an error event in the conversation stream.")
        value = payload.get("conversation_id")
        if isinstance(value, str):
            conversation_id = value
        message = payload.get("message")
        delta = ""
        if isinstance(message, dict):
            value = message.get("id")
            if isinstance(value, str):
                turn_id = value
            value = message.get("metadata", {}).get("model_slug")
            if isinstance(value, str):
                model = value
            content = message.get("content")
            if isinstance(content, dict):
                parts = content.get("parts")
                if isinstance(parts, list):
                    candidate = "".join(part for part in parts if isinstance(part, str))
                    if candidate.startswith(text):
                        delta = candidate[len(text) :]
                        text = candidate
                    elif not text.startswith(candidate):
                        delta = candidate
                        text += candidate
            complete = message.get("status") in _COMPLETION_STATUSES
        else:
            complete = payload.get("status") in _COMPLETION_STATUSES
        return text, turn_id, conversation_id, model, complete, delta

    async def close(self) -> None:
        close = getattr(self._session, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
