from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from gpt.api.protocol_adapters import (
    StreamUsageEstimator,
    anthropic_usage,
    estimate_text_chars_to_tokens,
    estimate_tokens_from_chars,
    rendered_request_prompt,
)
from gpt.requests import ChatCompletionRequest
from gpt.state import AuthRequired, RateLimited
from gpt.transport.breaker import RateLimitBreaker, global_rate_limit_breaker


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
            repeat_aborts = 0
            runtime_correction_count: int | None = None
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
                    # CORRECTION-TELEMETRY-PARITY: the terminal runtime events
                    # carry the authoritative spend -- an anti-repeat abort
                    # rolls its own pre-check increment back BEFORE raising, so
                    # raw tool_correction events overcount by exactly the
                    # number of aborted attempts.  Prefer runtime metadata;
                    # keep event counting only as the fallback below.
                    spent = event.metadata.get("correction_count")
                    if isinstance(spent, int):
                        runtime_correction_count = spent
                elif event.component == "assistantturn" and event.kind == "parsed":
                    parse_ms += int(event.metadata.get("parse_ms") or 0)
                elif event.component == "completionruntime" and event.kind == "tool_correction":
                    correction_count += 1
                elif (
                    event.component == "completionruntime"
                    and event.kind == "persistent_correction_repeat"
                ):
                    repeat_aborts += 1
                elif (
                    event.component == "completionruntime"
                    and event.kind == "submit_failed_before_commit_unknown"
                ):
                    # TURN-ID-FAILURE-TRACE: a failed turn never reaches
                    # submit_completed, so its terminal turn id arrives on the
                    # failure event instead (fallback chain: submit_completed
                    # -> failure events).
                    value = event.metadata.get("turn_id")
                    if isinstance(value, str) and turn_id is None:
                        turn_id = value
                    spent = event.metadata.get("correction_count")
                    if isinstance(spent, int):
                        runtime_correction_count = spent
                elif event.component == "promptcompat" and event.kind == "prompt_built":
                    tool_count = max(tool_count, int(event.metadata.get("tool_count") or 0))
            if turn_id is None and session_id is None:
                # Error responses carry no x-webgpt-session-id header, so the
                # session-filtered loop above skips every runtime event and
                # failure traces would still report turn_id=None.  Attribute
                # best-effort from in-window failure events instead.
                for event in events:
                    if (
                        event.component == "completionruntime"
                        and event.kind == "submit_failed_before_commit_unknown"
                    ):
                        value = event.metadata.get("turn_id")
                        if isinstance(value, str) and turn_id is None:
                            turn_id = value
                        spent = event.metadata.get("correction_count")
                        if isinstance(spent, int):
                            runtime_correction_count = spent
                        if turn_id is not None and runtime_correction_count is not None:
                            break
            if runtime_correction_count is not None:
                correction_count = runtime_correction_count
            else:
                # No terminal metadata (e.g. commit_unknown): subtract aborted
                # attempts from the raw count so telemetry stays net of them.
                correction_count = max(0, correction_count - repeat_aborts)
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
                headers = message.setdefault("headers", [])
                present = {name.lower() for name, _value in headers}
                # HEADER-PARITY: echo the internal trace uuid and publish
                # advisory rate-limit headers derived from the breaker state
                # (per-account aggregate when a pool is wired, else the global
                # singleton) so clients keep their api.anthropic.com
                # observability.
                if b"request-id" not in present:
                    headers.append((b"request-id", request_id.encode("latin-1")))
                for name, value in _advisory_ratelimit_headers(
                    getattr(self.server, "pool_rate_limit_breakers", None)
                ).items():
                    key = name.encode("latin-1")
                    if key not in present:
                        headers.append((key, value.encode("latin-1")))
                for raw_name, raw_value in headers:
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


# JSON-DELTA-CHUNK: tool_use arguments stream in bounded ``partial_json``
# pieces so clients observe incremental progress instead of one giant burst.
_JSON_DELTA_CHUNK_CHARS = 512


# LATE-FAIL-SURFACE: error types the Anthropic wire contract defines for
# ``event: error`` payloads. Anything else collapses to ``api_error`` so every
# SDK build can parse the frame instead of choking on a custom type.
_ANTHROPIC_STREAM_ERROR_TYPES = frozenset(
    {
        "api_error",
        "overloaded_error",
        "rate_limit_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
        "invalid_request_error",
        "request_too_large",
    }
)


# OVERLOADED-529: message fragments that mark a RateLimited as backend
# capacity exhaustion (HTTP 529 ``overloaded_error``) rather than a
# per-client quota limit. A raise site that knows better may also flag the
# exception instance directly with ``overloaded = True``.
_RATELIMIT_OVERLOAD_MARKERS = (
    "overloaded",
    "over capacity",
    "high demand",
    "server is busy",
)


def _is_overloaded_rate_limit(exc: Exception) -> bool:
    """True only for RateLimited carrying an explicit backend-overload signal."""
    if not isinstance(exc, RateLimited):
        return False
    if getattr(exc, "overloaded", False) is True:
        return True
    message = str(exc).casefold()
    return any(marker in message for marker in _RATELIMIT_OVERLOAD_MARKERS)


# HEADER-PARITY: advisory request-limit ceiling advertised to clients. The
# gateway has no hard per-key quota; the number only anchors the ratio math
# SDKs perform against ``requests-remaining``.
_ADVISORY_RATELIMIT_REQUESTS_LIMIT = 100


