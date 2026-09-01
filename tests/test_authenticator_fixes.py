"""Regression tests for the live-probe-2 login fixes (docs/reports/live-sse-probe-2-2026-08-24.md).

Covers:
(a) "Continue with Google" SSO buttons are excluded and a real submit button wins;
    a click that leaves the OpenAI auth domain is rejected and the next candidate tried.
(b) A landing-page "Log in" click that does not navigate falls back to the next
    selector instead of continuing the flow on the old page.
(c) An MFA challenge page that renders late (URL pattern mfa-challenge) still
    gets the TOTP code typed and submitted.
(d) Security-challenge detection stays strictly read-only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import gpt.auth.authenticator as authenticator_module
from gpt.auth.authenticator import AutoLoginManager, LoginCredentials

CHATGPT_URL = "https://chatgpt.com/"
EMAIL_URL = "https://auth.openai.com/logon?screen=email"
PASSWORD_URL = "https://auth.openai.com/logon?screen=password"
MFA_URL = "https://auth.openai.com/mfa-challenge/abc123"
GOOGLE_URL = "https://accounts.google.com/v3/signin/identifier"

EMAIL_INPUT_KEY = 'input[name="username"]'
PASSWORD_INPUT_KEY = 'input[name="password"]'
CODE_INPUT_KEY = 'input[inputmode="numeric"]'


class FakeKeyboard:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def type(self, text: str, delay: float = 0.0) -> None:
        self._page.typed.append(text)


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self._page = page
        self.selector = selector
        self.first = self

    async def count(self) -> int:
        return self._page.count_for(self.selector)

    async def wait_for(self, state: str | None = None, timeout: float | None = None) -> None:
        for key, script in self._page.wait_scripts.items():
            if key in self.selector and script:
                action = script.pop(0)
                if action == "timeout":
                    raise PlaywrightTimeoutError(f"{self.selector} not visible yet")
                break

    async def click(self, force: bool = False) -> None:
        self._page.click_log.append(self.selector)
        if self._page.on_click is not None:
            self._page.on_click(self.selector)

    async def inner_text(self, timeout: float | None = None) -> str:
        return ""


class FakePage:
    """Minimal fake of a Playwright page driven by per-test rules."""

    def __init__(self) -> None:
        self._url = CHATGPT_URL
        self.history: list[str] = [CHATGPT_URL]
        self.authenticated = False
        self.click_log: list[str] = []
        self.typed: list[str] = []
        # selector-substring -> list of actions ("timeout" raises, anything else succeeds)
        self.wait_scripts: dict[str, list[str | None]] = {}
        self.on_click = None
        self.go_back_calls = 0
        self.keyboard = FakeKeyboard(self)

    @property
    def url(self) -> str:
        return self._url

    @url.setter
    def url(self, value: str) -> None:
        self._url = value
        self.history.append(value)

    async def goto(self, url: str, **kwargs) -> None:
        self.url = url

    async def title(self) -> str:
        return "ChatGPT"

    async def go_back(self, **kwargs) -> None:
        self.go_back_calls += 1

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def count_for(self, selector: str) -> int:
        challenge_markers = (
            "challenges.cloudflare.com",
            "cf-turnstile",
            "challenge-stage",
            "cf-chl-",
        )
        if any(marker in selector for marker in challenge_markers):
            return 0
        if self.authenticated:
            # Submit-button candidates stay clickable post-login (the verify
            # step may run after authentication is already staged by the hook).
            submit_markers = (
                'name="action"',
                'type="submit"',
                'has-text("Continue")',
                'has-text("Verify")',
            )
            if any(marker in selector for marker in submit_markers):
                return 1
            login_markers = (
                "login-button",
                "/auth/login",
                "Log in",
                "profile-button",
                "Profile menu",
                "Account menu",
            )
            if any(marker in selector for marker in login_markers):
                return 0
            if "prompt-textarea" in selector or "textarea" in selector:
                return 1
            return 0
        # Pre-auth: every form element / button candidate is present.
        return 1


def make_manager(tmp_path) -> AutoLoginManager:
    manager = AutoLoginManager(profile_dir=tmp_path / "profile", headless=True)
    manager.step_delay_scale = 0.0
    manager.navigation_timeout = 0.05
    manager.email_timeout = 1.0
    manager.password_timeout = 1.0
    manager.mfa_input_timeout = 5.0
    return manager


@pytest.fixture
def install_fake_browser(monkeypatch):
    """Force the BrowserManager branch of login() and hand back the fake page."""

    def _install(page: FakePage) -> FakePage:
        monkeypatch.setattr(authenticator_module, "CLOAK_AVAILABLE", False)

        managers: list[_FakeBrowserManager] = []

        class _FakeBrowserManager:
            def __init__(self, **kwargs) -> None:
                self.stop_calls = 0
                managers.append(self)

            async def new_page(self) -> FakePage:
                return page

            async def stop(self) -> None:
                self.stop_calls += 1

        monkeypatch.setattr(authenticator_module, "BrowserManager", _FakeBrowserManager)
        # Exposed for cleanup assertions: login() must create exactly one
        # manager and always stop it (the leak-guard finally).
        page.browser_managers = managers  # type: ignore[attr-defined]
        return page

    return _install


def _default_navigation_hook(page: FakePage):
    """Standard click script: dead landing buttons until the anchor link navigates,
    email submit moves to password, password submit finishes the (non-MFA) login."""

    def on_click(selector: str) -> None:
        if selector == 'a[href*="/auth/login"]':
            page.url = EMAIL_URL
        elif selector == 'button[name="action"][value="default"]:visible':
            if page.url == EMAIL_URL:
                page.url = PASSWORD_URL
            elif page.url == PASSWORD_URL:
                page.authenticated = True
                page.url = CHATGPT_URL

    return on_click


# ---------------------------------------------------------------------------
# (a) SSO exclusion + off-domain rejection in _click_submit_button
# ---------------------------------------------------------------------------


async def test_continue_with_google_rejected_and_submit_button_used(tmp_path):
    manager = make_manager(tmp_path)
    page = FakePage()
    page.url = EMAIL_URL

    # On this page only two candidates match: the generic Continue match actually
    # resolves to "Continue with Google", while the type="submit" match is safe.
    original_count_for = page.count_for

    def count_for(selector: str) -> int:
        if 'name="action"' in selector or "btn-primary" in selector:
            return 0
        if "Continue" in selector and 'type="submit"' not in selector:
            return 1  # the Google button
        if 'type="submit"' in selector:
            return 1  # the real submit button
        return original_count_for(selector)

    page.count_for = count_for

    def on_click(selector: str) -> None:
        if "Continue" in selector and 'type="submit"' not in selector:
            page.url = GOOGLE_URL  # clicking the SSO button redirects away

    page.on_click = on_click

    submitted = await manager._click_submit_button(page, manager._EMAIL_SUBMIT_SELECTORS)

    assert submitted is True
    # The generic Continue (Google) button must never have been clicked: the
    # prioritised type="submit" candidate comes first among matching selectors.
    assert page.click_log == [
        'button[type="submit"]'
        + ':not(:has-text("with Google"))'
        + ':not(:has-text("with Apple"))'
        + ':not(:has-text("with Microsoft"))'
        + ":visible"
    ]
    assert not page.url.startswith("https://accounts.google.com")


async def test_off_domain_redirect_is_rolled_back_and_next_candidate_tried(tmp_path):
    manager = make_manager(tmp_path)
    page = FakePage()
    page.url = EMAIL_URL

    original_count_for = page.count_for

    def count_for(selector: str) -> int:
        if 'name="action"' in selector or "btn-primary" in selector:
            return 0
        if 'type="submit"' in selector:
            return 0  # no safe submit present this time
        if "Continue" in selector:
            return 1  # only the Google-matching generic Continue
        return original_count_for(selector)

    page.count_for = count_for

    def on_click(selector: str) -> None:
        if "Continue" in selector:
            page.url = GOOGLE_URL

    page.on_click = on_click

    submitted = await manager._click_submit_button(page, manager._EMAIL_SUBMIT_SELECTORS)

    assert submitted is False
    # The dangerous click happened once, was detected, and we navigated back.
    assert len(page.click_log) == 1
    assert page.go_back_calls == 1
    assert page.url == GOOGLE_URL  # go_back is faked as a counter only; URL stayed recorded


def test_submit_selector_tuples_exclude_sso_and_prioritise_submit():
    for selectors in (
        AutoLoginManager._EMAIL_SUBMIT_SELECTORS,
        AutoLoginManager._PASSWORD_SUBMIT_SELECTORS,
        AutoLoginManager._VERIFY_SUBMIT_SELECTORS,
    ):
        joined = "\n".join(selectors)
        assert 'has-text("with Google")' in joined
        assert 'has-text("with Apple")' in joined
        assert 'has-text("with Microsoft")' in joined
        bare_continue_positions = [
            i for i, sel in enumerate(selectors) if 'has-text("Continue")' in sel
        ]
        submit_positions = [i for i, sel in enumerate(selectors) if 'type="submit"' in sel]
        assert bare_continue_positions, "generic Continue fallback expected"
        # Every bare "Continue" fallback must carry the SSO exclusion...
        for i in bare_continue_positions:
            assert "with Google" in selectors[i]
        # ...and a type="submit" candidate exists before the last bare Continue.
        assert submit_positions and min(submit_positions) < max(bare_continue_positions)


# ---------------------------------------------------------------------------
# (b) Dead landing-page click falls back to the next selector
# ---------------------------------------------------------------------------


async def test_dead_login_button_click_falls_back_to_next_selector(
    tmp_path, install_fake_browser
):
    manager = make_manager(tmp_path)
    page = install_fake_browser(FakePage())
    page.on_click = _default_navigation_hook(page)

    result = await manager.login(
        LoginCredentials(username="u@example.com", password="pw", totp_secret_or_code="654321"),
        timeout_seconds=2,
    )

    assert result is True
    # The data-testid click was attempted but did not navigate...
    assert '[data-testid="login-button"]' in page.click_log
    # ...so the flow fell back to the anchor selector which did navigate.
    assert 'a[href*="/auth/login"]' in page.click_log
    email_click_positions = [
        i for i, sel in enumerate(page.click_log) if EMAIL_INPUT_KEY in sel
    ]
    assert email_click_positions and page.click_log.index(
        'a[href*="/auth/login"]'
    ) < email_click_positions[0]
    # The rest of the flow ran on the auth page, not the old landing page.
    assert EMAIL_URL in page.history
    assert "u@example.com" in page.typed
    assert page.history[-1] == CHATGPT_URL
    assert page.authenticated
    # The leak-guard finally must have stopped the browser exactly once.
    assert len(page.browser_managers) == 1
    assert page.browser_managers[0].stop_calls == 1


# ---------------------------------------------------------------------------
# (c) Late-rendering MFA page still gets the TOTP filled
# ---------------------------------------------------------------------------


async def test_late_mfa_challenge_still_fills_totp(tmp_path, install_fake_browser):
    manager = make_manager(tmp_path)
    page = install_fake_browser(FakePage())

    def on_click(selector: str) -> None:
        if selector == 'a[href*="/auth/login"]':
            page.url = EMAIL_URL
        elif selector == 'button[name="action"][value="default"]:visible':
            if page.url == EMAIL_URL:
                page.url = PASSWORD_URL
            elif page.url == PASSWORD_URL:
                page.url = MFA_URL  # password submit lands on the MFA challenge
            elif page.url.startswith("https://auth.openai.com/mfa-challenge"):
                page.authenticated = True
                page.url = CHATGPT_URL

    page.on_click = on_click
    # The OTP input mounts late: two polls time out before it appears.
    page.wait_scripts[CODE_INPUT_KEY] = ["timeout", "timeout", None]

    result = await manager.login(
        LoginCredentials(username="u@example.com", password="pw", totp_secret_or_code="654321"),
        timeout_seconds=2,
    )

    assert result is True
    assert MFA_URL in page.history
    assert "654321" in page.typed
    assert page.history[-1] == CHATGPT_URL
    assert page.authenticated
    # The leak-guard finally must have stopped the browser exactly once.
    assert len(page.browser_managers) == 1
    assert page.browser_managers[0].stop_calls == 1


def test_mfa_url_pattern_detection():
    assert AutoLoginManager._looks_like_mfa_page(MFA_URL)
    assert AutoLoginManager._looks_like_mfa_page("https://auth.openai.com/mfa/xyz")
    assert not AutoLoginManager._looks_like_mfa_page(EMAIL_URL)
    assert not AutoLoginManager._looks_like_mfa_page(CHATGPT_URL)


# ---------------------------------------------------------------------------
# (d) Challenge detection remains read-only
# ---------------------------------------------------------------------------


async def test_security_challenge_detection_is_read_only(tmp_path):
    manager = make_manager(tmp_path)

    page = MagicMock()
    challenge_locator = MagicMock()
    challenge_locator.count = AsyncMock(return_value=1)
    page.locator = MagicMock(return_value=challenge_locator)
    page.title = AsyncMock(return_value="Just a moment...")
    page.mouse = MagicMock()
    page.keyboard = MagicMock()

    assert await manager._has_security_challenge(page) is True
    assert not page.mouse.method_calls
    assert not page.keyboard.method_calls
    challenge_locator.click.assert_not_called()
