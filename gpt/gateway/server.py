from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from typing import Any, cast

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
    StreamUsageEstimator,
    estimate_anthropic_input_tokens,
    parse_anthropic_request,
    parse_responses_request,
    response_to_anthropic,
    response_to_responses,
)
from gpt.assistantturn import AssistantTurnBuilder
from gpt.auth.accounts import AccountStore, resolve_default_account
from gpt.completionruntime import CompletionRuntime
from gpt.conversations import ConversationRecord, ConversationStore
from gpt.gateway.config import GatewayTuning
from gpt.gateway.http_contract import (
    _ANTHROPIC_STREAM_ERROR_TYPES,
    _JSON_DELTA_CHUNK_CHARS,
    _anthropic_error,
    _anthropic_exception_error,
    _anthropic_payload_usage,
    _anthropic_refusal_response,
    _anthropic_request_headers,
    _client_name,
    _error,
    _is_overloaded_rate_limit,
    _messages_prompt_text,
    _request_prompt_text,
    _RequestTraceMiddleware,
    _session_headers,
    _sse_event,
)
from gpt.gateway.mock_backend import (
    format_conversational_reply as _mock_format_conversational_reply,
)
from gpt.gateway.mock_backend import (
    mock_argument_value as _mock_argument_value_impl,
)
from gpt.gateway.mock_backend import (
    mock_request_calls_for_tool as _mock_request_calls_for_tool_impl,
)
from gpt.gateway.mock_backend import (
    mock_tool_call as _mock_tool_call_impl,
)
from gpt.gateway.ops import (
    health as _ops_health,
)
from gpt.gateway.ops import (
    health_check_interval as _ops_health_check_interval,
)
from gpt.gateway.ops import (
    list_models as _ops_list_models,
)
from gpt.gateway.ops import (
    liveness as _ops_liveness,
)
from gpt.gateway.ops import (
    readiness as _ops_readiness,
)
from gpt.gateway.ops import (
    start_account_health_loop as _ops_start_account_health_loop,
)
from gpt.gateway.ops import (
    start_usage_poller as _ops_start_usage_poller,
)
from gpt.gateway.ops import (
    usage_poll_seconds as _ops_usage_poll_seconds,
)
from gpt.gateway.ops import (
    worker_browsers_connected as _ops_worker_browsers_connected,
)
from gpt.gateway.runtime import ModelRefusalError
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
from gpt.transport.account_health import AccountHealthTracker
from gpt.transport.breaker import RateLimitBreaker
from gpt.transport.browser import BrowserManager
from gpt.transport.factory import ChatGPTWorkerFactory, WorkerQueueTimeout
from gpt.transport.failover import FailoverRetryRequired, maybe_failover
from gpt.transport.hybrid import HybridWorkerFactory
from gpt.transport.multi_account import MultiAccountWorkerFactory
from gpt.transport.session import ChatGPTWebSession, _is_browser_crash
from gpt.transport.token_manager import local_mock_mode_enabled
from gpt.transport.usage_poller import (
    PoolPressureBoard,
    UsagePoller,
)
from gpt.types import TurnResult

logger = logging.getLogger("gpt.webchat.api")

DEFAULT_RESPONSE_SESSION_CAP = 512
_HEALTH_CHECK_ENV = "WEBGPT_HEALTH_CHECK_ENABLED"
_DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 300.0
_TRUTHY = {"1", "true", "yes", "on"}
# POOL-PER-ACCT-BREAKER (row S): ``global`` keeps the process-wide singleton
# breaker (historical behaviour); ``auto`` gives every account of a multi-
# account pool its own RateLimitBreaker so one exhausted account cools down
# alone instead of parking the whole pool for 90-600s. A single-account pool
# gains nothing from its own breaker and stays on the shared singleton.
_BREAKER_SCOPE_ENV = "WEBGPT_BREAKER_SCOPE"
_BREAKER_SCOPE_VALUES = ("global", "auto")


def _breaker_scope_per_account(account_count: int) -> bool:
    """Resolve ``WEBGPT_BREAKER_SCOPE`` for a pool of ``account_count`` accounts.

    Returns True only when per-account breakers must be wired: scope ``auto``
    AND at least two accounts in the pool. Unknown values fall back to
    ``global`` with a warning so a typo can never silently disable the
    emergency brake.
    """
    raw = os.environ.get(_BREAKER_SCOPE_ENV, "").strip().lower()
    if raw and raw not in _BREAKER_SCOPE_VALUES:
        logger.warning(
            "breaker_scope_invalid_value value=%r expected one of: %s",
            os.environ.get(_BREAKER_SCOPE_ENV),
            "|".join(_BREAKER_SCOPE_VALUES),
        )
    scope = raw if raw in _BREAKER_SCOPE_VALUES else "global"
    return scope == "auto" and account_count >= 2


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _lease_supports_affinity(factory: Any) -> bool:
    """Whether factory.lease accepts an optional positional affinity key.

    Decide before entering the context manager so a TypeError raised by the
    turn body can never be mistaken for an unsupported-affinity factory.
    """
    lease = getattr(factory, "lease", None)
    if lease is None:
        return False
    try:
        signature = inspect.signature(lease)
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        for parameter in signature.parameters.values()
    )


