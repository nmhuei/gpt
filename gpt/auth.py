from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import random
import re
from typing import Optional
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError
import pyotp

try:
    from cloakbrowser import launch_persistent_context_async
    CLOAK_AVAILABLE = True
except ImportError:
    CLOAK_AVAILABLE = False

from gpt.browser import BrowserManager
from gpt.profile import DEFAULT_PROFILE_DIR, ensure_profile_dir
from gpt.state import AuthRequired, ChatGPTWebError

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
    totp_secret_or_code: Optional[str] = None

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

    def __init__(self, profile_dir: Path | str = DEFAULT_PROFILE_DIR, headless: bool = True):
        self.profile_dir = ensure_profile_dir(profile_dir)
        self.headless = headless

    @staticmethod
    def generate_totp_code(totp_secret_or_code: str) -> str:
        """Compute 6-digit TOTP code if given a secret seed, or return clean 6-digit string."""
        cleaned = totp_secret_or_code.replace(" ", "").strip()
        if cleaned.isdigit() and len(cleaned) in (6, 8):
            return cleaned

        try:
            totp = pyotp.TOTP(cleaned)
            return totp.now()
        except Exception:
            try:
                padded = cleaned + "=" * ((8 - len(cleaned) % 8) % 8)
                totp = pyotp.TOTP(padded)
                return totp.now()
            except Exception as exc:
                raise ValueError(f"Invalid 2FA TOTP secret key: {exc}")

    async def login(
        self,
        credentials: LoginCredentials,
        timeout_seconds: int = 120,
    ) -> bool:
        """Execute automated zero-interaction login workflow."""
        profile_path = str(self.profile_dir)

        if CLOAK_AVAILABLE:
            context = await launch_persistent_context_async(
                user_data_dir=profile_path,
                headless=self.headless,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            close_func = context.close
        else:
            browser_mgr = BrowserManager(
                profile_dir=self.profile_dir,
                headless=self.headless,
                persistent=True,
            )
            page = await browser_mgr.new_page()
            close_func = browser_mgr.stop

        try:
            logger.info("Navigating to https://chatgpt.com/...")
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2.0)

            # Check if already logged in
            if await self._is_authenticated_page(page):
                logger.info("Already authenticated in current profile.")
                return True

            # 1. Click Login Button if present
            login_btns = page.locator(
                'button.btn-secondary:visible:has-text("Log in"), '
                'button.btn-primary:visible:has-text("Log in"), '
                'button:visible:text-is("Log in"), '
                'a:visible:has-text("Log in")'
            )
            if await login_btns.count() > 0:
                logger.info("Clicking landing Log in button...")
                await login_btns.first.click(force=True)
                await asyncio.sleep(2.0)

            # 2. Email step
            email_inp = page.locator(
                'input[type="email"]:visible, input#email:visible, input[name="email"]:visible'
            ).first
            if await email_inp.is_visible():
                logger.info("Entering username/email...")
                await email_inp.click(force=True)
                await page.keyboard.type(credentials.username, delay=random.uniform(20, 40))
                await asyncio.sleep(0.5)

                submit_btn = page.locator(
                    'button[type="submit"].btn-primary:visible, '
                    'button.btn-primary:visible:has-text("Continue"), '
                    'button[type="submit"]:visible'
                ).first
                await submit_btn.click(force=True)
                await asyncio.sleep(3.0)

            # 3. Password step
            logger.info("Waiting for password input field...")
            pass_inp = page.locator(
                'input[name="current-password"]:visible, input[type="password"]:visible'
            ).first
            await pass_inp.wait_for(state="visible", timeout=20000)

            logger.info("Entering password...")
            await pass_inp.click(force=True)
            await page.keyboard.type(credentials.password, delay=random.uniform(25, 45))
            await asyncio.sleep(0.5)

            pass_cont_btn = page.locator(
                'button:visible:has-text("Continue"), button[type="submit"]:visible'
            ).first
            await pass_cont_btn.click(force=True)
            await asyncio.sleep(4.0)

            # 4. Check for 2FA / MFA challenge
            if "mfa" in page.url.lower() or "challenge" in page.url.lower() or credentials.totp_secret_or_code:
                code_inp = page.locator(
                    'input[name*="code"]:visible, '
                    'input[name="code"]:visible, '
                    'input[inputmode="numeric"]:visible, '
                    'input[type="text"]:visible'
                ).first
                try:
                    await code_inp.wait_for(state="visible", timeout=12000)
                    if not credentials.totp_secret_or_code:
                        raise Invalid2FACodeError("Account requires 2FA but no 2FA secret or code was provided.")

                    totp_code = self.generate_totp_code(credentials.totp_secret_or_code)
                    logger.info(f"Submitting 2FA TOTP code ({totp_code})...")
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
            await self._wait_for_landing(page, timeout_seconds=timeout_seconds)
            logger.info("Login succeeded! Authenticated session established.")
            return True

        finally:
            await close_func()

    async def _wait_for_landing(self, page: Page, timeout_seconds: int = 60) -> None:
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            if await self._is_authenticated_page(page):
                return
            await asyncio.sleep(1.0)

        raise LoginError(f"Timed out waiting for authenticated page landing. URL: {page.url}")

    async def _is_authenticated_page(self, page: Page) -> bool:
        url = page.url
        if "auth.openai.com" in url or "auth0" in url or "/auth/login" in url:
            return False

        indicators = [
            '#prompt-textarea',
            'textarea',
            'div[data-testid*="profile-button"]',
            'button[aria-label*="User menu"]',
            'a[href="/"]',
        ]
        for ind in indicators:
            if await page.locator(ind).first.is_visible():
                return True
        return False