def _advisory_ratelimit_headers(
    breakers: Mapping[str, RateLimitBreaker] | None = None,
) -> dict[str, str]:
    """Advisory ``anthropic-ratelimit-*`` derived from breaker state.

    HEADER-PARITY: the real upstream quota is unknowable from behind the web
    transport, so these are static except where the RateLimitBreaker knows
    better -- while a cooldown window is open (or a half-open probe is out)
    remaining requests read as exhausted so clients back off in sync with the
    backend instead of hammering it.

    POOL-PER-ACCT-BREAKER: with per-account breakers wired (scope=auto pool),
    the aggregate advertises the weakest closed account -- ``min`` of the
    remaining values across breakers that are still closed -- and reads 0 only
    once EVERY account's window is open. The reset hint carries the longest
    open window so clients wait out exactly the account blocking the pool.
    Without per-account breakers the global singleton decides, unchanged.
    """
    limit = _ADVISORY_RATELIMIT_REQUESTS_LIMIT
    if breakers:
        try:
            snapshots = [breaker.snapshot() for breaker in breakers.values()]
        except Exception:  # pragma: no cover - header advice must never break I/O
            snapshots = []
        if snapshots:
            closed_remaining = [
                limit for snapshot in snapshots if snapshot.state == "closed"
            ]
            remaining = min(closed_remaining) if closed_remaining else 0
            longest_open = max(
                (snapshot.remaining_seconds for snapshot in snapshots), default=0.0
            )
            remaining_seconds = (
                int(longest_open + 0.999) if not closed_remaining else 0
            )
            return {
                "anthropic-ratelimit-requests-limit": str(limit),
                "anthropic-ratelimit-requests-remaining": str(remaining),
                "anthropic-ratelimit-requests-reset": f"{remaining_seconds}s",
            }
    try:
        snapshot = global_rate_limit_breaker().snapshot()
    except Exception:  # pragma: no cover - header advice must never break I/O
        snapshot = None
    state = snapshot.state if snapshot is not None else "closed"
    remaining_seconds = int(snapshot.remaining_seconds + 0.999) if snapshot else 0
    return {
        "anthropic-ratelimit-requests-limit": str(limit),
        "anthropic-ratelimit-requests-remaining": str(limit) if state == "closed" else "0",
        "anthropic-ratelimit-requests-reset": f"{remaining_seconds}s",
    }


def _anthropic_error(response: JSONResponse) -> JSONResponse:
    """Translate the shared local error taxonomy into Anthropic's envelope."""
    payload = json.loads(bytes(response.body))
    error = payload["error"]
    status_by_error = {
        400: (400, "invalid_request_error"),
        401: (401, "authentication_error"),
        403: (401, "authentication_error"),
        404: (404, "not_found_error"),
        409: (409, "invalid_request_error"),
        429: (429, "rate_limit_error"),
        500: (500, "api_error"),
        502: (502, "api_error"),
        # Retryable infrastructure failures must keep their retryable status
        # code for SDK clients (Claude Code) instead of collapsing into 500.
        503: (503, "api_error"),
        504: (504, "timeout_error"),
        # OVERLOADED-529: Anthropic's dedicated capacity-exhausted status.
        529: (529, "overloaded_error"),
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


def _anthropic_refusal_response(
    exc: Exception,
    *,
    model: str | None = None,
    prompt_text: str | None = None,
) -> JSONResponse:
    """STOP-REASON-REFUSAL (parity-delta-audit 2026-08-26 row M / G5).

    A definitive model refusal is a *completed* turn on the real Anthropic
    wire: HTTP 200 with ``stop_reason:"refusal"`` plus an honest explanation
    text block.  Returning 502 made Claude Code read a deliberate decline as
    an infrastructure fault (step-fail + blind retry that only burns more
    quota).  This helper is invoked ONLY for ``ModelRefusalError`` --
    infrastructure failures keep their retryable 429/503/504/529 statuses.
    """
    text = f"[webgpt-gateway:model_refusal] {exc}".strip()
    input_tokens = (
        estimate_tokens_from_chars(len(prompt_text)) if prompt_text is not None else 0
    )
    payload = {
        "id": f"msg_{uuid.uuid4().hex[:16]}",
        "type": "message",
        "role": "assistant",
        "model": model or "chatgpt-web",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "refusal",
        "stop_sequence": None,
        "usage": anthropic_usage(
            input_tokens, estimate_text_chars_to_tokens(len(text))
        ),
    }
    return JSONResponse(payload, status_code=200)


def _messages_prompt_text(messages: list[dict[str, Any]]) -> str:
    """Concatenated message text feeding the chars/4 usage estimate."""
    return "\n".join(str(message.get("content") or "") for message in messages)


def _request_prompt_text(request: ChatCompletionRequest) -> str:
    """Rendered turn prompt feeding the chars/4 usage estimate.

    USAGE-CONTRACT-ALIGN: returns the SAME ``render_messages(initial=True)``
    text that ``/v1/messages/count_tokens`` counts (see
    ``rendered_request_prompt``), so count_tokens and ``usage.input_tokens``
    agree for identical payloads instead of one being scaffold-inclusive and
    the other raw message content only.
    """
    return rendered_request_prompt(request)


def _anthropic_payload_usage(
    payload: dict[str, Any], prompt_text: str | None = None
) -> dict[str, Any]:
    """Estimated usage derived from a finalized Anthropic envelope.

    Used on stream paths that never observe incremental deltas (replayed or
    cached payloads): output tokens are estimated from the finished content
    blocks (text plus serialized tool arguments), input from ``prompt_text``
    when the caller knows it.
    """
    estimator = StreamUsageEstimator(prompt_text or "")
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            estimator.add_delta(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            estimator.add_delta(json.dumps(block.get("input", {}), ensure_ascii=False))
    return estimator.snapshot()



