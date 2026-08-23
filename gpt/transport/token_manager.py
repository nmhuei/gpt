"""Browser-backed credentials for the hybrid HTTP transport.

The browser remains the authority for ChatGPT authentication.  This module only
reads the authenticated context and performs the browser-context calls that
cannot reliably be reproduced outside Chromium (notably sentinel requirements).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from gpt.state import AuthRequired, ProtocolChanged


@dataclass(frozen=True)
class TokenBundle:
    """Immutable snapshot of credentials needed by ``CurlCffiTransport``."""

    access_token: str
    cookies: Mapping[str, str]
    cf_clearance: str | None = None
    oai_device_id: str | None = None
    is_local_mock: bool = False

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items())


@dataclass(frozen=True)
class SentinelTokens:
    """Per-turn browser-issued challenge tokens."""

    requirements_token: str | None = None
    proof_token: str | None = None
    turnstile_token: str | None = None


class TokenManager:
    """Extract and periodically refresh authenticated browser credentials."""

    def __init__(
        self,
        page: Any,
        *,
        refresh_interval: float = 1_800,
        origin: str = "https://chatgpt.com",
        auto_login: Callable[[], Awaitable[bool]] | None = None,
        allow_local_mock: bool | None = None,
    ) -> None:
        if refresh_interval <= 0:
            raise ValueError("refresh_interval must be positive")
        self.page = page
        self.refresh_interval = refresh_interval
        self.origin = origin.rstrip("/")
        self._auto_login = auto_login
        self.allow_local_mock = (
            True if allow_local_mock is None else allow_local_mock
        )
        self._bundle: TokenBundle | None = None
        self._last_refresh = 0.0
        self._lock = asyncio.Lock()
        self._auto_login_attempted = False

    async def extract_all(self) -> TokenBundle:
        """Read cookies, access token, and device id from the browser context."""
        async with self._lock:
            return await self._extract_all_unlocked()

    async def _extract_all_unlocked(self) -> TokenBundle:
        context = getattr(self.page, "context", None)
        if context is None or not hasattr(context, "cookies"):
            raise ProtocolChanged("The browser page does not expose a cookie context.")
        browser_cookies = await context.cookies()
        cookies = {
            item["name"]: item["value"]
            for item in browser_cookies
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str)
        }
        session = await self.page.evaluate(
            """async () => {
                const response = await fetch('/api/auth/session', {
                    credentials: 'include'
                });
                if (!response.ok) return {};
                return response.json();
            }"""
        )
        access_token = _find_string(session, "accessToken", "access_token")
        if not access_token and await self._attempt_auto_login():
            # A successful login updates cookies/storage asynchronously.  Read
            # the session endpoint again instead of trusting the login helper.
            session = await self.page.evaluate(
                """async () => {
                    const response = await fetch('/api/auth/session', {
                        credentials: 'include'
                    });
                    if (!response.ok) return {};
                    return response.json();
                }"""
            )
            access_token = _find_string(session, "accessToken", "access_token")
            browser_cookies = await context.cookies()
            cookies = {
                item["name"]: item["value"]
                for item in browser_cookies
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and isinstance(item.get("value"), str)
            }
        if not access_token:
            if self.allow_local_mock:
                self._bundle = _local_mock_bundle()
                self._last_refresh = time.monotonic()
                return self._bundle
            raise AuthRequired("ChatGPT browser session has no access token.")
        device_id = await self.page.evaluate(
            """() => localStorage.getItem('oai-device-id')
                || localStorage.getItem('oai_device_id')"""
        )
        if not isinstance(device_id, str) or not device_id:
            device_id = cookies.get("oai-device-id") or cookies.get("oai_device_id")
        self._bundle = TokenBundle(
            access_token=access_token,
            cookies=MappingProxyType(cookies),
            cf_clearance=cookies.get("cf_clearance"),
            oai_device_id=device_id,
        )
        self._last_refresh = time.monotonic()
        return self._bundle

    async def _attempt_auto_login(self) -> bool:
        """Try the configured account login once, without exposing credentials."""
        if self._auto_login_attempted:
            return False
        self._auto_login_attempted = True
        callback = self._auto_login or _configured_auto_login
        try:
            return bool(await callback())
        except Exception:
            # The subsequent auth error is intentionally generic: exceptions
            # from browser identity providers can contain sensitive details.
            return False

    async def refresh_if_needed(self) -> TokenBundle:
        """Return a current snapshot, refreshing it at most once per interval."""
        if self._bundle is not None and time.monotonic() - self._last_refresh < self.refresh_interval:
            return self._bundle
        async with self._lock:
            if (
                self._bundle is not None
                and time.monotonic() - self._last_refresh < self.refresh_interval
            ):
                return self._bundle
            return await self._extract_all_unlocked()

    async def get_sentinel_tokens(self, conversation_id: str | None = None) -> SentinelTokens:
        """Fetch browser-context sentinel requirements for one conversation turn."""
        if self._bundle is not None and self._bundle.is_local_mock:
            return SentinelTokens(requirements_token="local-mock-sentinel")
        result = await self.page.evaluate(
            """async (conversationId) => {
                const response = await fetch('/backend-anon/sentinel/chat-requirements', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'content-type': 'application/json'},
                    body: JSON.stringify({conversation_id: conversationId || undefined})
                });
                if (!response.ok) return {status: response.status};
                return response.json();
            }""",
            conversation_id,
        )
        if not isinstance(result, dict):
            raise ProtocolChanged("Sentinel requirements response was not an object.")
        if isinstance(result.get("status"), int) and result["status"] >= 400:
            raise AuthRequired("ChatGPT rejected sentinel requirements.")
        return SentinelTokens(
            requirements_token=_find_string(
                result,
                "requirements_token",
                "chat_requirements_token",
                "sentinel_token",
            ),
            proof_token=_find_string(result, "proof_token", "proofToken"),
            turnstile_token=_find_string(result, "turnstile_token", "turnstileToken"),
        )


def _find_string(value: Any, *keys: str) -> str | None:
    """Find a non-empty string in the small nested response envelopes ChatGPT uses."""
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for candidate in value.values():
            found = _find_string(candidate, *keys)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_string(candidate, *keys)
            if found:
                return found
    return None


def local_mock_mode_enabled() -> bool:
    """Return whether this process may use the browser-free local transport.

    This is deliberately opt-in.  A placeholder credential must never be sent
    to ChatGPT in a normal gateway process.
    """
    mode = os.environ.get("WEBGPT_MODE", "").strip().casefold()
    flag = os.environ.get("WEBGPT_LOCAL_MOCK", "").strip().casefold()
    return mode in {"dev", "development", "test", "testing"} or flag in {
        "1",
        "true",
        "yes",
        "on",
    }


def _local_mock_bundle() -> TokenBundle:
    """Create a clearly marked credential snapshot for local-only responses."""
    cookies = MappingProxyType(
        {
            "cf_clearance": "local-mock-clearance",
            "oai-device-id": "local-mock-device",
        }
    )
    return TokenBundle(
        access_token="local-mock-token",
        cookies=cookies,
        cf_clearance=cookies["cf_clearance"],
        oai_device_id=cookies["oai-device-id"],
        is_local_mock=True,
    )


async def _configured_auto_login() -> bool:
    """Run the existing login workflow when all .env credentials are present."""
    from gpt.auth import AutoLoginManager, LoginCredentials
    from gpt.config.settings import load_config

    config = load_config()
    if not (config.email and config.password and config.totp_key):
        return False
    manager = AutoLoginManager(
        profile_dir=config.profile_dir,
        headless=config.headless,
    )
    return await manager.login(
        LoginCredentials(config.email, config.password, config.totp_key)
    )
