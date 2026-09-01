from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from gpt.auth.accounts import AccountStore
from gpt.transport.breaker import global_rate_limit_breaker
from gpt.transport.multi_account import MultiAccountWorkerFactory
from gpt.transport.usage_poller import (
    POLL_SECONDS_ENV,
    UsagePoller,
    create_account_pollers,
    make_web_token_cache_provider,
)

logger = logging.getLogger("gpt.webchat.api")
DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 300.0


def health_check_interval() -> float:
    raw = os.environ.get("WEBGPT_HEALTH_CHECK_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS


def usage_poll_seconds() -> float:
    raw = os.environ.get(POLL_SECONDS_ENV, "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def worker_browsers_connected(server: Any) -> bool:
    factory = server._worker_factory
    if factory is None:
        return False
    if isinstance(factory, MultiAccountWorkerFactory):
        return factory.browsers_connected
    return bool(factory.browser_manager.connected)


def start_account_health_loop(server: Any) -> None:
    tracker = server._account_health_tracker
    factory = server._worker_factory
    if tracker is None or server._health_loop_task is not None:
        return
    if not isinstance(factory, MultiAccountWorkerFactory):
        return

    async def _loop() -> None:
        from gpt.transport.account_health import periodic_health_loop

        await periodic_health_loop(
            tracker,
            AccountStore(),
            list(factory.factories),
            server._health_check_interval(),
            stop_event=server._health_loop_stop,
        )

    server._health_loop_stop = asyncio.Event()
    server._health_loop_task = asyncio.create_task(
        _loop(), name="webgpt-account-health-loop"
    )


def start_usage_poller(server: Any) -> None:
    if server._usage_poller is not None or server._account_usage_pollers:
        return
    if server._usage_poll_seconds() <= 0:
        return

    if server.pool_rate_limit_breakers:
        token_provider_factory = None
        if all(
            name in server.account_profiles
            for name in server.pool_rate_limit_breakers
        ):
            def token_provider_factory(name: str):
                return make_web_token_cache_provider(server.account_profiles[name])
        account_pollers, board = create_account_pollers(
            server.pool_rate_limit_breakers,
            token_provider_factory=token_provider_factory,
        )
        if account_pollers:
            for poller in account_pollers.values():
                poller.start()
            server._account_usage_pollers = account_pollers
            server.pool_pressure_board = board
            return

    web_profile: str | None = None
    if len(server.account_profiles) == 1:
        web_profile = next(iter(server.account_profiles.values()))
    elif not server.account_profiles and server.profile_dir:
        web_profile = server.profile_dir

    token_provider = (
        make_web_token_cache_provider(web_profile) if web_profile else None
    )
    server._usage_poller = UsagePoller(
        global_rate_limit_breaker(),
        token_provider=token_provider,
    )
    server._usage_poller.start()


async def liveness(_server: Any, _request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "status": "ok"})


async def readiness(server: Any, _request: Request) -> JSONResponse:
    if server.mock_backend:
        return JSONResponse(
            {
                "ready": True,
                "browser": "not_required",
                "authenticated": False,
                "auth_status": "not_required",
                "backend": "mock",
                "workers": {
                    "max": None,
                    "live": 0,
                    "idle": 0,
                    "leased": 0,
                    "queued": 0,
                },
            }
        )
    try:
        async with server._lease_session() as session:
            if server.transport == "hybrid":
                auth_status = "authenticated"
                browser_connected = worker_browsers_connected(server)
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
                "browser": "ready" if browser_connected else "disconnected",
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

    if server._worker_factory is not None:
        stats = await server._worker_factory.stats()
        payload["workers"] = {
            "max": stats.max_workers,
            "live": stats.live_workers,
            "idle": stats.idle_workers,
            "leased": stats.leased_workers,
            "queued": stats.queue_waiters,
        }
    return JSONResponse(payload, status_code=200 if ready else 503)


async def health(server: Any, _request: Request) -> JSONResponse:
    if server.mock_backend:
        return JSONResponse(
            {
                "ok": True,
                "status": "ok",
                "browser": "not_required",
                "authenticated": False,
                "auth_status": "not_required",
                "backend": "mock",
                "active_sessions": len(server.conversations),
            }
        )

    session = server._session
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
        "active_sessions": len(server.conversations),
    }
    if server._usage_poller is not None:
        payload["usage"] = server._usage_poller.state()
    elif server._account_usage_pollers:
        payload["usage"] = {
            "accounts": {
                name: poller.state()
                for name, poller in sorted(server._account_usage_pollers.items())
            }
        }

    if server._worker_factory is not None:
        stats = await server._worker_factory.stats()
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
            "ready" if worker_browsers_connected(server) else "not_started"
        )
    return JSONResponse(payload)


async def list_models(server: Any, _request: Request) -> JSONResponse:
    default_model: dict[str, Any] = {
        "id": "chatgpt-web",
        "object": "model",
        "created": 0,
        "owned_by": "chatgpt-web",
        "display_name": "ChatGPT Web default",
        "available": True,
    }
    data: list[dict[str, Any]] = [default_model]

    if server.mock_backend:
        default_model.update(
            {
                "id": "mock-backend",
                "owned_by": "webgpt-mock",
                "display_name": "WebGPT deterministic mock backend",
            }
        )
        return JSONResponse({"object": "list", "data": data})

    runtime_active = server._session is not None or bool(
        server._worker_factory is not None and worker_browsers_connected(server)
    )
    if not runtime_active:
        return JSONResponse({"object": "list", "data": data})

    try:
        async with server._lease_session() as session:
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


__all__ = [
    "DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS",
    "health",
    "health_check_interval",
    "list_models",
    "liveness",
    "readiness",
    "start_account_health_loop",
    "start_usage_poller",
    "usage_poll_seconds",
    "worker_browsers_connected",
]
