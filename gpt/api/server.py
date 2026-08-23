from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from gpt.api.openai_types import (
    format_openai_chat_response,
    format_openai_chunk,
    format_openai_usage_chunk,
)
from gpt.api.protocol_adapters import (
    estimate_anthropic_input_tokens,
    parse_anthropic_request,
    parse_responses_request,
    response_to_anthropic,
    response_to_responses,
)
from gpt.assistantturn import AssistantTurnBuilder
from gpt.completionruntime import CompletionRuntime
from gpt.conversations import ConversationRecord, ConversationStore
from gpt.model_registry import ModelRegistry
from gpt.requests import (
    ChatCompletionRequest,
    RequestValidationError,
    parse_chat_completion_request,
)
from gpt.state import (
    AnonymousSessionUnavailable,
    AuthRequired,
    BrowserDisconnected,
    CommitUnknown,
    ConversationConflict,
    ConversationNotFound,
    EmptyModelResponse,
    GenerationInterrupted,
    GenerationTimeout,
    MalformedToolCall,
    ModelUnavailable,
    RateLimited,
    UIChanged,
    WebChatError,
)
from gpt.toolstream import ToolStreamSieve
from gpt.tracing import RuntimeTraceBus
from gpt.transport.browser import BrowserManager
from gpt.transport.factory import ChatGPTWorkerFactory, WorkerQueueTimeout
from gpt.transport.hybrid import HybridWorkerFactory
from gpt.transport.session import ChatGPTWebSession
from gpt.types import TurnResult

logger = logging.getLogger("gpt.webchat.api")


def _error(message: str, status: int, error_type: str, code: str | None = None) -> JSONResponse:
    resolved_code = code or error_type
    retryable = resolved_code in {
        "generation_timeout",
        "web_ui_changed",
        "browser_disconnected",
        "worker_queue_timeout",
        "empty_model_response",
    }
    return JSONResponse(
        {
            "error": {
                "message": message,
                "type": error_type,
                "code": resolved_code,
                "retryable": retryable,
            }
        },
        status_code=status,
        headers={"x-should-retry": "true" if retryable else "false"},
    )


def _session_headers(session_id: str) -> dict[str, str]:
    return {"x-webgpt-session-id": session_id}


def _client_name(request: Request) -> str:
    user_agent = request.headers.get("user-agent", "").casefold()
    if "opencode" in user_agent:
        return "opencode"
    if "claude" in user_agent or "anthropic" in user_agent:
        return "claude-code"
    if "openai" in user_agent:
        return "openai-client"
    return "unknown"


def _anthropic_request_headers(request: Request) -> dict[str, str]:
    """Preserve Anthropic and Claude Code extensions without whitelisting betas."""
    return {
        name: value
        for name, value in request.headers.items()
        if name == "x-api-key"
        or name.startswith("anthropic-")
        or name.startswith("x-claude-code-")
    }


