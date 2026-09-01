from __future__ import annotations

import asyncio
import logging
import random
import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

try:
    from cloakbrowser import launch_persistent_context_async
    CLOAK_AVAILABLE = True
except ImportError:
    CLOAK_AVAILABLE = False

from gpt.auth.totp import generate_totp_code
from gpt.profile import DEFAULT_PROFILE_DIR, ensure_profile_dir
from gpt.state import ChatGPTWebError
from gpt.transport.browser import BrowserManager
from gpt.transport.challenge import (
    ChallengeDetectedError,
    ChallengeKind,
    detect_challenge,
)

logger = logging.getLogger("gpt.auth")


class LoginError(ChatGPTWebError):
    """Base error for automated login failures."""


class InvalidCredentialsError(LoginError):
    """Raised when username or password is rejected by Auth0/OpenAI."""


class Invalid2FACodeError(LoginError):
    """Raised when 2FA TOTP code is rejected."""


class CaptchaChallengeError(ChallengeDetectedError, LoginError):
    """Raised when Cloudflare / Arkose CAPTCHA is detected during login."""


@dataclass
class LoginCredentials:
    username: str
    password: str
    totp_secret_or_code: str | None = None

    @classmethod
    def from_string(cls, cred_str: str) -> LoginCredentials:
        """Parse format 'username|password|2fa_secret_or_code' or 'username:password:2fa'."""
        parts = [p.strip() for p in re.split(r"[|:]", cred_str) if p.strip()]
        if len(parts) < 2:
            raise ValueError("Credentials string must have at least 'username|password'.")
        username = parts[0]
        password = parts[1]
        totp = parts[2] if len(parts) >= 3 else None
        return cls(username=username, password=password, totp_secret_or_code=totp)


