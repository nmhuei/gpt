from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

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

logger = logging.getLogger("gpt.auth")


class LoginError(ChatGPTWebError):
    """Base error for automated login failures."""


class InvalidCredentialsError(LoginError):
    """Raised when username or password is rejected by Auth0/OpenAI."""


class Invalid2FACodeError(LoginError):
    """Raised when 2FA TOTP code is rejected."""


class CaptchaChallengeError(LoginError):
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

    def __init__(
        self,
        profile_dir: Path | str = DEFAULT_PROFILE_DIR,
        headless: bool = True,
        cdp_url: str | None = None,
    ):
        self.profile_dir = ensure_profile_dir(profile_dir)
        self.headless = headless
        self.cdp_url = cdp_url

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
        browser = None
        context = None
        owns_page = True

        if self.cdp_url:
            logger.info(f"Connecting to existing browser via CDP: {self.cdp_url}...")
            playwright_cm = async_playwright()
            playwright = await playwright_cm.start()
            browser = await playwright.chromium.connect_over_cdp(self.cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
            owns_page = False
        elif CLOAK_AVAILABLE:
            profile_path = str(self.profile_dir)
            context = await launch_persistent_context_async(
                user_data_dir=profile_path,
                headless=self.headless,
            )
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            browser_mgr = BrowserManager(
                profile_dir=self.profile_dir,
                headless=self.headless,
                persistent=True,
            )
            page = await browser_mgr.new_page()

        try:
            logger.info("[Step 1] Navigating to https://chatgpt.com/...")
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2.0)

            # Check if already truly authenticated
            if await self._is_authenticated_page(page):
                logger.info("[Auth Check] Already authenticated with active user profile.")
                return True

            # 1. Click Login Button if on landing page
            login_btns = page.locator(
                '[data-testid="login-button"], '
                'button[data-testid="login-button"], '
                'button[data-testid="welcome-login-button"], '
                'a[href*="/auth/login"], '
                'button:visible:has-text("Log in"), '
                'a:visible:has-text("Log in")'
            )
            if await login_btns.count() > 0:
                logger.info("[Step 2] Clicking 'Log in' button to reach login page...")
                await login_btns.first.click(force=True)
                await asyncio.sleep(3.0)

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
                if await email_inp.is_visible(timeout=5000):
                    logger.info("[Step 3] Entering email/username...")
                    await email_inp.click(force=True)
                    await page.keyboard.type(credentials.username, delay=random.uniform(20, 40))
                    await asyncio.sleep(0.5)

                    submit_btn = page.locator(
                        'button[type="submit"].btn-primary:visible, '
                        'button[name="action"][value="default"]:visible, '
                        'button.btn-primary:visible:has-text("Continue"), '
                        'button:visible:has-text("Continue"), '
                        'button[type="submit"]:visible'
                    ).first
                    await submit_btn.click(force=True)
                    await asyncio.sleep(3.0)
            except Exception as e:
                logger.debug(f"Email step bypass or error: {e}")

            # 3. Password step
            logger.info("[Step 4] Waiting for password input field...")
            pass_selectors = (
                'input[name="password"]:visible, '
                'input[name="current-password"]:visible, '
                'input[type="password"]:visible, '
                'input#password:visible'
            )
            pass_inp = page.locator(pass_selectors).first
            try:
                await pass_inp.wait_for(state="visible", timeout=25000)
            except PlaywrightTimeoutError:
                # Check for Turnstile / Challenge
                page_text = await page.content()
                if "Just a moment" in page_text or "turnstile" in page_text.lower() or "challenge" in page_text.lower():
                    raise CaptchaChallengeError("Cloudflare Turnstile challenge detected on login page.") from None
                raise LoginError("Password input field did not appear in time.") from None

            logger.info("[Step 5] Entering password...")
            await pass_inp.click(force=True)
            await page.keyboard.type(credentials.password, delay=random.uniform(25, 45))
            await asyncio.sleep(0.5)

            pass_cont_btn = page.locator(
                'button[name="action"][value="default"]:visible, '
                'button:visible:has-text("Continue"), '
                'button[type="submit"]:visible'
            ).first
            await pass_cont_btn.click(force=True)
            await asyncio.sleep(4.0)

            # 4. Check for 2FA / MFA challenge
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
            try:
                if await code_inp.is_visible(timeout=8000) or credentials.totp_secret_or_code:
                    if not credentials.totp_secret_or_code:
                        raise Invalid2FACodeError("Account requires 2FA but no 2FA secret or code was provided.")

                    totp_code = self.generate_totp_code(credentials.totp_secret_or_code)
                    logger.info(f"Submitting computed 2FA TOTP code ({totp_code})...")
                    await code_inp.click(force=True)
                    await page.keyboard.type(totp_code, delay=random.uniform(25, 50))
                    await asyncio.sleep(0.5)

                    verify_btn = page.locator(
                        'button:visible:has-text("Continue"), '
                        'button:visible:has-text("Verify"), '
                        'button[type="submit"]:visible'
                    ).first
                    await verify_btn.click(force=True)
                    await asyncio.sleep(5.0)
            except PlaywrightTimeoutError:
                pass

            # 5. Wait for landing on authenticated ChatGPT
            logger.info("[Step 7] Verifying landing on authenticated session...")
            await self._wait_for_landing(page, timeout_seconds=timeout_seconds)
            logger.info("[SUCCESS] Authenticated session established successfully!")
            return True

        finally:
            if owns_page:
                if context:
                    await context.close()
            elif playwright_cm:
                await playwright_cm.__aexit__(None, None, None)

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
