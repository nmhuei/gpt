from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    ViewportSize,
    async_playwright,
)

from gpt.profile import DEFAULT_PROFILE_DIR, ensure_profile_dir
from gpt.state import BrowserDisconnected

logger = logging.getLogger("gpt.transport.browser")

DEFAULT_VIEWPORT: ViewportSize = {"width": 1280, "height": 800}

# Owner-confirmed anti-Cloudflare strategy: every browser launch must prefer
# CloakBrowser (anti-fingerprint) so cf_clearance is minted from a consistent,
# hardened fingerprint.  A vanilla Playwright Chromium has none of that hardening
# and its clearances get challenged immediately, so falling back to it is only
# allowed when the operator explicitly opts in via WEBGPT_REQUIRE_CLOAKBROWSER=0.
_REQUIRE_CLOAK_ENV = "WEBGPT_REQUIRE_CLOAKBROWSER"


class BrowserManager:
    """Owns one Playwright browser/context with an optional persistent profile."""

    def __init__(
        self,
        headless: bool = True,
        persistent: bool = False,
        profile_dir: Path | str | None = None,
        user_agent: str | None = None,
        viewport: ViewportSize | None = None,
        executable_path: str | None = None,
        cdp_url: str | None = None,
        proxy: str | None = None,
    ):
        self.headless = headless
        self.persistent = persistent
        self.profile_dir = Path(profile_dir) if profile_dir else DEFAULT_PROFILE_DIR
        self.user_agent = user_agent
        self.viewport = viewport or DEFAULT_VIEWPORT
        self.executable_path = executable_path
        self.cdp_url = cdp_url
        self.proxy = proxy or os.environ.get("WEBGPT_PROXY", "").strip() or None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._lock = asyncio.Lock()
        self._initial_page_claimed = False
        self._attached_over_cdp = False
        # Which backend actually served the last start(): "cdp",
        # "cloakbrowser", or "chromium-fallback" (unhardened — inspect this in
        # diagnostics whenever a clearance keeps getting challenged).
        self.launch_backend: str | None = None

    @property
    def context(self) -> BrowserContext | None:
        return self._context

    @property
    def connected(self) -> bool:
        if self._context is None:
            return False
        return self._browser is None or self._browser.is_connected()

    async def start(self) -> BrowserContext:
        async with self._lock:
            if self._context is not None:
                return self._context
            self._playwright = await async_playwright().start()
            if self.cdp_url:
                try:
                    self._validate_local_cdp_url(self.cdp_url)
                    self._browser = await self._playwright.chromium.connect_over_cdp(
                        self.cdp_url
                    )
                    # From this point onward the browser is never ours to
                    # close, including the exceptional no-context case.
                    self._attached_over_cdp = True
                    if not self._browser.contexts:
                        raise BrowserDisconnected("CDP browser has no default context.")
                    self._context = self._browser.contexts[0]
                    self.launch_backend = "cdp"
                    return self._context
                except Exception:
                    await self._cleanup_unlocked()
                    raise
            options: dict[str, Any] = {
                "headless": self.headless,
                "viewport": self.viewport,
            }
            if self.user_agent:
                options["user_agent"] = self.user_agent
            if self.executable_path:
                executable = Path(self.executable_path).expanduser()
                if not executable.is_file():
                    await self._playwright.stop()
                    self._playwright = None
                    raise FileNotFoundError(executable)
                options["executable_path"] = str(executable)
            try:
                if self.persistent:
                    try:
                        from cloakbrowser import launch_persistent_context_async
                        mem_args = [
                            "--disable-dev-shm-usage",
                            "--js-flags=--max-old-space-size=512",
                            "--renderer-process-limit=2",
                            "--disable-speech-api",
                            "--disable-background-networking",
                        ]
                        self._context = await launch_persistent_context_async(
                            user_data_dir=str(ensure_profile_dir(self.profile_dir)),
                            headless=self.headless,
                            viewport=self.viewport,
                            proxy=self.proxy,
                            args=mem_args,
                        )
                        self.launch_backend = "cloakbrowser"
                    except Exception as exc:
                        # CloakBrowser-first policy: a vanilla Chromium fallback
                        # must be an explicit operator decision and is always
                        # announced loudly, because its fingerprint gets
                        # challenged by Cloudflare immediately.
                        self._guard_vanilla_fallback("persistent", exc)
                        logger.warning(
                            "CloakBrowser persistent launch failed (%s); falling back "
                            "to vanilla Playwright Chromium WITHOUT anti-fingerprint "
                            "hardening. Cloudflare will likely challenge this "
                            "browser. Repair cloakbrowser, or set %s=0 to silence "
                            "this refusal path.",
                            exc,
                            _REQUIRE_CLOAK_ENV,
                        )
                        stealth_args = [
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-infobars",
                        ]
                        options["args"] = stealth_args
                        if self.proxy:
                            options["proxy"] = {"server": self.proxy}
                        self._context = await self._playwright.chromium.launch_persistent_context(
                            user_data_dir=str(ensure_profile_dir(self.profile_dir)), **options
                        )
                        self.launch_backend = "chromium-fallback"
                else:
                    try:
                        from cloakbrowser import launch_async
                        self._browser = await launch_async(
                            headless=self.headless,
                        )
                        self._context = await self._browser.new_context(viewport=self.viewport)
                        self.launch_backend = "cloakbrowser"
                    except Exception as exc:
                        self._guard_vanilla_fallback("ephemeral", exc)
                        logger.warning(
                            "CloakBrowser ephemeral launch failed (%s); falling back "
                            "to vanilla Playwright Chromium WITHOUT anti-fingerprint "
                            "hardening. Cloudflare will likely challenge this "
                            "browser. Repair cloakbrowser, or set %s=0 to silence "
                            "this refusal path.",
                            exc,
                            _REQUIRE_CLOAK_ENV,
                        )
                        launch_keys = {"headless", "executable_path"}
                        launch_options = {k: v for k, v in options.items() if k in launch_keys}
                        launch_options["args"] = [
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-infobars",
                        ]
                        context_options = {k: v for k, v in options.items() if k not in launch_keys}
                        self._browser = await self._playwright.chromium.launch(**launch_options)
                        self._context = await self._browser.new_context(**context_options)
                        self.launch_backend = "chromium-fallback"

                return self._context
            except Exception:
                await self._cleanup_unlocked()
                raise

    @staticmethod
    def _guard_vanilla_fallback(mode: str, cause: Exception) -> None:
        """Refuse the unhardened Chromium fallback unless explicitly allowed.

        Default is strict: if CloakBrowser cannot launch, stop here instead of
        silently minting clearances from a fingerprint Cloudflare will flag.
        Operators opt into the fallback with WEBGPT_REQUIRE_CLOAKBROWSER=0.
        """
        raw = os.environ.get(_REQUIRE_CLOAK_ENV, "").strip().casefold()
        required = raw not in {"0", "false", "no", "off"}
        if required:
            raise BrowserDisconnected(
                f"CloakBrowser {mode} launch failed ({cause}) and the vanilla "
                f"Chromium fallback is disabled by default. Repair cloakbrowser "
                f"or set {_REQUIRE_CLOAK_ENV}=0 to allow the unhardened fallback."
            ) from cause

    @staticmethod
    def _validate_local_cdp_url(url: str) -> None:
        """CDP grants full browser access, so only accept a loopback endpoint."""
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https", "ws", "wss"}:
            raise ValueError("CDP URL must use http(s) or ws(s).")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("CDP URL must point to a loopback address.")

    async def new_page(self) -> Page:
        context = await self.start()
        if not self.connected:
            raise BrowserDisconnected("Browser context disconnected.")
        if (
            self.persistent
            and not self._attached_over_cdp
            and not self._initial_page_claimed
            and context.pages
        ):
            page = context.pages[0]
            self._initial_page_claimed = True
        else:
            page = await context.new_page()
        page.set_default_timeout(30_000)
        return page

    async def _cleanup_unlocked(self) -> None:
        # A persistent context owns its browser process and must be closed
        # directly.  For an ephemeral launch the Browser owns its contexts, so
        # browser.close() is the single shutdown boundary.  Closing both layers
        # can race Playwright's transport and leave noisy unhandled futures.
        if self.persistent and not self._attached_over_cdp and self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser is not None and not self._attached_over_cdp:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        # A CDP Browser belongs to the user.  Do not close its default context
        # or browser; stopping Playwright only drops this tool's connection.
        if self._attached_over_cdp:
            self._browser = None
        self._context = None
        if self._playwright is not None:
            await asyncio.sleep(0.05)
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._initial_page_claimed = False
        self._attached_over_cdp = False

    async def stop(self) -> None:
        async with self._lock:
            await self._cleanup_unlocked()

    async def __aenter__(self) -> BrowserManager:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()
