from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from gpt.api.conversations import ConversationRecord, ConversationStore
from gpt.api.messages import render_messages
from gpt.api.openai_types import format_openai_chat_response, format_openai_chunk
from gpt.api.tool_transpiler import ToolTranspiler
from gpt.session import ChatGPTWebSession
from gpt.state import (
    AuthRequired,
    BrowserDisconnected,
    ConversationNotFound,
    GenerationInterrupted,
    GenerationTimeout,
    MalformedToolCall,
    ModelUnavailable,
    RateLimited,
    UIChanged,
    WebChatError,
)
from gpt.types import ResponseCompleted, ResponseFailed, TurnResult

logger = logging.getLogger("gpt.webchat.api")


def _error(message: str, status: int, error_type: str, code: str | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": error_type, "code": code or error_type}},
        status_code=status,
    )


def _session_headers(session_id: str) -> dict[str, str]:
    return {"x-webgpt-session-id": session_id}


class WebChatAPIServer:
    """OpenAI-compatible, conversation-aware facade over ChatGPT Web."""

    def __init__(
        self,
        headless: bool = True,
        persistent: bool = False,
        profile_dir: str | None = None,
        executable_path: str | None = None,
        cdp_url: str | None = None,
    ):
        self.headless = headless
        self.persistent = persistent
        self.profile_dir = profile_dir
        self.executable_path = executable_path
        self.cdp_url = cdp_url
        self._session: ChatGPTWebSession | None = None
        self._session_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self.conversations = ConversationStore()
        self._active_gateway_session_id: str | None = None

    async def get_or_create_session(self) -> ChatGPTWebSession:
        async with self._session_lock:
            if self._session is None:
                try:
                    self._session = await ChatGPTWebSession.create(
                        headless=self.headless,
                        persistent=self.persistent,
                        profile_dir=self.profile_dir,
                        executable_path=self.executable_path,
                        cdp_url=self.cdp_url,
                    )
                except (AuthRequired, UIChanged, WebChatError):
                    raise
                except Exception as exc:
                    raise BrowserDisconnected(
                        "Chromium could not start; the profile may already be in use."
                    ) from exc
            return self._session

    async def close(self) -> None:
        async with self._session_lock:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def health(self, _request: Request) -> JSONResponse:
        session = self._session
        state = session.state.value if session else "not_started"
        authenticated = None
        if session is not None:
            try:
                authenticated = await session.ui_driver.auth_status() == "authenticated"
            except Exception:
                authenticated = False
        return JSONResponse(
            {
                "ok": state not in {"fatal_error", "browser_disconnected"},
                "status": "ok",
                "browser": "ready" if session and session.browser_manager.connected else "not_started",
                "authenticated": authenticated,
                "backend": state,
                "active_sessions": len(self.conversations),
            }
        )

    async def list_models(self, _request: Request) -> JSONResponse:
        # This stable alias always means "use the model currently selected by
        # ChatGPT Web".  Dynamic UI labels are added only when the driver has
        # confirmed that they are actual selectable menu entries.
        default_model: dict[str, Any] = {
            "id": "chatgpt-web",
            "object": "model",
            "created": 0,
            "owned_by": "chatgpt-web",
            "display_name": "ChatGPT Web default",
            "available": True,
        }
        data: list[dict[str, Any]] = [default_model]
        session = self._session
        if session is not None:
            try:
                for model in await session.models():
                    model_id = model.id or model.label
                    if model_id == "chatgpt-web":
                        default_model["display_name"] = model.label
                        default_model["available"] = model.available
                        continue
                    data.append(
                        {
                            "id": model_id,
                            "object": "model",
                            "created": 0,
                            "owned_by": "chatgpt-web",
                            "display_name": model.label,
                            "available": model.available,
                        }
                    )
            except Exception:
                logger.warning("dynamic_model_discovery_failed", exc_info=True)
        return JSONResponse({"object": "list", "data": data})

    async def chat_completions(self, request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return _error("Invalid JSON body", 400, "invalid_request_error")
        validation = self._validate_request(body)
        if isinstance(validation, JSONResponse):
            return validation
        messages, model, tools, tool_choice, stream = validation
        explicit_id = request.headers.get("x-webgpt-session-id")

        if stream:
            try:
                record, _, _ = self.conversations.resolve(
                    messages, model, tools, explicit_id, tool_choice
                )
            except KeyError:
                return _error("Unknown x-webgpt-session-id", 404, "session_not_found")
            except ValueError as exc:
                return _error(str(exc), 409, "conversation_conflict")
            return StreamingResponse(
                self._stream_request(
                    record.session_id, messages, model, tools, tool_choice
                ),
                media_type="text/event-stream",
                headers={
                    **_session_headers(record.session_id),
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        started = time.monotonic()
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        try:
            async with self._request_lock:
                record, tail, cached = self.conversations.resolve(
                    messages, model, tools, explicit_id, tool_choice
                )
                if cached:
                    assert record.last_response is not None
                    return JSONResponse(
                        record.last_response, headers=_session_headers(record.session_id)
                    )
                response, _ = await self._execute_turn(
                    record, tail, messages, model, tools, tool_choice
                )
            self._log_request(request_id, record, model, False, len(tools), started, None)
            return JSONResponse(response, headers=_session_headers(record.session_id))
        except KeyError:
            self._log_request(
                request_id, None, model, False, len(tools), started, "session_not_found"
            )
            return _error("Unknown x-webgpt-session-id", 404, "session_not_found")
        except Exception as exc:
            self._log_request(request_id, None, model, False, len(tools), started, type(exc).__name__)
            return self._map_exception(exc)

    @staticmethod
    def _validate_request(body: Any):
        if not isinstance(body, dict):
            return _error("Request body must be an object", 400, "invalid_request_error")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return _error("messages must be a non-empty array", 400, "invalid_request_error")
        if any(not isinstance(message, dict) for message in messages):
            return _error("Each message must be an object", 400, "invalid_request_error")
        allowed_roles = {"system", "developer", "user", "assistant", "tool"}
        if any(message.get("role") not in allowed_roles for message in messages):
            return _error("Unsupported message role", 400, "invalid_request_error")
        model = str(body.get("model") or "chatgpt-web")
        tools = body.get("tools") or []
        if not isinstance(tools, list):
            return _error("tools must be an array", 400, "invalid_request_error")
        try:
            ToolTranspiler.validate_tools(tools)
            if tools:
                ToolTranspiler.build_tool_instructions(tools, body.get("tool_choice", "auto"))
        except ValueError as exc:
            return _error(str(exc), 400, "invalid_request_error")
        return messages, model, tools, body.get("tool_choice", "auto"), body.get("stream") is True

    async def _position_session(
        self, session: ChatGPTWebSession, record: ConversationRecord, model: str
    ) -> None:
        if record.conversation_id:
            if session.conversation_id != record.conversation_id:
                await session.open(record.conversation_id)
        elif record.messages:
            if self._active_gateway_session_id != record.session_id:
                raise ConversationNotFound(
                    "This anonymous conversation cannot be restored after switching sessions."
                )
        else:
            await session.new_conversation()
        if model != "chatgpt-web":
            await session.select_model(model)

    async def _execute_turn(
        self,
        record: ConversationRecord,
        tail: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]],
        tool_choice: Any,
    ) -> tuple[dict[str, Any], TurnResult]:
        if not tail:
            raise ValueError("Request does not add a new message to this conversation.")
        self._validate_tool_result_correlation(record, tail)
        session = await self.get_or_create_session()
        await self._position_session(session, record, model)
        prompt = render_messages(
            tail, initial=not record.messages, tools=tools, tool_choice=tool_choice
        )
        result = await session.send(prompt)
        allowed = set(ToolTranspiler.validate_tools(tools))
        try:
            clean, calls = self._parse_model_output(result.text, allowed, tool_choice)
        except MalformedToolCall:
            self._debug_raw_output(result.text)
            raise
        response = format_openai_chat_response(clean, calls or None, model=model)
        assistant = response["choices"][0]["message"]
        self.conversations.commit(
            record,
            messages,
            assistant,
            response,
            model,
            tools,
            result.conversation_id,
            tool_choice,
        )
        self._active_gateway_session_id = record.session_id
        return response, result

    async def _stream_request(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]],
        tool_choice: Any,
    ):
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        async with self._request_lock:
            try:
                record, tail, cached = self.conversations.resolve(
                    messages, model, tools, session_id, tool_choice
                )
                if cached:
                    assert record.last_response is not None
                    async for chunk in self._stream_cached(record.last_response, model, completion_id):
                        yield chunk
                    return
                session = await self.get_or_create_session()
                await self._position_session(session, record, model)
                prompt = render_messages(
                    tail, initial=not record.messages, tools=tools, tool_choice=tool_choice
                )
                self._validate_tool_result_correlation(record, tail)
                session.drain_events()
                task = asyncio.create_task(session.send(prompt))
                async for event in session.events():
                    if isinstance(event, ResponseFailed):
                        raise WebChatError(event.reason)
                    elif isinstance(event, ResponseCompleted):
                        break
                result = await task
                allowed = set(ToolTranspiler.validate_tools(tools))
                try:
                    clean, calls = self._parse_model_output(
                        result.text, allowed, tool_choice
                    )
                except MalformedToolCall:
                    self._debug_raw_output(result.text)
                    raise
                if not tools:
                    # ChatGPT's rendered DOM is mutable while Markdown/code is
                    # being produced, so intermediate text is not an append-only
                    # stream and cannot safely be retracted once sent over SSE.
                    # Buffer until completion, then emit deterministic chunks.
                    # This preserves OpenAI stream semantics and guarantees that
                    # concatenated deltas equal the final assistant content.
                    final_text = clean or ""
                    for offset in range(0, len(final_text), 24):
                        yield format_openai_chunk(
                            delta_content=final_text[offset : offset + 24],
                            model=model,
                            completion_id=completion_id,
                        )
                elif clean:
                    yield format_openai_chunk(
                        delta_content=clean, model=model, completion_id=completion_id
                    )
                if calls:
                    yield format_openai_chunk(
                        delta_tool_calls=calls,
                        finish_reason="tool_calls",
                        model=model,
                        completion_id=completion_id,
                    )
                else:
                    yield format_openai_chunk(
                        finish_reason="stop", model=model, completion_id=completion_id
                    )
                response = format_openai_chat_response(clean, calls or None, model=model)
                self.conversations.commit(
                    record,
                    messages,
                    response["choices"][0]["message"],
                    response,
                    model,
                    tools,
                    result.conversation_id,
                    tool_choice,
                )
                self._active_gateway_session_id = record.session_id
                yield "data: [DONE]\n\n"
            except Exception as exc:
                error = bytes(self._map_exception(exc).body).decode()
                yield f"data: {error}\n\n"
                yield "data: [DONE]\n\n"

    @staticmethod
    async def _stream_cached(response: dict[str, Any], model: str, completion_id: str):
        message = response["choices"][0]["message"]
        if message.get("content"):
            yield format_openai_chunk(
                delta_content=message["content"], model=model, completion_id=completion_id
            )
        calls = message.get("tool_calls")
        yield format_openai_chunk(
            delta_tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
            model=model,
            completion_id=completion_id,
        )
        yield "data: [DONE]\n\n"

    @staticmethod
    def _parse_model_output(
        text: str, allowed: set[str], tool_choice: Any
    ) -> tuple[str | None, list[dict[str, Any]]]:
        clean, calls = ToolTranspiler.parse_tool_calls(text, allowed_tools=allowed)
        if tool_choice == "none" and calls:
            raise MalformedToolCall("Model emitted a tool call while tool_choice=none.")
        required = tool_choice == "required" or isinstance(tool_choice, dict)
        if required and not calls:
            raise MalformedToolCall("Model did not emit the required tool call.")
        if isinstance(tool_choice, dict) and calls:
            selected = tool_choice.get("function", {}).get("name")
            if any(call["function"]["name"] != selected for call in calls):
                raise MalformedToolCall("Model called a tool different from tool_choice.")
        return clean, calls

    @staticmethod
    def _debug_raw_output(text: str) -> None:
        if os.environ.get("WEBGPT_DEBUG_RAW_OUTPUT") == "1":
            logger.warning("malformed_model_output=%r", text[:4_000])

    @staticmethod
    def _validate_tool_result_correlation(
        record: ConversationRecord, tail: list[dict[str, Any]]
    ) -> None:
        tool_results = [message for message in tail if message.get("role") == "tool"]
        if not tool_results:
            return
        pending: set[str] = set()
        for message in reversed(record.messages):
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    call_id = call.get("id") if isinstance(call, dict) else None
                    if isinstance(call_id, str):
                        pending.add(call_id)
                break
        result_ids = [message.get("tool_call_id") for message in tool_results]
        if not pending or any(call_id not in pending for call_id in result_ids):
            raise ValueError("tool_call_id does not match the pending assistant tool call.")
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("Duplicate tool result in one request.")

    @staticmethod
    def _map_exception(exc: Exception) -> JSONResponse:
        mapping: list[tuple[type[Exception], int, str]] = [
            (AuthRequired, 401, "authentication_error"),
            (ModelUnavailable, 400, "model_unavailable"),
            (ConversationNotFound, 404, "conversation_not_found"),
            (GenerationTimeout, 504, "generation_timeout"),
            (GenerationInterrupted, 409, "generation_interrupted"),
            (MalformedToolCall, 422, "malformed_tool_call"),
            (RateLimited, 429, "rate_limited"),
            (BrowserDisconnected, 503, "browser_disconnected"),
            (UIChanged, 502, "ui_changed"),
            (ValueError, 400, "invalid_request_error"),
            (WebChatError, 502, "webchat_error"),
        ]
        for error_type, status, code in mapping:
            if isinstance(exc, error_type):
                return _error(str(exc), status, code)
        logger.exception("unhandled_gateway_error", exc_info=exc)
        return _error("Internal gateway error", 500, "internal_error")

    @staticmethod
    def _log_request(
        request_id: str,
        record: ConversationRecord | None,
        model: str,
        stream: bool,
        tool_count: int,
        started: float,
        error_code: str | None,
    ) -> None:
        logger.info(
            "gateway_request %s",
            json.dumps(
                {
                    "request_id": request_id,
                    "gateway_session_id": record.session_id if record else None,
                    "duration_ms": int((time.monotonic() - started) * 1_000),
                    "model": model,
                    "stream": stream,
                    "tool_count": tool_count,
                    "error_code": error_code,
                },
                separators=(",", ":"),
            ),
        )


@asynccontextmanager
async def _lifespan(app: Starlette):
    yield
    await app.state.server.close()


def create_api_app(
    headless: bool = True,
    persistent: bool = False,
    profile_dir: str | None = None,
    executable_path: str | None = None,
    cdp_url: str | None = None,
) -> Starlette:
    server = WebChatAPIServer(
        headless=headless,
        persistent=persistent,
        profile_dir=profile_dir,
        executable_path=executable_path,
        cdp_url=cdp_url,
    )
    app = Starlette(
        routes=[
            Route("/health", server.health, methods=["GET"]),
            Route("/healthz", server.health, methods=["GET"]),
            Route("/v1/models", server.list_models, methods=["GET"]),
            Route("/v1/chat/completions", server.chat_completions, methods=["POST"]),
        ],
        lifespan=_lifespan,
    )
    app.state.server = server
    return app