class _RequestTraceMiddleware:
    def __init__(self, app: Any, server: Any) -> None:
        self.app = app
        self.server = server

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        if scope.get("type") != "http" or not path.startswith("/v1/"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        protocol = {
            "/v1/chat/completions": "openai_chat",
            "/v1/responses": "openai_responses",
            "/v1/messages": "anthropic_messages",
            "/v1/messages/count_tokens": "anthropic_count_tokens",
            "/v1/models": "openai_models",
        }.get(path, "unknown")
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        start_event = self.server.trace.emit(
            "api",
            "request_start",
            metadata={
                "request_id": request_id,
                "client": _client_name(request),
                "protocol": protocol,
                "path": path,
            },
        )
        status_code = 500
        session_id: str | None = None
        finalized = False

        def finalize(error: str | None = None) -> None:
            nonlocal finalized
            if finalized:
                return
            finalized = True
            events = self.server.trace.snapshot(after_sequence=start_event.sequence)
            queue_ms = 0
            browser_ms = 0
            parse_ms = 0
            tool_count = 0
            correction_count = 0
            turn_id: str | None = None
            for event in events:
                if session_id is None or event.session_id != session_id:
                    continue
                if event.component == "completionruntime" and event.kind == "lease_acquired":
                    queue_ms += int(event.metadata.get("queue_ms") or 0)
                elif event.component == "completionruntime" and event.kind == "submit_completed":
                    browser_ms += int(event.metadata.get("browser_ms") or 0)
                    value = event.metadata.get("turn_id")
                    if isinstance(value, str):
                        turn_id = value
                elif event.component == "assistantturn" and event.kind == "parsed":
                    parse_ms += int(event.metadata.get("parse_ms") or 0)
                elif event.component == "completionruntime" and event.kind == "tool_correction":
                    correction_count += 1
                elif event.component == "promptcompat" and event.kind == "prompt_built":
                    tool_count = max(tool_count, int(event.metadata.get("tool_count") or 0))
            record = self.server.conversations.get(session_id) if session_id else None
            conversation_id = record.conversation_id if record else None
            self.server.trace.emit(
                "api",
                "request_completed",
                session_id=session_id,
                conversation_id=conversation_id,
                metadata={
                    "request_id": request_id,
                    "client": _client_name(request),
                    "protocol": protocol,
                    "gateway_session_id": session_id,
                    "browser_conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "duration_ms": int((time.monotonic() - started) * 1_000),
                    "queue_ms": queue_ms,
                    "browser_ms": browser_ms,
                    "parse_ms": parse_ms,
                    "tool_count": tool_count,
                    "correction_count": correction_count,
                    "status": "ok" if status_code < 400 and error is None else "error",
                    "http_status": status_code,
                    "error": error or (None if status_code < 400 else f"http_{status_code}"),
                },
            )

        async def traced_send(message: dict[str, Any]) -> None:
            nonlocal status_code, session_id
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
                for raw_name, raw_value in message.get("headers", []):
                    if raw_name.lower() == b"x-webgpt-session-id":
                        session_id = raw_value.decode("latin-1")
                        break
            if message.get("type") == "http.response.body" and not message.get("more_body", False):
                finalize()
            await send(message)

        try:
            await self.app(scope, receive, traced_send)
        except Exception as exc:
            finalize(type(exc).__name__)
            raise


def _sse_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _anthropic_error(response: JSONResponse) -> JSONResponse:
    """Translate the shared local error taxonomy into Anthropic's envelope."""
    payload = json.loads(bytes(response.body))
    error = payload["error"]
    status_by_error = {
        400: (400, "invalid_request_error"),
        401: (401, "authentication_error"),
        403: (401, "authentication_error"),
        404: (404, "not_found_error"),
        429: (429, "rate_limit_error"),
    }
    status, err_type = status_by_error.get(response.status_code, (500, "api_error"))
    retryable = error.get("retryable") is True
    headers = {"x-should-retry": "true" if retryable else "false"}
    if retryable and status == 429:
        headers["retry-after"] = "1"
    return JSONResponse(
        {"type": "error", "error": {"type": err_type, "message": error["message"]}},
        status_code=status,
        headers=headers,
    )


def _anthropic_exception_error(server: Any, exc: Exception) -> JSONResponse:
    if isinstance(exc, AuthRequired):
        return _anthropic_error(_error(str(exc), 401, "authentication_error"))
    mapped = server._map_exception(exc)
    mapped_payload = json.loads(bytes(mapped.body))
    if mapped_payload["error"].get("code") == "internal_error":
        return _anthropic_error(_error(str(exc) or "Internal gateway error", 500, "api_error"))
    return _anthropic_error(mapped)



class WebChatAPIServer:
    """OpenAI-compatible, conversation-aware facade over ChatGPT Web."""

    def __init__(
        self,
        headless: bool = True,
        persistent: bool = False,
        profile_dir: str | None = None,
        executable_path: str | None = None,
        cdp_url: str | None = None,
        transport: str = "browser",
        model_aliases: Mapping[str, str] | None = None,
        conversation_store_path: str | None = None,
        conversation_ttl_seconds: float = 86_400,
        force_anthropic_initial_tool: bool = False,
        max_workers: int = 1,
        warm_workers: int = 1,
        queue_timeout: float = 30.0,
        trace_path: str | None = None,
        prompt_debug_dir: str | None = None,
        prewarm: bool = False,
        generation_timeout_seconds: float = 120.0,
        require_anonymous: bool = False,
    ):
        self.headless = headless
        self.persistent = persistent
        self.profile_dir = profile_dir
        self.executable_path = executable_path
        self.cdp_url = cdp_url
        if transport not in {"browser", "hybrid"}:
            raise ValueError("transport must be 'browser' or 'hybrid'")
        self.transport = transport
        self._session: ChatGPTWebSession | None = None
        self._session_lock = asyncio.Lock()
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._worker_factory: ChatGPTWorkerFactory | None = None
        if transport == "hybrid" or max_workers > 1:
            shared_browser = BrowserManager(
                headless=headless,
                persistent=persistent,
                profile_dir=profile_dir,
                executable_path=executable_path,
                cdp_url=cdp_url,
            )
            factory_class = HybridWorkerFactory if transport == "hybrid" else ChatGPTWorkerFactory
            self._worker_factory = factory_class(
                shared_browser,
                max_workers=max_workers,
                warm_workers=min(warm_workers, max_workers),
                queue_timeout=queue_timeout,
            )
        self.conversations = ConversationStore(
            state_path=conversation_store_path,
            ttl_seconds=conversation_ttl_seconds,
        )
        self._active_gateway_session_id: str | None = None
        self._response_sessions: dict[str, str] = {}
        self.force_anthropic_initial_tool = force_anthropic_initial_tool
        self.model_registry = ModelRegistry(model_aliases)
        self.prewarm = prewarm
        self.require_anonymous = require_anonymous
        if self.require_anonymous and max_workers != 1:
            raise ValueError("free_anonymous gateway mode requires max_workers=1")
        self.trace = RuntimeTraceBus(output_path=trace_path)
        self.completion_runtime = CompletionRuntime(
            self.conversations,
            self._lease_session,
            self.trace,
            prompt_debug_dir=prompt_debug_dir,
            generation_timeout_seconds=generation_timeout_seconds,
        )

    async def get_or_create_session(self) -> ChatGPTWebSession:
        async with self._session_lock:
            if self._session is None:
                try:
                    session = await ChatGPTWebSession.create(
                        headless=self.headless,
                        persistent=self.persistent,
                        profile_dir=self.profile_dir,
                        executable_path=self.executable_path,
                        cdp_url=self.cdp_url,
                    )
                    if self.require_anonymous:
                        auth_status = await session.ui_driver.auth_status()
                        if auth_status == "authenticated":
                            await session.close()
                            raise AnonymousSessionUnavailable(
                                "Certification requires an unauthenticated Free anonymous ChatGPT Web session."
                            )
                        if auth_status != "anonymous":
                            await session.close()
                            raise RateLimited(
                                "ChatGPT anonymous quota exhausted; redirected to login wall."
                            )

                    self._session = session
                except (AnonymousSessionUnavailable, AuthRequired, UIChanged, WebChatError):
                    raise
                except Exception as exc:
                    raise BrowserDisconnected(
                        "Chromium could not start; the profile may already be in use."
                    ) from exc
            return self._session

    def _conversation_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._conversation_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conversation_locks[session_id] = lock
        return lock

    async def discard_session(self, session: ChatGPTWebSession | None = None) -> None:
        async with self._session_lock:
            if (session is None or session is self._session) and self._session is not None:
                try:
                    await self._session.close()
                except Exception:
                    pass
                self._session = None


    @asynccontextmanager
    async def _lease_session(self):
        if self._worker_factory is None:
            session = await self.get_or_create_session()
            if self.require_anonymous:
                auth_status = await session.ui_driver.auth_status()
                if auth_status == "authenticated":
                    await self.discard_session(session)
                    raise AnonymousSessionUnavailable(
                        "Certification requires an unauthenticated Free anonymous ChatGPT Web session."
                    )
                if auth_status != "anonymous":
                    await self.discard_session(session)
                    raise RateLimited(
                        "ChatGPT anonymous quota exhausted; redirected to login wall."
                    )

            try:
                yield session
            except RateLimited:
                # A Free-anonymous 429 exhausts this ephemeral browser/session.
                # Do not resend the same logical request here, but discard the
                # browser completely so the next independent/client retry gets
                # a brand-new ephemeral browser process/context.
                await self.discard_session(session)
                raise
            except (AuthRequired, AnonymousSessionUnavailable):
                await self.discard_session(session)
                raise
            return
        async with self._worker_factory.lease() as session:
            yield session


    async def close(self) -> None:
        if self._worker_factory is not None:
            await self._worker_factory.close()
            self._worker_factory = None
        async with self._session_lock:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def liveness(self, _request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "status": "ok"})

    async def readiness(self, _request: Request) -> JSONResponse:
        try:
            async with self._lease_session() as session:
                if self.transport == "hybrid":
                    auth_status = "authenticated"
                    browser_connected = self._worker_factory.browser_manager.connected
                else:
                    auth_status = await session.ui_driver.auth_status()
                    browser_connected = session.browser_manager.connected
                ready = bool(
                    browser_connected
                    and session.state.value == "ready"
                    and auth_status in {"authenticated", "anonymous"}
                )
                payload: dict[str, Any] = {
                    "ready": ready,
                    "browser": (
                        "ready" if browser_connected else "disconnected"
                    ),
                    "authenticated": auth_status == "authenticated",
                    "auth_status": auth_status,
                    "backend": session.state.value,
                }
        except Exception as exc:
            return JSONResponse(
                {
                    "ready": False,
                    "browser": "unavailable",
                    "authenticated": None,
                    "auth_status": "unknown",
                    "backend": type(exc).__name__,
                },
                status_code=503,
            )
        if self._worker_factory is not None:
            stats = await self._worker_factory.stats()
            payload["workers"] = {
                "max": stats.max_workers,
                "live": stats.live_workers,
                "idle": stats.idle_workers,
                "leased": stats.leased_workers,
                "queued": stats.queue_waiters,
            }
        return JSONResponse(payload, status_code=200 if ready else 503)

    async def health(self, _request: Request) -> JSONResponse:
        session = self._session
        state = session.state.value if session else "not_started"
        authenticated = None
        auth_status = "unknown"
        browser_state = "not_started"
        if session is not None:
            try:
                auth_status = await session.ui_driver.auth_status()
                authenticated = auth_status == "authenticated"
            except Exception:
                authenticated = False
            browser_state = (
                "ready" if session.browser_manager.connected else "disconnected"
            )
        payload: dict[str, Any] = {
            "ok": state not in {"fatal_error", "browser_disconnected"},
            "status": "ok",
            "browser": browser_state,
            "authenticated": authenticated,
            "auth_status": auth_status,
            "backend": state,
            "active_sessions": len(self.conversations),
        }
        if self._worker_factory is not None:
            stats = await self._worker_factory.stats()
            payload["workers"] = {
                "max": stats.max_workers,
                "live": stats.live_workers,
                "idle": stats.idle_workers,
                "leased": stats.leased_workers,
                "queued": stats.queue_waiters,
                "created": stats.created_workers,
                "closed": stats.closed_workers,
            }
            payload["browser"] = (
                "ready"
                if self._worker_factory.browser_manager.connected
                else "not_started"
            )
        return JSONResponse(payload)

    async def list_models(self, _request: Request) -> JSONResponse:
        # chatgpt-web is the stable request alias; every additional entry must
        # come from live UI discovery rather than an inferred subscription tier.
        default_model: dict[str, Any] = {
            "id": "chatgpt-web",
            "object": "model",
            "created": 0,
            "owned_by": "chatgpt-web",
            "display_name": "ChatGPT Web default",
            "available": True,
        }
        data: list[dict[str, Any]] = [default_model]
        runtime_active = self._session is not None or bool(
            self._worker_factory is not None
            and self._worker_factory.browser_manager.connected
        )
        if not runtime_active:
            return JSONResponse({"object": "list", "data": data})
        try:
            async with self._lease_session() as session:
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
                            "reasoning_efforts": model.reasoning_efforts,
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
        try:
            normalized = parse_chat_completion_request(
                body, protocol="openai_chat", client=_client_name(request)
            )
        except RequestValidationError as exc:
            return _error(str(exc), 400, "invalid_request_error")
        resolution = self.model_registry.resolve(normalized.requested_model)
        messages = normalized.messages
        model = resolution.response_model
        ui_model = resolution.ui_label
        tools = normalized.tools
        tool_choice = normalized.tool_choice
        stream = normalized.stream
        reasoning_effort = normalized.reasoning_effort
        explicit_id = request.headers.get("x-webgpt-session-id")

        if stream:
            try:
                record, _, _ = self.conversations.resolve(
                    messages, model, tools, explicit_id, tool_choice
                )
            except KeyError:
                return _error("Unknown x-webgpt-session-id", 404, "session_not_found")
            except ConversationConflict as exc:
                return _error(str(exc), 409, "conversation_conflict")
            return StreamingResponse(
                self._stream_request(
                    record.session_id,
                    messages,
                    model,
                    ui_model,
                    tools,
                    tool_choice,
                    reasoning_effort,
                    normalized.protocol,
                    normalized.client,
                    normalized.stream_include_usage,
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
            response, record = await self.complete_normalized(normalized, explicit_id)
            self._log_request(request_id, record, model, False, len(tools), started, None)
            return JSONResponse(response, headers=_session_headers(record.session_id))
        except KeyError:
            self._log_request(
                request_id, None, model, False, len(tools), started, "session_not_found"
            )
            return _error("Unknown x-webgpt-session-id", 404, "session_not_found")
        except Exception as exc:
            self._log_request(
                request_id, None, model, False, len(tools), started, type(exc).__name__
            )
            return self._map_exception(exc)

    async def complete_normalized(
        self,
        normalized: ChatCompletionRequest,
        explicit_id: str | None = None,
        *,
        append_to_session: bool = False,
        stream_callback: Any = None,
    ) -> tuple[dict[str, Any], ConversationRecord]:
        """Execute a normalized request through the single browser runtime.

        Protocol adapters use this instead of calling HTTP handlers so model,
        tool, conversation and browser behavior cannot diverge by protocol.
        """
        resolution = self.model_registry.resolve(normalized.requested_model)
        messages = normalized.messages
        tools = normalized.tools
        if append_to_session:
            if explicit_id is None:
                raise KeyError("previous response has no gateway session")
            previous = self.conversations.get(explicit_id)
            if previous is None:
                raise KeyError(explicit_id)
            messages = previous.messages + messages
            if not tools:
                tools = previous.tools
        record, _, _ = self.conversations.resolve(
            messages,
            resolution.response_model,
            tools,
            explicit_id,
            normalized.tool_choice,
        )
        async with self._conversation_lock(record.session_id):
            # Re-resolve after waiting because another request for this logical
            # conversation may have committed while we were queued.
            record, tail, cached = self.conversations.resolve(
                messages,
                resolution.response_model,
                tools,
                explicit_id or record.session_id,
                normalized.tool_choice,
            )
            if cached:
                assert record.last_response is not None
                return record.last_response, record
            if self.conversations.pending_matches(
                record,
                messages,
                resolution.response_model,
                tools,
                normalized.tool_choice,
            ):
                reconciled = await self._reconcile_pending_request(
                    record,
                    messages,
                    resolution.response_model,
                    resolution.ui_label,
                    tools,
                    normalized.tool_choice,
                    normalized.reasoning_effort,
                )
                if reconciled is not None:
                    return reconciled, record
                # Authoritative history proved that the pending turn is absent.
                # Only now is a bounded resend safe.
                self.conversations.clear_pending(record)
            response, _ = await self._execute_turn(
                record,
                tail,
                messages,
                resolution.response_model,
                resolution.ui_label,
                tools,
                normalized.tool_choice,
                normalized.reasoning_effort,
                normalized.protocol,
                normalized.client,
                stream_callback,
            )
            return response, record

    async def responses(self, request: Request) -> Response:
        try:
            body = await request.json()
            adapted = parse_responses_request(body)
            adapted = replace(
                adapted, request=replace(adapted.request, client=_client_name(request))
            )
            previous_session = (
                self._response_sessions.get(adapted.previous_response_id)
                if adapted.previous_response_id
                else None
            )
            if adapted.previous_response_id and previous_session is None:
                return _error("Unknown previous_response_id", 404, "response_not_found")
            response, record = await self.complete_normalized(
                adapted.request, previous_session, append_to_session=previous_session is not None
            )
            response_id = f"resp_{uuid.uuid4().hex[:16]}"
            self._response_sessions[response_id] = record.session_id
            payload = response_to_responses(response, response_id=response_id)
            if adapted.request.stream:
                return StreamingResponse(
                    self._responses_stream(payload),
                    media_type="text/event-stream",
                    headers=_session_headers(record.session_id),
                )
            return JSONResponse(payload, headers=_session_headers(record.session_id))
        except RequestValidationError as exc:
            return _error(str(exc), 400, "invalid_request_error")
        except KeyError:
            return _error("Unknown previous_response_id", 404, "response_not_found")
        except Exception as exc:
            return self._map_exception(exc)

    async def anthropic_messages(self, request: Request) -> Response:
        started = time.monotonic()
        request_id = f"anth_{uuid.uuid4().hex[:12]}"
        try:
            body = await request.json()
            adapted = parse_anthropic_request(body)
            adapted = replace(
                adapted,
                request=replace(
                    adapted.request,
                    client=_client_name(request),
                    request_headers=_anthropic_request_headers(request),
                ),
            )
            if adapted.request.stream:
                return StreamingResponse(
                    self._anthropic_live_stream(request, adapted),
                    media_type="text/event-stream",
                    headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
                )
            payload, record = await self._complete_anthropic(adapted)
            duration_ms = int((time.monotonic() - started) * 1_000)
            logger.info(
                "anthropic_gateway_request %s",
                json.dumps(
                    {
                        "request_id": request_id,
                        "gateway_session_id": record.session_id,
                        "duration_ms": duration_ms,
                        "stream": adapted.request.stream,
                        "tool_count": len(adapted.request.tools),
                        "stop_reason": payload.get("stop_reason"),
                        "error_code": None,
                    },
                    separators=(",", ":"),
                ),
            )
            if adapted.request.stream:
                return StreamingResponse(
                    self._anthropic_stream(payload),
                    media_type="text/event-stream",
                    headers={
                        **_session_headers(record.session_id),
                        "x-webgpt-duration-ms": str(duration_ms),
                    },
                )
            return JSONResponse(
                payload,
                headers={
                    **_session_headers(record.session_id),
                    "x-webgpt-duration-ms": str(duration_ms),
                },
            )
        except RequestValidationError as exc:
            logger.info(
                "anthropic_gateway_request %s",
                json.dumps(
                    {
                        "request_id": request_id,
                        "duration_ms": int((time.monotonic() - started) * 1_000),
                        "error_code": "invalid_request_error",
                    },
                    separators=(",", ":"),
                ),
            )
            return _anthropic_error(_error(str(exc), 400, "invalid_request_error"))
        except Exception as exc:
            mapped = self._map_exception(exc)
            logger.info(
                "anthropic_gateway_request %s",
                json.dumps(
                    {
                        "request_id": request_id,
                        "duration_ms": int((time.monotonic() - started) * 1_000),
                        "error_code": json.loads(bytes(mapped.body))["error"]["code"],
                    },
                    separators=(",", ":"),
                ),
            )
            return _anthropic_exception_error(self, exc)

    async def anthropic_count_tokens(self, request: Request) -> Response:
        try:
            return JSONResponse({"input_tokens": estimate_anthropic_input_tokens(await request.json())})
        except RequestValidationError as exc:
            return _anthropic_error(_error(str(exc), 400, "invalid_request_error"))
        except Exception as exc:
            return _anthropic_exception_error(self, exc)

    async def _complete_anthropic(
        self, adapted: Any, stream_callback: Any = None
    ) -> tuple[dict[str, Any], ConversationRecord]:
        if (
            self.force_anthropic_initial_tool
            and len(self.conversations) == 0
            and adapted.request.tools
            and adapted.request.tool_choice == "auto"
        ):
            tools_by_name = {
                str(tool.get("function", {}).get("name")): tool
                for tool in adapted.request.tools
                if isinstance(tool.get("function"), dict)
                and isinstance(tool["function"].get("name"), str)
            }
            if self._mock_request_calls_for_tool(adapted.request.messages, tools_by_name):
                forced = self._force_initial_anthropic_tool(adapted.request)
                if forced is not None:
                    return forced
        self._debug_protocol_messages("anthropic_messages", adapted.request.messages)
        pending_match = self._record_for_pending_tool_results(adapted.request.messages)
        if pending_match is not None:
            pending_record, new_results = pending_match
            # Claude Code may resend its full transcript. Only append tool
            # results that have not yet been committed by this gateway.
            adapted_request = replace(adapted.request, messages=new_results)
            response, record = await self.complete_normalized(
                adapted_request,
                pending_record.session_id,
                append_to_session=True,
                stream_callback=stream_callback,
            )
        else:
            response, record = await self.complete_normalized(
                adapted.request, stream_callback=stream_callback
            )
        return response_to_anthropic(response), record


    @staticmethod
    def _mock_request_calls_for_tool(
        messages: list[dict[str, Any]], tools_by_name: dict[str, dict[str, Any]]
    ) -> bool:
        """Keep forced initial calls limited to explicit tool requests."""
        user_text = next(
            (
                str(message.get("content", "")).strip()
                for message in reversed(messages)
                if message.get("role") == "user" and str(message.get("content", "")).strip()
            ),
            "",
        ).casefold()
        if not user_text:
            return False
        if any(
            phrase in user_text
            for phrase in (
                "use a tool",
                "use the tool",
                "call a tool",
                "call the tool",
                "run a command",
                "execute a command",
                "read the file",
                "inspect the workspace",
            )
        ):
            return True
        return any(user_text.startswith(f"{name.casefold()} ") for name in tools_by_name)


    def _force_initial_anthropic_tool(
        self, request: ChatCompletionRequest
    ) -> tuple[dict[str, Any], ConversationRecord] | None:
        read_name = next(
            (
                tool["function"]["name"]
                for tool in request.tools
                if tool.get("function", {}).get("name") == "Read"
            ),
            None,
        )
        if read_name is None:
            return None
        record, _tail, cached = self.conversations.resolve(
            request.messages,
            request.requested_model,
            request.tools,
            None,
            request.tool_choice,
        )
        if cached and record.last_response is not None:
            return response_to_anthropic(record.last_response), record
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        tool_call = {
            "id": call_id,
            "type": "function",
            "function": {
                "name": read_name,
                "arguments": json.dumps({"file_path": "SPEC.md"}),
            },
        }
        response = format_openai_chat_response(
            None,
            [tool_call],
            model=request.requested_model,
        )
        self.conversations.commit(
            record,
            request.messages,
            response["choices"][0]["message"],
            response,
            request.requested_model,
            request.tools,
            None,
            request.tool_choice,
        )
        # A forced initial tool call is intentionally served without touching the
        # browser. Mark this logical session as active so the following
        # tool_result turn can continue on the already-warm blank web session
        # instead of being treated as an unrestorable conversation.
        self._active_gateway_session_id = record.session_id
        self.completion_runtime._active_gateway_session_id = record.session_id
        return response_to_anthropic(response), record

    def _record_for_pending_tool_results(
        self, messages: list[dict[str, Any]]
    ) -> tuple[ConversationRecord, list[dict[str, Any]]] | None:
        incoming_results = [message for message in messages if message.get("role") == "tool"]
        if not incoming_results:
            return None
        matches: list[tuple[ConversationRecord, list[dict[str, Any]]]] = []
        for record in self.conversations._records.values():
            known_result_ids = {
                message.get("tool_call_id")
                for message in record.messages
                if message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str)
            }
            new_results = [
                message
                for message in incoming_results
                if message.get("tool_call_id") not in known_result_ids
            ]
            new_result_ids = {
                message.get("tool_call_id")
                for message in new_results
                if isinstance(message.get("tool_call_id"), str)
            }
            if not new_result_ids:
                continue
            pending: set[str] = set()
            for message in reversed(record.messages):
                if message.get("role") != "assistant":
                    continue
                pending = {
                    call["id"]
                    for call in message.get("tool_calls") or []
                    if isinstance(call, dict) and isinstance(call.get("id"), str)
                }
                break
            if new_result_ids.issubset(pending):
                matches.append((record, new_results))
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    async def _responses_stream(payload: dict[str, Any]):
        created = {**payload, "output": []}
        yield _sse_event("response.created", {"type": "response.created", "response": created})
        for index, item in enumerate(payload["output"]):
            if item["type"] == "message":
                text = item["content"][0]["text"]
                yield _sse_event(
                    "response.output_text.delta",
                    {"type": "response.output_text.delta", "output_index": index, "delta": text},
                )
            yield _sse_event(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": index, "item": item},
            )
        yield _sse_event("response.completed", {"type": "response.completed", "response": payload})

    async def _anthropic_live_stream(self, request: Request, adapted: Any):
        """Send the Anthropic stream preamble immediately, then keep it alive.

        The browser transport may take minutes before its first model delta. A
        real SSE response must therefore be established before awaiting that
        work, and a cancelled client must not retain a worker lease.
        """
        message_id = f"msg_{uuid.uuid4().hex[:16]}"
        base = {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": adapted.request.requested_model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        deltas: asyncio.Queue[str] = asyncio.Queue()

        async def on_delta(text: str) -> None:
            await deltas.put(text)

        callback = on_delta if not adapted.request.tools else None
        task = asyncio.create_task(
            self._complete_anthropic(adapted, stream_callback=callback)
        )
        delta_task: asyncio.Task[str] | None = None
        emitted: list[str] = []
        started_content = False
        try:
            yield _sse_event("message_start", {"type": "message_start", "message": base})
            while not task.done() or not deltas.empty():
                if delta_task is None and callback is not None:
                    delta_task = asyncio.create_task(deltas.get())
                waiting = {task}
                if delta_task is not None:
                    waiting.add(delta_task)
                done, _pending = await asyncio.wait(
                    waiting, timeout=15, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    if await request.is_disconnected():
                        return
                    yield ": ping\n\n"
                    continue
                if delta_task is not None and delta_task in done:
                    delta = delta_task.result()
                    delta_task = None
                    if not started_content:
                        started_content = True
                        yield _sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": 0,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )
                    emitted.append(delta)
                    yield _sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": delta},
                        },
                    )
            payload, _record = await task
            payload["id"] = message_id
            if started_content and payload["content"] and payload["content"][0]["type"] == "text":
                final_text = payload["content"][0]["text"]
                streamed_text = "".join(emitted)
                remainder = final_text[len(streamed_text) :] if final_text.startswith(streamed_text) else final_text
                if remainder:
                    yield _sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": remainder},
                        },
                    )
                yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
                yield _sse_event(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": payload["stop_reason"], "stop_sequence": None},
                        "usage": {"output_tokens": 0},
                    },
                )
                yield _sse_event("message_stop", {"type": "message_stop"})
            else:
                async for event in self._anthropic_content_events(payload):
                    yield event
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            response = _anthropic_exception_error(self, exc)
            yield _sse_event("error", json.loads(bytes(response.body)))
        finally:
            if delta_task is not None and not delta_task.done():
                delta_task.cancel()
                with suppress(asyncio.CancelledError):
                    await delta_task
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    @staticmethod
    async def _anthropic_stream(payload: dict[str, Any]):
        base = {**payload, "content": []}
        yield _sse_event("message_start", {"type": "message_start", "message": base})
        async for event in WebChatAPIServer._anthropic_content_events(payload):
            yield event

    @staticmethod
    async def _anthropic_content_events(payload: dict[str, Any]):
        for index, block in enumerate(payload["content"]):
            if block["type"] == "text":
                yield _sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {**block, "text": ""},
                    },
                )
                text = block["text"]
                for offset in range(0, len(text), 32):
                    yield _sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "text_delta", "text": text[offset : offset + 32]},
                        },
                    )
            elif block["type"] == "tool_use":
                yield _sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": block["id"],
                            "name": block["name"],
                            "input": {},
                        },
                    },
                )
                yield _sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(block.get("input", {}), ensure_ascii=False),
                        },
                    },
                )
            else:
                yield _sse_event(
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": block},
                )
            yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": index})
        yield _sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": payload["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": 0},
            },
        )
        yield _sse_event("message_stop", {"type": "message_stop"})

    async def _position_session(
        self,
        session: ChatGPTWebSession,
        record: ConversationRecord,
        ui_model: str | None,
        reasoning_effort: str | None = None,
    ) -> None:
        await self.completion_runtime.position_session(
            session, record, ui_model, reasoning_effort
        )

    async def _reconcile_pending_request(
        self,
        record: ConversationRecord,
        messages: list[dict[str, Any]],
        model: str,
        ui_model: str | None,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        reasoning_effort: str | None,
    ) -> dict[str, Any] | None:
        execution = await self.completion_runtime.reconcile_pending(
            record,
            messages,
            model,
            ui_model,
            tools,
            tool_choice,
            reasoning_effort,
        )
        if execution is None:
            return None
        response = format_openai_chat_response(
            execution.turn.content,
            execution.turn.tool_calls or None,
            model=model,
        )
        self.conversations.commit(
            record,
            messages,
            response["choices"][0]["message"],
            response,
            model,
            tools,
            execution.web_result.conversation_id,
            tool_choice,
        )
        return response

    async def _execute_turn(
        self,
        record: ConversationRecord,
        tail: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str,
        ui_model: str | None,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        reasoning_effort: str | None = None,
        protocol: str = "unknown",
        client: str = "unknown",
        stream_callback: Any = None,
    ) -> tuple[dict[str, Any], TurnResult]:
        self._validate_tool_result_correlation(record, tail, messages)
        try:
            execution = await self.completion_runtime.execute(
                record,
                tail,
                messages,
                model,
                ui_model,
                tools,
                tool_choice,
                reasoning_effort,
                protocol,
                client,
                stream_callback,
            )
        except MalformedToolCall:
            pending = record.pending_prompt or ""
            if pending:
                self._debug_raw_output(pending)
            raise
        response = format_openai_chat_response(
            execution.turn.content,
            execution.turn.tool_calls or None,
            model=model,
        )
        self.conversations.commit(
            record,
            messages,
            response["choices"][0]["message"],
            response,
            model,
            tools,
            execution.web_result.conversation_id,
            tool_choice,
        )
        return response, execution.web_result

    async def _stream_request(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        model: str,
        ui_model: str | None,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        reasoning_effort: str | None = None,
        protocol: str = "unknown",
        client: str = "unknown",
        include_usage: bool = False,
    ):
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        async with self._conversation_lock(session_id):
            try:
                record, tail, cached = self.conversations.resolve(
                    messages, model, tools, session_id, tool_choice
                )
                if cached:
                    assert record.last_response is not None
                    async for chunk in self._stream_cached(
                        record.last_response, model, completion_id, include_usage=include_usage
                    ):
                        yield chunk
                    return
                if self.conversations.pending_matches(
                    record, messages, model, tools, tool_choice
                ):
                    reconciled = await self._reconcile_pending_request(
                        record,
                        messages,
                        model,
                        ui_model,
                        tools,
                        tool_choice,
                        reasoning_effort,
                    )
                    if reconciled is not None:
                        async for chunk in self._stream_cached(
                            reconciled, model, completion_id, include_usage=include_usage
                        ):
                            yield chunk
                        return
                    self.conversations.clear_pending(record)
                async with self._lease_session() as session:
                    async for chunk in self._stream_turn_on_session(
                        session,
                        record,
                        tail,
                        messages,
                        model,
                        ui_model,
                        tools,
                        tool_choice,
                        reasoning_effort,
                        protocol,
                        client,
                        include_usage,
                        completion_id,
                    ):
                        yield chunk
            except Exception as exc:
                error = bytes(self._map_exception(exc).body).decode()
                yield f"data: {error}\n\n"
                yield "data: [DONE]\n\n"

    async def _stream_turn_on_session(
        self,
        session: ChatGPTWebSession,
        record: ConversationRecord,
        tail: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str,
        ui_model: str | None,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        reasoning_effort: str | None,
        protocol: str,
        client: str,
        include_usage: bool,
        completion_id: str,
    ):
        self._validate_tool_result_correlation(record, tail, messages)
        result, _prompt = await self.completion_runtime.execute_raw_on_session(
            session,
            record,
            tail,
            messages,
            model,
            ui_model,
            tools,
            tool_choice,
            reasoning_effort,
            protocol,
            client,
        )
        yield format_openai_chunk(
            delta_role="assistant",
            model=model,
            completion_id=completion_id,
        )
        sieve = ToolStreamSieve(tools=tools, tool_choice=tool_choice)
        emitted_text: list[str] = []
        try:
            for offset in range(0, len(result.text), 24):
                part = sieve.feed(result.text[offset : offset + 24])
                for delta in part.text_deltas:
                    emitted_text.append(delta)
                    yield format_openai_chunk(
                        delta_content=delta,
                        model=model,
                        completion_id=completion_id,
                    )
            final = sieve.finalize()
            for delta in final.text_deltas:
                emitted_text.append(delta)
                yield format_openai_chunk(
                    delta_content=delta,
                    model=model,
                    completion_id=completion_id,
                )
            calls = final.tool_calls
            clean = None if calls else "".join(emitted_text)
        except MalformedToolCall:
            self._debug_raw_output(result.text)
            raise
        if calls:
            yield format_openai_chunk(
                delta_tool_calls=calls,
                model=model,
                completion_id=completion_id,
            )
            yield format_openai_chunk(
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
        if include_usage:
            yield format_openai_usage_chunk(model=model, completion_id=completion_id)
        yield "data: [DONE]\n\n"

    @staticmethod
    async def _stream_cached(
        response: dict[str, Any],
        model: str,
        completion_id: str,
        *,
        include_usage: bool = False,
    ):
        message = response["choices"][0]["message"]
        yield format_openai_chunk(
            delta_role="assistant",
            model=model,
            completion_id=completion_id,
        )
        if message.get("content"):
            yield format_openai_chunk(
                delta_content=message["content"], model=model, completion_id=completion_id
            )
        calls = message.get("tool_calls")
        if calls:
            yield format_openai_chunk(
                delta_tool_calls=calls,
                model=model,
                completion_id=completion_id,
            )
            yield format_openai_chunk(
                finish_reason="tool_calls",
                model=model,
                completion_id=completion_id,
            )
        else:
            yield format_openai_chunk(
                finish_reason="stop",
                model=model,
                completion_id=completion_id,
            )
        if include_usage:
            yield format_openai_usage_chunk(model=model, completion_id=completion_id)
        yield "data: [DONE]\n\n"

    @staticmethod
    def _parse_model_output(
        text: str, tools: list[dict[str, Any]], tool_choice: Any
    ) -> tuple[str | None, list[dict[str, Any]]]:
        turn = AssistantTurnBuilder.from_model_text(
            text,
            tools=tools,
            tool_choice=tool_choice,
        )
        return turn.content, turn.tool_calls

    @staticmethod
    def _debug_raw_output(text: str) -> None:
        if os.environ.get("WEBGPT_DEBUG_RAW_OUTPUT") == "1":
            logger.warning("malformed_model_output=%r", text[:4_000])

    @staticmethod
    def _debug_protocol_messages(protocol: str, messages: list[dict[str, Any]]) -> None:
        if os.environ.get("WEBGPT_DEBUG_PROTOCOL") != "1":
            return
        summary = []
        for message in messages:
            calls = message.get("tool_calls") or []
            summary.append(
                {
                    "role": message.get("role"),
                    "tool_call_id": message.get("tool_call_id"),
                    "tool_calls": [
                        call.get("id") for call in calls if isinstance(call, dict)
                    ],
                }
            )
        logger.warning("protocol_message_shape=%s", json.dumps({"protocol": protocol, "messages": summary}))

    @staticmethod
    def _validate_tool_result_correlation(
        record: ConversationRecord,
        tail: list[dict[str, Any]],
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        tool_results = [message for message in tail if message.get("role") == "tool"]
        if not tool_results:
            return
        pending = {
            call_id
            for transcript in (record.messages, messages or [], tail)
            for message in transcript
            if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
            if isinstance(call, dict)
            if isinstance((call_id := call.get("id")), str)
        }
        result_ids = [message.get("tool_call_id") for message in tool_results]
        if not pending or any(call_id not in pending for call_id in result_ids):
            raise ConversationConflict(
                "tool_call_id does not match the pending assistant tool call."
            )
        if any(call_id in record.delivered_tool_result_ids for call_id in result_ids):
            raise ConversationConflict(
                "tool_call_id result was already delivered to this conversation."
            )
        if len(result_ids) != len(set(result_ids)):
            raise ConversationConflict("Duplicate tool result in one request.")

    @staticmethod
    def _map_exception(exc: Exception) -> JSONResponse:
        mapping: list[tuple[type[Exception], int, str]] = [
            (AnonymousSessionUnavailable, 503, "anonymous_session_unavailable"),
            (AuthRequired, 503, "anonymous_session_unavailable"),
            (ModelUnavailable, 400, "model_unavailable"),
            (ConversationNotFound, 404, "conversation_not_found"),
            (ConversationConflict, 409, "conversation_conflict"),
            (GenerationTimeout, 504, "generation_timeout"),
            (GenerationInterrupted, 409, "generation_interrupted"),
            (EmptyModelResponse, 502, "empty_model_response"),
            (CommitUnknown, 409, "commit_unknown"),
            (MalformedToolCall, 502, "malformed_model_tool_call"),
            (RateLimited, 429, "rate_limit"),
            (WorkerQueueTimeout, 503, "worker_queue_timeout"),
            (BrowserDisconnected, 503, "browser_disconnected"),
            (UIChanged, 503, "web_ui_changed"),
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
    server: WebChatAPIServer = app.state.server
    if server._worker_factory is not None:
        await server._worker_factory.start()
    elif server.prewarm:
        try:
            await server.get_or_create_session()
        except Exception as exc:
            logger.warning("webgpt prewarm failed; continuing lazy startup", exc_info=exc)
            server.trace.emit(
                "webchat",
                "prewarm_failed",
                metadata={"error_type": type(exc).__name__},
            )
    try:
        yield
    finally:
        close_timeout = float(os.environ.get("WEBGPT_SERVER_CLOSE_TIMEOUT", "10"))
        try:
            await asyncio.wait_for(server.close(), timeout=close_timeout)
        except TimeoutError:
            logger.warning(
                "webgpt server close timed out after %.1fs; continuing process shutdown",
                close_timeout,
            )


def create_api_app(
    headless: bool = True,
    persistent: bool = False,
    profile_dir: str | None = None,
    executable_path: str | None = None,
    cdp_url: str | None = None,
    transport: str = "browser",
    model_aliases: Mapping[str, str] | None = None,
    conversation_store_path: str | None = None,
    conversation_ttl_seconds: float = 86_400,
    force_anthropic_initial_tool: bool = False,
    max_workers: int = 1,
    warm_workers: int = 1,
    queue_timeout: float = 30.0,
    trace_path: str | None = None,
    prompt_debug_dir: str | None = None,
    prewarm: bool = False,
    generation_timeout_seconds: float = 120.0,
    require_anonymous: bool = False,
) -> Starlette:
    server = WebChatAPIServer(
        headless=headless,
        persistent=persistent,
        profile_dir=profile_dir,
        executable_path=executable_path,
        cdp_url=cdp_url,
        transport=transport,
        model_aliases=model_aliases,
        conversation_store_path=conversation_store_path,
        conversation_ttl_seconds=conversation_ttl_seconds,
        force_anthropic_initial_tool=force_anthropic_initial_tool,
        max_workers=max_workers,
        warm_workers=warm_workers,
        queue_timeout=queue_timeout,
        trace_path=trace_path,
        prompt_debug_dir=prompt_debug_dir,
        prewarm=prewarm,
        generation_timeout_seconds=generation_timeout_seconds,
        require_anonymous=require_anonymous,
    )
    app = Starlette(
        routes=[
            Route("/health", server.health, methods=["GET"]),
            Route("/healthz", server.liveness, methods=["GET"]),
            Route("/readyz", server.readiness, methods=["GET"]),
            Route("/api/hello", server.liveness, methods=["HEAD", "GET"]),
            Route("/v1/models", server.list_models, methods=["GET"]),
            Route("/models", server.list_models, methods=["GET"]),
            Route("/v1/chat/completions", server.chat_completions, methods=["POST"]),
            Route("/chat/completions", server.chat_completions, methods=["POST"]),
            Route("/v1/responses", server.responses, methods=["POST"]),
            Route("/responses", server.responses, methods=["POST"]),
            Route("/v1/messages", server.anthropic_messages, methods=["POST"]),
            Route("/messages", server.anthropic_messages, methods=["POST"]),
            Route("/v1/messages/count_tokens", server.anthropic_count_tokens, methods=["POST"]),
            Route("/messages/count_tokens", server.anthropic_count_tokens, methods=["POST"]),
            Route("/v1/v1/messages", server.anthropic_messages, methods=["POST"]),
        ],
        middleware=[Middleware(_RequestTraceMiddleware, server=server)],
        lifespan=_lifespan,
    )
    app.state.server = server
    return app