class WebChatAPIServer:
    """OpenAI-compatible, conversation-aware facade over ChatGPT Web."""

    def __init__(
        self,
        headless: bool = True,
        persistent: bool = False,
        profile_dir: str | None = None,
        account_profiles: Mapping[str, str] | None = None,
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
        generation_timeout_seconds: float | None = None,
        require_anonymous: bool = False,
        mock_backend: bool = False,
    ):
        self.tuning = GatewayTuning.from_environ()
        self.headless = headless
        self.persistent = persistent
        self.profile_dir = profile_dir
        self.account_profiles = dict(account_profiles or {})
        self.executable_path = executable_path
        self.cdp_url = cdp_url
        # In explicitly local dev/test mode, keep every request inside the
        # gateway.  This preserves Claude Code's real SSE/tool adapters while
        # ensuring a browser's unauthenticated state cannot become a 401.
        self.mock_backend = mock_backend or local_mock_mode_enabled()
        if transport not in ("hybrid", "browser"):
            raise ValueError("gateway generation transport must be 'hybrid' or 'browser'")
        self.transport = transport
        self._session: ChatGPTWebSession | None = None
        self._session_lock = asyncio.Lock()
        # LATE-FAIL-SURFACE: count of mid-stream failures masked behind the
        # clean end_turn close because content had already reached the client
        # (R4 keeps that close non-retryable). Observability for how often a
        # truncated turn is presented as complete.
        self.late_failure_masked = 0
        # session_id -> [lock, waiter_count]; entries are dropped once the last
        # request for that conversation finishes so long-running servers do not
        # accumulate one lock per conversation ever seen.
        self._conversation_locks: dict[str, list] = {}
        self._worker_factory: HybridWorkerFactory | ChatGPTWorkerFactory | MultiAccountWorkerFactory | None = None
        self._account_health_tracker: AccountHealthTracker | None = None
        self._health_loop_task: asyncio.Task | None = None
        self._health_loop_stop: asyncio.Event | None = None
        # USAGE-POLLER-WIRE: constructed only when WEBGPT_USAGE_POLL_SECONDS
        # is enabled; stays None (zero overhead) on the default OFF path.
        self._usage_poller: UsagePoller | None = None
        # POOL-POLLER-PERACCT wire-up: one poller per pool account plus the
        # shared pressure board when scope=auto resolved to a real pool;
        # both stay empty/None (singleton path governs) otherwise.
        self._account_usage_pollers: dict[str, UsagePoller] = {}
        self.pool_pressure_board: PoolPressureBoard | None = None
        # POOL-PER-ACCT-BREAKER: per-account breakers when scope resolves to a
        # real pool; empty means the global singleton governs (default).
        self.pool_rate_limit_breakers: dict[str, RateLimitBreaker] = {}
        if not self.mock_backend:
            factory_class = HybridWorkerFactory if transport == "hybrid" else ChatGPTWorkerFactory
            if self.account_profiles:
                if cdp_url and len(self.account_profiles) > 1:
                    raise ValueError("A single --cdp-url cannot back multiple account profiles.")
                per_account_breakers: dict[str, RateLimitBreaker] | None = None
                if _breaker_scope_per_account(len(self.account_profiles)):
                    per_account_breakers = {
                        name: RateLimitBreaker.from_env()
                        for name in self.account_profiles
                    }
                account_factories: dict[str, Any] = {}
                for account_name, account_profile in self.account_profiles.items():
                    browser = BrowserManager(
                        headless=headless,
                        persistent=True,
                        profile_dir=account_profile,
                        executable_path=executable_path,
                        cdp_url=cdp_url if len(self.account_profiles) == 1 else None,
                    )
                    factory_kwargs: dict[str, Any] = {
                        "max_workers": max_workers,
                        "warm_workers": min(warm_workers, max_workers),
                        "queue_timeout": queue_timeout,
                    }
                    if factory_class is HybridWorkerFactory:
                        async def no_implicit_login() -> bool:
                            return False

                        factory_kwargs["auto_login"] = no_implicit_login
                        factory_kwargs["allow_local_mock"] = False
                    if per_account_breakers is not None:
                        factory_kwargs["rate_limit_breaker"] = per_account_breakers[
                            account_name
                        ]
                    account_factories[account_name] = factory_class(browser, **factory_kwargs)
                if _env_flag(_HEALTH_CHECK_ENV):
                    self._account_health_tracker = AccountHealthTracker()
                default_account_name: str | None = None
                try:
                    default_account_name = resolve_default_account(AccountStore())
                except Exception:
                    logger.warning(
                        "default_account_resolution_failed", exc_info=True
                    )
                self._worker_factory = MultiAccountWorkerFactory(
                    account_factories,
                    health=self._account_health_tracker,
                    default_name=default_account_name,
                    breakers=per_account_breakers,
                )
                self.pool_rate_limit_breakers = dict(per_account_breakers or {})
            else:
                shared_browser = BrowserManager(
                    headless=headless,
                    persistent=persistent,
                    profile_dir=profile_dir,
                    executable_path=executable_path,
                    cdp_url=cdp_url,
                )
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
        self._response_session_cap = self.tuning.response_session_cap
        # response_id -> gateway session_id, bounded LRU so a long-lived
        # process cannot grow this map without limit.
        self._response_sessions: OrderedDict[str, str] = OrderedDict()
        self.force_anthropic_initial_tool = force_anthropic_initial_tool
        self.model_registry = ModelRegistry(model_aliases)
        self.prewarm = prewarm
        self.require_anonymous = require_anonymous
        if self.require_anonymous and not self.mock_backend and max_workers != 1:
            raise ValueError("free_anonymous gateway mode requires max_workers=1")
        # Live SSE stream lifetime bounds (BUG-A): a stream must always end with
        # an explicit terminator instead of pinging forever when the backend
        # wedges.  Both are overridable for tests.
        self.queue_timeout = float(queue_timeout)
        self.generation_timeout_seconds = (
            self.tuning.generation_timeout_seconds
            if generation_timeout_seconds is None
            else float(generation_timeout_seconds)
        )
        self.stream_idle_seconds = self._env_float("WEBGPT_STREAM_IDLE_SECONDS", 15.0)
        self.stream_deadline_seconds = self._resolve_stream_deadline_seconds()
        self.trace = RuntimeTraceBus(output_path=trace_path)
        self.completion_runtime = CompletionRuntime(
            self.conversations,
            self._lease_session,
            self.trace,
            prompt_debug_dir=prompt_debug_dir,
            generation_timeout_seconds=self.generation_timeout_seconds,
            tuning=self.tuning,
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

    @asynccontextmanager
    async def _conversation_lock(self, session_id: str):
        """Serialize turns per conversation without leaking lock entries.

        The waiter count is bumped synchronously with the dict lookup, so when
        it drops back to zero no other request can still be holding or waiting
        on this entry and it is safe to remove.
        """
        entry = self._conversation_locks.get(session_id)
        if entry is None:
            entry = [asyncio.Lock(), 0]
            self._conversation_locks[session_id] = entry
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] <= 0:
                self._conversation_locks.pop(session_id, None)

    @staticmethod
    def _resolve_response_session_cap() -> int:
        """Compatibility helper backed by the typed construction snapshot parser."""
        return GatewayTuning.from_environ().response_session_cap

    def _remember_response_session(self, response_id: str, session_id: str) -> None:
        sessions = self._response_sessions
        sessions[response_id] = session_id
        sessions.move_to_end(response_id)
        while len(sessions) > self._response_session_cap:
            sessions.popitem(last=False)

    def _lookup_response_session(self, response_id: str) -> str | None:
        session_id = self._response_sessions.get(response_id)
        if session_id is not None:
            self._response_sessions.move_to_end(response_id)
        return session_id

    def start_account_health_loop(self) -> None:
        _ops_start_account_health_loop(self)

    @staticmethod
    def _health_check_interval() -> float:
        return _ops_health_check_interval()

    @staticmethod
    def _usage_poll_seconds() -> float:
        return _ops_usage_poll_seconds()

    def start_usage_poller(self) -> None:
        _ops_start_usage_poller(self)

    async def discard_session(self, session: ChatGPTWebSession | None = None) -> None:
        async with self._session_lock:
            if (session is None or session is self._session) and self._session is not None:
                try:
                    await self._session.close()
                except Exception:
                    pass
                self._session = None


    def _worker_browsers_connected(self) -> bool:
        return _ops_worker_browsers_connected(self)

    @asynccontextmanager
    async def _lease_session(self, record: ConversationRecord | None = None):
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
        if isinstance(self._worker_factory, MultiAccountWorkerFactory):
            requested_account = record.account_name if record is not None else None
            async with self._worker_factory.lease(requested_account) as session:
                selected_account = getattr(session, "_webgpt_account_name", None)
                if record is not None and record.account_name is None and selected_account:
                    record.account_name = str(selected_account)
                yield session
            return
        affinity_key = (
            (record.conversation_id or record.session_id) if record is not None else None
        )
        if affinity_key and _lease_supports_affinity(self._worker_factory):
            async with cast(Any, self._worker_factory).lease(affinity_key) as session:
                yield session
            return
        async with cast(Any, self._worker_factory).lease() as session:
            yield session


    async def close(self) -> None:
        if self._health_loop_stop is not None:
            self._health_loop_stop.set()
        if self._health_loop_task is not None:
            self._health_loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._health_loop_task
            self._health_loop_task = None
        # USAGE-POLLER-WIRE: cancel the advisory loop before sessions/factories
        # die so no poll tick races the teardown; safe when never started.
        if self._usage_poller is not None:
            await self._usage_poller.stop()
            self._usage_poller = None
        # POOL-POLLER-PERACCT wire-up: cancel every per-account loop too.
        for poller in list(self._account_usage_pollers.values()):
            await poller.stop()
        self._account_usage_pollers.clear()
        self.pool_pressure_board = None
        if self._worker_factory is not None:
            await self._worker_factory.close()
            self._worker_factory = None
        async with self._session_lock:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def liveness(self, request: Request) -> JSONResponse:
        return await _ops_liveness(self, request)

    async def readiness(self, request: Request) -> JSONResponse:
        return await _ops_readiness(self, request)

    async def health(self, request: Request) -> JSONResponse:
        return await _ops_health(self, request)

    async def list_models(self, request: Request) -> JSONResponse:
        return await _ops_list_models(self, request)

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
            if self.mock_backend:
                return await self._complete_mock(
                    record,
                    tail,
                    messages,
                    resolution.response_model,
                    tools,
                    normalized.tool_choice,
                    stream_callback,
                )
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
            # Roadmap A3: a turn that provably never committed on ChatGPT Web
            # may fail over to a fresh account/web session instead of surfacing
            # the raw backend error. The reset is performed by maybe_failover
            # and the client is asked to resend via a retryable error; no
            # internal retry happens here because live-stream callbacks may
            # already have forwarded partial deltas for the failed attempt.
            failover_attempts = 0
            try:
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
            except Exception as exc:
                if await self._maybe_failover_record(
                    record, exc, attempts=failover_attempts
                ):
                    raise FailoverRetryRequired(
                        "Conversation failed over to a fresh ChatGPT web session; "
                        "please resend this request to continue."
                    ) from exc
                raise
            return response, record

    @staticmethod
    def _mock_argument_value(schema: Any, name: str) -> Any:
        return _mock_argument_value_impl(schema, name)

    @classmethod
    def _mock_tool_call(
        cls,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: Any,
    ) -> list[dict[str, Any]] | None:
        return _mock_tool_call_impl(messages, tools, tool_choice)

    @staticmethod
    def _mock_request_calls_for_tool(
        messages: list[dict[str, Any]],
        tools_by_name: dict[str, dict[str, Any]],
    ) -> bool:
        return _mock_request_calls_for_tool_impl(messages, tools_by_name)

    @staticmethod
    def _format_conversational_reply(user_text: str) -> str:
        return _mock_format_conversational_reply(user_text)

    async def _complete_mock(
        self,
        record: ConversationRecord,
        tail: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        stream_callback: Any,
    ) -> tuple[dict[str, Any], ConversationRecord]:
        """Serve a browser-free deterministic turn through the normal adapters."""
        self._validate_tool_result_correlation(record, tail, messages)
        calls = None
        if not any(message.get("role") == "tool" for message in tail):
            calls = self._mock_tool_call(messages, tools, tool_choice)
        if calls:
            content = None
        else:
            user_text = "request"
            for message in reversed(messages):
                if message.get("role") != "user":
                    continue
                raw_val = message.get("content")
                text_val = ""
                if isinstance(raw_val, str):
                    text_val = raw_val.strip()
                elif isinstance(raw_val, list):
                    parts = [
                        b.get("text", "")
                        for b in raw_val
                        if isinstance(b, dict) and isinstance(b.get("text"), str)
                    ]
                    text_val = " ".join(parts).strip()
                clean = re.sub(r"<system-reminder>.*?</system-reminder>", "", text_val, flags=re.DOTALL).strip()
                if clean:
                    user_text = clean
                    break
            content = self._format_conversational_reply(user_text)
            if stream_callback is not None:
                for offset in range(0, len(content), 24):
                    await stream_callback(content[offset : offset + 24])
                    await asyncio.sleep(0)
        response = format_openai_chat_response(
            content,
            calls,
            model=model,
            prompt_text=_messages_prompt_text(messages),
        )
        self.conversations.commit(
            record,
            messages,
            response["choices"][0]["message"],
            response,
            model,
            tools,
            f"mock_{record.session_id}",
            tool_choice,
        )
        self._active_gateway_session_id = record.session_id
        self.completion_runtime._active_gateway_session_id = record.session_id
        return response, record

    async def responses(self, request: Request) -> Response:
        try:
            body = await request.json()
            adapted = parse_responses_request(body)
            adapted = replace(
                adapted, request=replace(adapted.request, client=_client_name(request))
            )
            previous_session = (
                self._lookup_response_session(adapted.previous_response_id)
                if adapted.previous_response_id
                else None
            )
            if adapted.previous_response_id and previous_session is None:
                return _error("Unknown previous_response_id", 404, "response_not_found")
            response, record = await self.complete_normalized(
                adapted.request, previous_session, append_to_session=previous_session is not None
            )
            response_id = f"resp_{uuid.uuid4().hex[:16]}"
            self._remember_response_session(response_id, record.session_id)
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
        except ModelRefusalError as exc:
            # STOP-REASON-REFUSAL: a definitive model refusal leaves the
            # Anthropic boundary as a completed message (HTTP 200,
            # stop_reason:"refusal"), never as a 502 infrastructure error.
            logger.info(
                "anthropic_gateway_request %s",
                json.dumps(
                    {
                        "request_id": request_id,
                        "duration_ms": int((time.monotonic() - started) * 1_000),
                        "error_code": "model_refusal",
                    },
                    separators=(",", ":"),
                ),
            )
            return _anthropic_refusal_response(
                exc,
                model=adapted.request.requested_model,
                prompt_text=_request_prompt_text(adapted.request),
            )
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
        # PARITY-P0-1: thread the client prompt into the chars/4 estimate so
        # non-stream Anthropic responses carry non-zero usage.
        return (
            response_to_anthropic(
                response, prompt_text=_request_prompt_text(adapted.request)
            ),
            record,
        )


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
        # USAGE-CONTRACT-ALIGN: render once for every estimate below.
        prompt_text = _request_prompt_text(request)
        record, _tail, cached = self.conversations.resolve(
            request.messages,
            request.requested_model,
            request.tools,
            None,
            request.tool_choice,
        )
        if cached and record.last_response is not None:
            return (
                response_to_anthropic(
                    record.last_response, prompt_text=prompt_text
                ),
                record,
            )
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
            prompt_text=prompt_text,
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
        return (
            response_to_anthropic(response, prompt_text=prompt_text),
            record,
        )

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
        # PARITY-P0-1: incremental chars/4 usage estimate -- input from the
        # client prompt up front, output accumulated across streamed deltas.
        # USAGE-CONTRACT-ALIGN: render once and reuse for the finalized-
        # payload fallback below instead of rendering twice per turn.
        prompt_text = _request_prompt_text(adapted.request)
        estimator = StreamUsageEstimator(prompt_text)
        base = {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": adapted.request.requested_model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": estimator.snapshot(),
        }
        deltas: asyncio.Queue[str] = asyncio.Queue()
        # Every tool-protocol emit opener (soft <cmd>/<json>, legacy sentinel,
        # DSML and XML markup): once one appears, nothing more may stream.
        emit_openers = (
            "<cmd>",
            "<json>",
            "<WEBGPT_TOOL_CALL>",
            "<|DSML|tool_calls>",
            "<tool_calls>",
        )
        max_opener_len = max(len(tag) for tag in emit_openers)
        stream_buffer = ""
        stream_mode = "undecided"

        async def on_delta(text: str) -> None:
            # R5 BUG-A (live-cli-verify-round5-2026-08-24): the previous
            # protocol-blind sieve leaked raw soft-protocol emit tags
            # (<cmd>...</cmd>) as visible text deltas and then failed closed
            # with "Late tool call cannot be safely streamed" at finalize, so
            # a tool_use parsed on the feedback-delta turn never reached the
            # client execution loop (T3c/T-D/T-D2, reproduced 3/3). This
            # filter withholds everything from the first potential emit tag
            # onward; the finalized payload replay below carries the
            # authoritative tool_use block, and any prose tail held back here
            # is re-emitted by the remainder reconciliation. Ordinary prose
            # still streams progressively (conformance: safe text before
            # completion; sentinels never leak into the text stream).
            nonlocal stream_buffer, stream_mode
            if stream_mode == "swallowed":
                return
            stream_buffer += text
            if stream_mode == "undecided":
                stripped = stream_buffer.lstrip()
                if not stripped:
                    return
                if any(stripped.startswith(tag) for tag in emit_openers):
                    stream_mode = "swallowed"
                    return
                if any(tag.startswith(stripped) for tag in emit_openers):
                    return  # could still become an emit tag; keep buffering
                stream_mode = "text"
            # Text mode: forward proven-safe prose only. Hold back a trailing
            # partial markup candidate so an emit tag split across deltas
            # cannot leak, and swallow from a real emit tag onward.
            cut = len(stream_buffer)
            for tag in emit_openers:
                pos = stream_buffer.find(tag)
                if pos != -1:
                    cut = min(cut, pos)
                    stream_mode = "swallowed"
                    break
            if stream_mode != "swallowed":
                last_lt = stream_buffer.rfind("<")
                if last_lt != -1:
                    suffix = stream_buffer[last_lt:]
                    if (
                        len(suffix) <= max_opener_len
                        and ">" not in suffix
                        and "{" not in suffix
                        and not any(ch.isspace() for ch in suffix)
                    ):
                        cut = min(cut, last_lt)
            safe = stream_buffer[:cut]
            stream_buffer = stream_buffer[cut:]
            if safe:
                estimator.add_delta(safe)
                await deltas.put(safe)

        task = asyncio.create_task(
            self._complete_anthropic(adapted, stream_callback=on_delta)
        )
        delta_task: asyncio.Task[str] | None = None
        emitted: list[str] = []
        started_content = False
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        finished = False
        try:
            yield _sse_event("message_start", {"type": "message_start", "message": base})
            while not finished and (not task.done() or not deltas.empty()):
                if delta_task is None:
                    delta_task = asyncio.create_task(deltas.get())
                waiting: set[asyncio.Task[Any]] = {task}
                if delta_task is not None:
                    waiting.add(delta_task)
                done, _pending = await asyncio.wait(
                    waiting, timeout=self.stream_idle_seconds, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    if await request.is_disconnected():
                        return
                    elapsed = loop.time() - started_at
                    if elapsed >= self.stream_deadline_seconds:
                        # Never keep a client on an endless ping treadmill: fail
                        # the turn through the no-retry stream close so the SDK
                        # observes a clean terminator instead of pings forever.
                        raise GenerationTimeout(
                            "live stream exceeded "
                            f"{self.stream_deadline_seconds:.0f}s without completing"
                        )
                    # PING-WIRE: emit the canonical Anthropic ``event: ping``
                    # frame instead of an SSE comment -- same wire semantics,
                    # but heartbeats survive proxies that strip comments.
                    yield _sse_event("ping", {"type": "ping"})
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
                # STREAM-CORRECT-DEDUP (2026-08-26, parity-delta-audit G1):
                # per-attempt remainder reconciliation.  ``emitted`` holds ONLY
                # first-attempt live deltas -- CompletionRuntime cancels delta
                # forwarding at each attempt's terminal event, so a corrected /
                # failed-over attempt is never streamed here.  Therefore:
                #   * prefix match  -> append just the tail (delivered bytes
                #     are NEVER replayed);
                #   * mismatch      -> the full finalized text is its FIRST
                #     delivery on this stream, not a replay of anything already
                #     sent.  Do not "deduplicate" it away.
                final_text = payload["content"][0]["text"]
                streamed_text = "".join(emitted)
                remainder = final_text[len(streamed_text) :] if final_text.startswith(streamed_text) else final_text
                if remainder:
                    estimator.add_delta(remainder)
                    yield _sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": remainder},
                        },
                    )
                yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
                # R5 BUG-A hardening: blocks that follow the streamed text
                # (notably a parsed tool_use) must still reach the client --
                # dropping them desyncs stop_reason="tool_use" from the actual
                # content blocks and silently kills the client execution loop.
                async for event in self._anthropic_block_events(payload, start_index=1):
                    yield event
                # PARITY-P0-1: charge blocks that never passed through
                # on_delta (parsed tool_use arguments, held-back prose) so the
                # accumulated output estimate covers everything delivered.
                for extra in payload["content"][1:]:
                    if not isinstance(extra, dict):
                        continue
                    if extra.get("type") == "tool_use":
                        estimator.add_delta(
                            json.dumps(extra.get("input", {}), ensure_ascii=False)
                        )
                    elif extra.get("type") == "text":
                        estimator.add_delta(str(extra.get("text") or ""))
                yield _sse_event(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": payload["stop_reason"], "stop_sequence": None},
                        "usage": estimator.snapshot(),
                    },
                )
                yield _sse_event("message_stop", {"type": "message_stop"})
            else:
                async for event in self._anthropic_content_events(
                    payload, usage=_anthropic_payload_usage(payload, prompt_text)
                ):
                    yield event
            # The terminal event has been flushed. Return immediately so the
            # response body closes cleanly: no ping/heartbeat may ever follow
            # message_stop.
            finished = True
        except asyncio.CancelledError:
            raise
        except ModelRefusalError as exc:
            # STOP-REASON-REFUSAL: close the already-open stream as a
            # completed turn whose text block explains the refusal, ending
            # with ``stop_reason:"refusal"``.  A completed turn is the one
            # outcome no client retries (R4-DOUBLING), and the CLI learns
            # this was a deliberate model decline instead of an outage.
            refusal_text = f"[webgpt-gateway:model_refusal] {exc}".strip()
            logger.info(
                "anthropic_stream_model_refusal %s",
                json.dumps(
                    {"exception": type(exc).__name__, "message": str(exc)},
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            )
            estimator.add_delta(refusal_text)
            if not started_content:
                yield _sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            yield _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "text_delta",
                        "text": ("\n\n" + refusal_text if started_content else refusal_text),
                    },
                },
            )
            yield _sse_event(
                "content_block_stop", {"type": "content_block_stop", "index": 0}
            )
            yield _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "refusal", "stop_sequence": None},
                    "usage": estimator.snapshot(),
                },
            )
            yield _sse_event("message_stop", {"type": "message_stop"})
            return
        except Exception as exc:
            response = _anthropic_exception_error(self, exc)
            error_payload = json.loads(bytes(response.body))
            logger.warning(
                "anthropic_live_stream_error %s",
                json.dumps(
                    {
                        "exception": type(exc).__name__,
                        "error_type": error_payload["error"]["type"],
                        "message": error_payload["error"]["message"],
                    },
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            )
            if not started_content:
                # LATE-FAIL-SURFACE: nothing has been delivered yet, so
                # surfacing the failure cannot lose or duplicate content --
                # emit the standard Anthropic ``event: error`` envelope. The
                # SDK may retry, but the replacement generation starts clean
                # and the CLI learns the turn actually failed.
                async for event in self._anthropic_stream_error_event(error_payload):
                    yield event
                return
            # R4-DOUBLING: after message_start the HTTP status is fixed at 200,
            # so an SSE ``error`` event (or an abrupt EOF) is the only failure
            # signal left -- and the Anthropic SDK retries 5xx-class errors and
            # connection errors while Claude Code's own loop re-POSTs on stream
            # failures, each retry spawning a duplicate ChatGPT Web generation.
            # Content already streamed makes a retry a partial-output
            # duplicator, so keep closing with the regular terminator: a
            # completed turn is the one outcome no client ever retries. The
            # late_failure_masked counter measures how often this masks a
            # truncated turn as complete. Errors that reach this handler have
            # already exhausted the gateway's internal correction budget, so a
            # client retry would only burn more quota.
            self.late_failure_masked += 1
            logger.warning(
                "late_failure_masked %s",
                json.dumps(
                    {
                        "exception": type(exc).__name__,
                        "error_type": error_payload["error"]["type"],
                        "message": error_payload["error"]["message"],
                    },
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            )
            async for event in self._anthropic_no_retry_close(
                error_payload, started_content, estimator=estimator
            ):
                yield event
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
    async def _anthropic_no_retry_close(
        error_payload: dict[str, Any],
        started_content: bool,
        estimator: StreamUsageEstimator | None = None,
        prompt_text: str = "",
    ):
        """Terminate an already-open Anthropic stream without a retry signal.

        The Anthropic SDK retries 408/409/429/5xx responses and connection
        errors, and Claude Code's outer loop re-POSTs after stream failures;
        both paths create a fresh generation on ChatGPT Web. Ending the stream
        with the ordinary ``content_block_stop`` -> ``message_delta`` ->
        ``message_stop`` terminator reads as a completed turn, which nothing
        retries. The reason still reaches the CLI verbatim inside the message
        text.

        PARITY-P0-1: the reason text streamed out here is counted toward the
        usage estimate before the terminal ``message_delta``, so even an
        error-only turn reports a full Anthropic usage object (input from the
        prompt estimate, output > 0) instead of a bare zeroed stub.
        """
        error = error_payload.get("error", {})
        text = (
            f"[webgpt-gateway:{error.get('type', 'api_error')}] "
            f"{error.get('message', 'gateway error')}"
        )
        emitted_text = text if not started_content else f"\n\n{text}"
        if estimator is None:
            estimator = StreamUsageEstimator(prompt_text)
        estimator.add_delta(emitted_text)
        if not started_content:
            yield _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        yield _sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": emitted_text},
            },
        )
        yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": estimator.snapshot(),
            },
        )
        yield _sse_event("message_stop", {"type": "message_stop"})

    @staticmethod
    async def _anthropic_stream_error_event(error_payload: dict[str, Any]):
        """Emit one standard Anthropic ``event: error`` frame and stop.

        LATE-FAIL-SURFACE: used when a live stream fails before any content
        block was opened. Nothing was delivered, so an explicit error cannot
        destroy client work the way masking a truncated turn does; the SDK
        turns the frame into a raised exception and Claude Code surfaces the
        real failure instead of treating a dead turn as complete. Types
        outside the Anthropic contract collapse to ``api_error`` so every
        SDK build can parse it.
        """
        error = error_payload.get("error", {})
        err_type = str(error.get("type") or "api_error")
        if err_type not in _ANTHROPIC_STREAM_ERROR_TYPES:
            err_type = "api_error"
        yield _sse_event(
            "error",
            {
                "type": "error",
                "error": {
                    "type": err_type,
                    "message": str(error.get("message") or "gateway error"),
                },
            },
        )

    @staticmethod
    async def _anthropic_stream(
        payload: dict[str, Any], usage: dict[str, Any] | None = None
    ):
        base = {**payload, "content": []}
        yield _sse_event("message_start", {"type": "message_start", "message": base})
        async for event in WebChatAPIServer._anthropic_content_events(payload, usage=usage):
            yield event

    @staticmethod
    async def _anthropic_content_events(
        payload: dict[str, Any], usage: dict[str, Any] | None = None
    ):
        async for event in WebChatAPIServer._anthropic_block_events(payload):
            yield event
        yield _sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": payload["stop_reason"], "stop_sequence": None},
                # PARITY-P0-1: never emit a zero output estimate -- fall back
                # to the finalized payload's own content when no incremental
                # estimator was threaded through.
                "usage": usage if usage is not None else _anthropic_payload_usage(payload),
            },
        )
        yield _sse_event("message_stop", {"type": "message_stop"})

    @staticmethod
    async def _anthropic_block_events(
        payload: dict[str, Any], start_index: int = 0
    ):
        for index, block in enumerate(payload["content"], start=0):
            if index < start_index:
                continue
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
                # JSON-DELTA-CHUNK: split the serialized arguments into
                # bounded pieces -- concatenated by the client they reproduce
                # the exact same JSON as the previous single-frame burst.
                partial_json = json.dumps(block.get("input", {}), ensure_ascii=False)
                for offset in range(0, len(partial_json), _JSON_DELTA_CHUNK_CHARS):
                    yield _sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": partial_json[
                                    offset : offset + _JSON_DELTA_CHUNK_CHARS
                                ],
                            },
                        },
                    )
            else:
                yield _sse_event(
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": block},
                )
            yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": index})

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
            prompt_text=_messages_prompt_text(messages),
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

    async def _maybe_failover_record(
        self,
        record: ConversationRecord,
        exc: Exception,
        *,
        attempts: int = 0,
    ) -> bool:
        """Apply roadmap A3 failover when the failed turn cannot have committed.

        Only meaningful on the multi-account path; the conversation binding is
        reset (never migrated) so the next turn routes to a fresh account.
        """
        if not isinstance(self._worker_factory, MultiAccountWorkerFactory):
            return False
        previous_account = record.account_name
        previous_conversation_id = record.conversation_id

        def _emit(reason: str) -> None:
            logger.info("conversation_failover %s", reason)
            self.trace.emit(
                "webchat",
                "conversation_failover",
                session_id=record.session_id,
                metadata={
                    "reason": reason,
                    "previous_account_name": previous_account,
                    "previous_conversation_id": previous_conversation_id,
                    "error_type": type(exc).__name__,
                },
            )

        reconciled_user_turn_present: bool | None = None
        if isinstance(exc, CommitUnknown):
            reconciled_user_turn_present = await self._reconcile_user_turn_present(record)
        return maybe_failover(
            record,
            exc,
            attempts=attempts,
            reconciled_user_turn_present=reconciled_user_turn_present,
            store=self.conversations,
            emit=_emit,
        )

    async def _reconcile_user_turn_present(
        self, record: ConversationRecord
    ) -> bool | None:
        """Ask authoritative web history whether the pending user turn landed.

        Returns ``None`` (fail-closed verdict for failover purposes) whenever
        reconciliation is impossible, errors out, or yields no answer.
        """
        prompt = record.pending_prompt
        if not prompt:
            return None
        try:
            async with self._lease_session(record) as session:
                reconcile = getattr(session, "reconcile", None)
                if reconcile is None:
                    return None
                reconciliation = await reconcile(prompt)
        except Exception:
            logger.warning("failover_reconcile_failed", exc_info=True)
            return None
        try:
            return bool(reconciliation.user_turn_present)
        except AttributeError:
            return None

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
            prompt_text=_messages_prompt_text(messages),
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
                async with self._lease_session(record) as session:
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
        response = format_openai_chat_response(
            clean,
            calls or None,
            model=model,
            prompt_text=_messages_prompt_text(messages),
        )
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
            # OPENAI-USAGE-WIRE: final chunk carries the accumulated chars/4
            # estimate over the submitted prompt and everything the client saw.
            yield format_openai_usage_chunk(
                model=model,
                completion_id=completion_id,
                prompt_text=_messages_prompt_text(messages),
                completion_text="".join(emitted_text),
                completion_tool_calls=calls,
            )
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
            # OPENAI-USAGE-WIRE: a committed response already carries the
            # estimated usage from its original turn; otherwise fall back to
            # estimating over the cached message (prompt unknown on replay).
            stored = response.get("usage")
            if isinstance(stored, dict) and stored.get("total_tokens"):
                yield format_openai_usage_chunk(
                    model=model,
                    completion_id=completion_id,
                    prompt_tokens=int(stored.get("prompt_tokens") or 0),
                    completion_tokens=int(stored.get("completion_tokens") or 0),
                )
            else:
                yield format_openai_usage_chunk(
                    model=model,
                    completion_id=completion_id,
                    completion_text=message.get("content"),
                    completion_tool_calls=message.get("tool_calls"),
                )
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
    def _env_float(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            return default
        return value if value > 0 else default

    def _resolve_stream_deadline_seconds(self) -> float:
        """Total lifetime budget for one live SSE stream.

        Claude Code treats an open, silent connection as a hang.  The default is
        generous enough for the slowest legitimate turn (worker queue wait plus
        browser generation plus slack) while guaranteeing the stream terminates
        with an explicit SSE error event instead of pinging forever.
        """
        return self._env_float(
            "WEBGPT_STREAM_DEADLINE_SECONDS",
            self.queue_timeout + self.generation_timeout_seconds + 30.0,
        )

    @staticmethod
    def _map_exception(exc: Exception) -> JSONResponse:
        if _is_overloaded_rate_limit(exc):
            # OVERLOADED-529: capacity exhaustion gets Anthropic's dedicated
            # status so clients back off exactly like they do against
            # api.anthropic.com, instead of reading it as a personal quota.
            return _error(str(exc), 529, "overloaded_error")
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
        if _is_browser_crash(exc):
            # A Playwright "Target crashed"-style failure is always retryable
            # infrastructure loss, never a generic 500 (BUG-B).
            logger.warning("browser_crash_classified_as_disconnected: %s", exc)
            return _error(str(exc), 503, "browser_disconnected")
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
        try:
            await server._worker_factory.start()
        except Exception as exc:
            logger.warning("webgpt worker warmup failed; continuing lazy startup", exc_info=exc)
            server.trace.emit(
                "webchat",
                "prewarm_failed",
                metadata={"error_type": type(exc).__name__},
            )
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
    server.start_account_health_loop()
    # USAGE-POLLER-WIRE: no-op (nothing constructed) while the flag is off.
    server.start_usage_poller()
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
    account_profiles: Mapping[str, str] | None = None,
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
    generation_timeout_seconds: float | None = None,
    require_anonymous: bool = False,
    mock_backend: bool = False,
) -> Starlette:
    server = WebChatAPIServer(
        headless=headless,
        persistent=persistent,
        profile_dir=profile_dir,
        account_profiles=account_profiles,
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
        mock_backend=mock_backend,
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