class AutoLoginManager:
    """Automates zero-interaction login for ChatGPT Web using username|password|2fa."""

    # Candidate selectors for the landing-page "Log in" button. Tried in order;
    # each click must actually navigate to the auth page before we continue.
    _LOGIN_BUTTON_SELECTORS = (
        '[data-testid="login-button"]',
        'button[data-testid="login-button"]',
        'button[data-testid="welcome-login-button"]',
        'a[href*="/auth/login"]',
        'button:visible:has-text("Log in")',
        'a:visible:has-text("Log in")',
    )

    # Exclude third-party SSO buttons ("Continue with Google/Apple/Microsoft")
    # from every generic "Continue" match; clicking those redirects off the
    # OpenAI auth domain.
    _SSO_EXCLUSION = (
        ':not(:has-text("with Google"))'
        ':not(:has-text("with Apple"))'
        ':not(:has-text("with Microsoft"))'
    )

    _EMAIL_SUBMIT_SELECTORS = (
        'button[name="action"][value="default"]:visible',
        'button[type="submit"].btn-primary' + _SSO_EXCLUSION + ":visible",
        "button.btn-primary" + _SSO_EXCLUSION + ':visible:has-text("Continue")',
        'button[type="submit"]' + _SSO_EXCLUSION + ":visible",
        "button" + _SSO_EXCLUSION + ':visible:has-text("Continue")',
    )

    _PASSWORD_SUBMIT_SELECTORS = (
        'button[name="action"][value="default"]:visible',
        "button.btn-primary" + _SSO_EXCLUSION + ':visible:has-text("Continue")',
        'button[type="submit"]' + _SSO_EXCLUSION + ":visible",
        "button" + _SSO_EXCLUSION + ':visible:has-text("Continue")',
    )

    _VERIFY_SUBMIT_SELECTORS = (
        'button[name="action"][value="default"]:visible',
        'button[type="submit"]' + _SSO_EXCLUSION + ":visible",
        'button:visible:has-text("Verify")',
        "button" + _SSO_EXCLUSION + ':visible:has-text("Continue")',
    )

    # MFA challenge pages live under auth.openai.com/mfa-challenge/<id>.
    _MFA_URL_RE = re.compile(
        r"mfa-challenge|/mfa/|two-factor|two_factor|otp-challenge", re.IGNORECASE
    )

    _AUTH_ALLOWED_SUFFIXES = ("openai.com", "chatgpt.com")

    def __init__(
        self,
        profile_dir: Path | str = DEFAULT_PROFILE_DIR,
        headless: bool = True,
        cdp_url: str | None = None,
        executable_path: str | None = None,
    ):
        self.profile_dir = ensure_profile_dir(profile_dir)
        self.headless = headless
        self.cdp_url = cdp_url
        self.executable_path = executable_path
        # Multiplier applied to fixed inter-step settles; tests set this to 0.
        self.step_delay_scale = 1.0
        # Seconds to wait for the page to navigate after clicking "Log in".
        self.navigation_timeout = 20.0
        # Seconds to wait for the OTP input on an MFA challenge page.
        self.mfa_input_timeout = 60.0
        self.email_timeout = 10.0
        self.password_timeout = 35.0

    async def _pause(self, seconds: float) -> None:
        await asyncio.sleep(max(seconds, 0.0) * self.step_delay_scale)

    async def _open_cdp_page(self) -> tuple[Any, Page, Any | None]:
        """Attach to an already-running browser over CDP.

        Acquisition is guarded so a failure partway through (connect refused,
        unusable context/page) shuts the Playwright driver down instead of
        leaking it; once acquisition succeeds the caller's main ``finally``
        owns cleanup exactly as before.  The third element is the scratch
        context we created ourselves (or ``None`` when the page lives in a
        pre-existing user-owned context) — the caller must close it after
        login; a user-owned context must survive.
        """
        playwright_cm = async_playwright()
        playwright = await playwright_cm.start()
        created_context = False
        try:
            assert self.cdp_url is not None
            browser = await playwright.chromium.connect_over_cdp(self.cdp_url)
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = await browser.new_context()
                created_context = True
            page = context.pages[0] if context.pages else await context.new_page()
        except BaseException:
            # Close only the context we created ourselves (while the CDP
            # connection is still alive); a pre-existing, user-owned context
            # must survive, so for that branch we just disconnect below.
            if created_context:
                with suppress(Exception):
                    await context.close()
            # Disconnect without closing the user's own browser contexts.
            with suppress(Exception):
                await playwright_cm.__aexit__(None, None, None)
            raise
        return playwright_cm, page, (context if created_context else None)

    async def _open_cloak_context(self) -> tuple[Any, Page]:
        """Launch a CloakBrowser persistent context plus its working page.

        If the context launches but the page cannot be created, close the
        freshly launched context before propagating so the browser process
        never leaks outside the main try/finally.
        """
        context = await launch_persistent_context_async(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
        except BaseException:
            with suppress(Exception):
                await context.close()
            raise
        return context, page

    @staticmethod
    def generate_totp_code(totp_secret_or_code: str) -> str:
        """Compute 6-digit TOTP code if given a secret seed, or return clean 6-digit string."""
        return generate_totp_code(totp_secret_or_code)

    async def login(
        self,
        credentials: LoginCredentials,
        timeout_seconds: int = 120,
    ) -> bool:
        """Execute automated zero-interaction login workflow."""
        playwright_cm = None
        context = None
        browser_mgr: BrowserManager | None = None
        owns_page = True
        cdp_owned_context = None

        if self.cdp_url:
            logger.info(f"Connecting to existing browser via CDP: {self.cdp_url}...")
            playwright_cm, page, cdp_owned_context = await self._open_cdp_page()
            owns_page = False
        elif CLOAK_AVAILABLE:
            # CloakBrowser-first policy: this is the preferred launch backend
            # for automated logins (anti-fingerprint hardening).
            logger.info("Launching CloakBrowser for automated login...")
            context, page = await self._open_cloak_context()
        else:
            # Last resort: BrowserManager still tries CloakBrowser internally
            # before any vanilla Chromium fallback (which it logs loudly and
            # only permits behind WEBGPT_REQUIRE_CLOAKBROWSER=0).
            logger.warning(
                "cloakbrowser package unavailable; delegating to BrowserManager "
                "(CloakBrowser-first inside; vanilla Chromium only as an "
                "explicitly permitted last resort)."
            )
            browser_mgr = BrowserManager(
                profile_dir=self.profile_dir,
                headless=self.headless,
                persistent=True,
                executable_path=self.executable_path,
            )
            try:
                page = await browser_mgr.new_page()
            except BaseException:
                # new_page() runs before the main try/finally below; without
                # this guard a failed launch would leak the browser process.
                await browser_mgr.stop()
                raise
            if getattr(browser_mgr, "launch_backend", None) == "chromium-fallback":
                logger.warning(
                    "Automated login is running on an unhardened vanilla Chromium "
                    "fallback; Cloudflare challenges are likely."
                )

        try:
            logger.info("[Step 1] Navigating to https://chatgpt.com/...")
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=45000)
            await self._pause(2.0)

            if await self._has_security_challenge(page):
                raise CaptchaChallengeError(
                    "A security verification challenge is blocking automated login; "
                    "manual operator verification is required.",
                    kind=await self._challenge_kind(page),
                    url=page.url,
                )

            # Check if already truly authenticated
            if await self._is_authenticated_page(page):
                logger.info("[Auth Check] Already authenticated with active user profile.")
                return True

            # 1. Click Login Button if on landing page. Each candidate click must
            # actually navigate to the auth page; otherwise fall back to the next
            # selector instead of continuing the flow on the old page.
            before_login_url = page.url
            navigated_to_auth = False
            for login_selector in self._LOGIN_BUTTON_SELECTORS:
                login_loc = page.locator(login_selector)
                try:
                    if await login_loc.count() == 0:
                        continue
                    await login_loc.first.click(force=True)
                except Exception as exc:
                    logger.debug(f"[Step 2] Login selector '{login_selector}' not usable: {exc}")
                    continue
                logger.info(
                    "[Step 2] Clicked 'Log in'; waiting for navigation to the auth page..."
                )
                if await self._wait_for_navigation(page, before_login_url, self.navigation_timeout):
                    navigated_to_auth = True
                    break
                logger.warning(
                    f"[Step 2] Clicking '{login_selector}' did not navigate "
                    f"(still on: {page.url}); trying next selector."
                )
            if not navigated_to_auth:
                logger.warning(
                    "[Step 2] No 'Log in' selector produced a navigation; "
                    "continuing on the current page."
                )
            await self._pause(1.0)

            # 2. Email step (Auth0 / OpenAI ID)
            email_selectors = (
                'input[name="username"]:visible, '
                'input[type="email"]:visible, '
                'input#email:visible, '
                'input#username:visible, '
                'input[name="email"]:visible'
            )
            email_inp = page.locator(email_selectors).first
            try:
                email_visible = False
                deadline = asyncio.get_event_loop().time() + self.email_timeout
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        await email_inp.wait_for(state="visible", timeout=1000)
                        email_visible = True
                        break
                    except PlaywrightTimeoutError:
                        pass
                    await self._pause(0.5)

                if email_visible:
                    logger.info("[Step 3] Entering email/username...")
                    await email_inp.click(force=True)
                    await page.keyboard.type(credentials.username, delay=random.uniform(20, 40))
                    await self._pause(0.5)

                    await self._click_submit_button(page, self._EMAIL_SUBMIT_SELECTORS)
                    await self._pause(3.0)
            except LoginError:
                raise
            except Exception as e:
                logger.debug(f"Email step bypass or error: {e}")

            # 3. Password step. Security challenges are detected and surfaced;
            # they are never clicked or bypassed automatically.
            logger.info("[Step 4] Waiting for password input field...")
            pass_selectors = (
                'input[name="password"]:visible, '
                'input[name="current-password"]:visible, '
                'input[type="password"]:visible, '
                'input#password:visible'
            )
            pass_inp = page.locator(pass_selectors).first
            deadline = asyncio.get_event_loop().time() + self.password_timeout
            password_visible = False
            while asyncio.get_event_loop().time() < deadline:
                try:
                    await pass_inp.wait_for(state="visible", timeout=750)
                    password_visible = True
                    break
                except PlaywrightTimeoutError:
                    pass
                if await self._has_security_challenge(page):
                    raise CaptchaChallengeError(
                        "A security verification challenge is blocking automated login; "
                        "manual operator verification is required.",
                        kind=await self._challenge_kind(page),
                        url=page.url,
                    )
                await self._pause(0.5)
            if not password_visible:
                raise LoginError(
                    f"Password input field did not appear in time. URL: {page.url}"
                )

            logger.info("[Step 5] Entering password...")
            await pass_inp.click(force=True)
            await page.keyboard.type(credentials.password, delay=random.uniform(25, 45))
            await self._pause(0.5)

            submitted = await self._click_submit_button(page, self._PASSWORD_SUBMIT_SELECTORS)
            if not submitted:
                raise LoginError(
                    f"Could not find a usable submit button on the password step. URL: {page.url}"
                )
            await self._pause(4.0)

            # 4. Check for 2FA / MFA challenge.
            # NOTE: locator.is_visible() returns immediately (it does not wait),
            # which silently skipped MFA on slow-rendering challenge pages. We
            # poll with wait_for() instead, keyed off the URL pattern as well as
            # input visibility.
            logger.info("[Step 6] Checking for 2FA / MFA challenge...")
            code_selectors = (
                'input[name*="code"]:visible, '
                'input[name="code"]:visible, '
                'input[inputmode="numeric"]:visible, '
                'input[autocomplete="one-time-code"]:visible, '
                'input[name="totp"]:visible, '
                'input[type="text"]:visible'
            )
            code_inp = page.locator(code_selectors).first
            loop = asyncio.get_event_loop()
            hard_deadline = loop.time() + self.mfa_input_timeout
            grace_deadline = loop.time() + 10.0
            otp_ready = False
            seen_mfa_url = False
            while loop.time() < hard_deadline:
                if self._looks_like_mfa_page(page.url):
                    if not seen_mfa_url:
                        logger.info("[Step 6] MFA challenge page detected via URL.")
                    seen_mfa_url = True
                    grace_deadline = max(grace_deadline, loop.time() + 30.0)
                try:
                    await code_inp.wait_for(state="visible", timeout=1500)
                    otp_ready = True
                    break
                except PlaywrightTimeoutError:
                    pass

                if await self._has_security_challenge(page):
                    raise CaptchaChallengeError(
                        "A security verification challenge is blocking automated login; "
                        "manual operator verification is required.",
                        kind=await self._challenge_kind(page),
                        url=page.url,
                    )
                if loop.time() >= grace_deadline:
                    break
                await self._pause(0.25)

            if seen_mfa_url and not otp_ready and loop.time() >= hard_deadline:
                raise LoginError(
                    f"MFA challenge page detected but no OTP input appeared in time. "
                    f"URL: {page.url}"
                )

            if otp_ready:
                if not credentials.totp_secret_or_code:
                    raise Invalid2FACodeError("Account requires 2FA but no 2FA secret or code was provided.")

                totp_code = self.generate_totp_code(credentials.totp_secret_or_code)
                logger.info("Submitting computed 2FA TOTP code...")
                await code_inp.click(force=True)
                await page.keyboard.type(totp_code, delay=random.uniform(25, 50))
                await self._pause(0.5)

                submitted = await self._click_submit_button(page, self._VERIFY_SUBMIT_SELECTORS)
                if not submitted:
                    raise LoginError(
                        f"Could not find a usable button to submit the 2FA code. URL: {page.url}"
                    )
                await self._pause(5.0)

            # 5. Wait for landing on authenticated ChatGPT
            logger.info("[Step 7] Verifying landing on authenticated session...")
            await self._wait_for_landing(page, timeout_seconds=timeout_seconds)
            logger.info("[SUCCESS] Authenticated session established successfully!")
            return True

        finally:
            if browser_mgr is not None:
                await browser_mgr.stop()
            if owns_page:
                if context:
                    await context.close()
            elif playwright_cm:
                # A scratch CDP context we created must not linger in the
                # user's browser after login; a user-owned one must survive.
                if cdp_owned_context is not None:
                    with suppress(Exception):
                        await cdp_owned_context.close()
                await playwright_cm.__aexit__(None, None, None)

    async def _has_security_challenge(self, page: Page) -> bool:
        """Return True when an anti-bot / human-verification page is visible.

        Thin wrapper over the shared transport detector
        (``gpt.transport.challenge.detect_challenge``) so login and the HTTP
        transports classify challenges identically.  Detection is intentionally
        read-only: this helper never clicks, solves, retries identities, or
        otherwise attempts to bypass the challenge.
        """
        return await detect_challenge(page) is not ChallengeKind.NONE

    async def _challenge_kind(self, page: Page) -> ChallengeKind:
        """Shared classification used when a challenge must be reported."""
        return await detect_challenge(page)

    async def _wait_for_landing(self, page: Page, timeout_seconds: int = 60) -> None:
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            if await self._is_authenticated_page(page):
                return
            await asyncio.sleep(1.0)

        raise LoginError(f"Timed out waiting for authenticated page landing. URL: {page.url}")

    async def _is_authenticated_page(self, page: Page) -> bool:
        """Accurately detect if the page is authenticated with an active user profile."""
        url = page.url
        if "auth.openai.com" in url or "auth0" in url or "/auth/login" in url:
            return False

        # If Log in button is present, we are NOT authenticated
        login_btn = page.locator(
            '[data-testid="login-button"]:visible, '
            'a[href*="/auth/login"]:visible, '
            'button:visible:has-text("Log in"), '
            'a:visible:has-text("Log in")'
        )
        if await login_btn.count() > 0:
            return False

        # Indicators of an authenticated user session
        auth_indicators = [
            '[data-testid="accounts-profile-button"]:visible',
            'button[aria-label*="Open profile menu"]:visible',
            'button[aria-label*="Account menu"]:visible',
            'button[aria-label*="Profile menu"]:visible',
            'button[data-testid="profile-button"]:visible',
            'div[data-testid*="profile-button"]:visible',
        ]
        for ind in auth_indicators:
            if await page.locator(ind).count() > 0:
                return True

        # Check if composer is present without login button
        composer = page.locator('#prompt-textarea:visible, textarea:visible')
        return await composer.count() > 0

    @staticmethod
    def _looks_like_mfa_page(url: str) -> bool:
        """Detect MFA challenge pages via URL pattern (auth.openai.com/mfa-challenge/<id>)."""
        return bool(AutoLoginManager._MFA_URL_RE.search(url or ""))

    @classmethod
    def _is_allowed_auth_host(cls, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(
            host == suffix or host.endswith("." + suffix)
            for suffix in cls._AUTH_ALLOWED_SUFFIXES
        )

    @classmethod
    def _left_auth_domain(cls, before: str, after: str) -> bool:
        """True when a click moved us off the OpenAI auth domain (e.g. accounts.google.com)."""
        return cls._is_allowed_auth_host(before) and not cls._is_allowed_auth_host(after)

    async def _wait_for_navigation(
        self, page: Page, previous_url: str, timeout_seconds: float
    ) -> bool:
        """Poll until page.url changes away from previous_url, or the timeout elapses."""
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while True:
            if page.url != previous_url:
                return True
            if asyncio.get_event_loop().time() >= deadline:
                return False
            await self._pause(0.5)

    async def _click_submit_button(self, page: Page, selectors: tuple[str, ...]) -> bool:
        """Click the first usable Continue/submit button among ``selectors``.

        Buttons whose click redirects off the OpenAI auth domain (e.g.
        "Continue with Google") are rejected: we navigate back and try the next
        candidate element/selector instead. Returns True once a safe click lands.
        """
        before_url = page.url
        for selector in selectors:
            locator = page.locator(selector)
            try:
                total = await locator.count()
            except Exception as exc:
                logger.debug(f"[Submit] Selector '{selector}' not usable: {exc}")
                continue
            for index in range(total):
                button = locator.first if index == 0 else locator.nth(index)
                try:
                    await button.click(force=True)
                except Exception as exc:
                    logger.debug(f"[Submit] Click failed on '{selector}' [{index}]: {exc}")
                    continue
                await self._pause(3.0)
                if self._left_auth_domain(before_url, page.url):
                    logger.warning(
                        f"[Submit] Clicking '{selector}' [{index}] left the OpenAI auth "
                        f"domain (now: {page.url}); trying the next candidate."
                    )
                    try:
                        await page.go_back(wait_until="domcontentloaded")
                    except Exception as exc:
                        logger.debug(f"[Submit] go_back after redirect failed: {exc}")
                    await self._pause(1.0)
                    continue
                return True
        return False
