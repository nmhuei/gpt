"""Cloudflare-resilience regression tests (prevention-first strategy).

Covers:
(a) Every browser-launch path prefers CloakBrowser; the vanilla Chromium
    fallback requires an explicit operator opt-in and warns loudly.
(b) The shared challenge detector classifies {none, cloudflare_challenge,
    turnstile} correctly from fixture HTML / page doubles.
(c) A 403 + challenge-page response triggers exactly one real-browser
    credential re-mint plus one retry, then raises a typed error.
(d) Fingerprint consistency: the transport impersonates the exact minting
    browser version (chrome146) and sends its captured User-Agent.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

import gpt.auth.authenticator as authenticator_module
import gpt.transport.browser as browser_module
from gpt.auth.authenticator import AutoLoginManager, LoginCredentials
from gpt.state import AuthRequired, BrowserDisconnected, ProtocolChanged, RateLimited
from gpt.transport.browser import BrowserManager
from gpt.transport.challenge import (
    ChallengeDetectedError,
    ChallengeKind,
    LimitSignal,
    classify_http_challenge,
    classify_limit_signal,
    detect_challenge,
)
from gpt.transport.curl_transport import (
    CLOAKBROWSER_USER_AGENT,
    IMPERSONATE_TARGET,
    CurlCffiTransport,
)
from gpt.transport.token_manager import SentinelTokens, TokenBundle
from gpt.types import ModelInfo, SendRequest

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class FakeCloakContext:
    def __init__(self) -> None:
        self.pages: list = []
        self.new_page = AsyncMock(return_value=MagicMock(name="page"))
        self.close = AsyncMock()


class FakeChromium:
    def __init__(self) -> None:
        self.launch = AsyncMock(return_value=MagicMock(name="browser"))
        self.launch_persistent_context = AsyncMock(return_value=FakeCloakContext())

    async def connect_over_cdp(self, url: str):
        raise AssertionError("CDP was not expected in this test")


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakePlaywrightCM:
    def __init__(self) -> None:
        self._pw = FakePlaywright()

    async def start(self) -> FakePlaywright:
        return self._pw

    async def __aexit__(self, *exc) -> None:
        pass


class StopNavigation(Exception):
    """Sentinel used to cut a login flow short once the launch choice is made."""


# ---------------------------------------------------------------------------
# (a) CloakBrowser-first launch preference
# ---------------------------------------------------------------------------


async def test_login_prefers_cloakbrowser_over_browser_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(authenticator_module, "CLOAK_AVAILABLE", True)

    launched = AsyncMock(return_value=FakeCloakContext())
    monkeypatch.setattr(authenticator_module, "launch_persistent_context_async", launched)

    def _forbidden(*args, **kwargs):
        raise AssertionError("BrowserManager must not be used while cloakbrowser exists")

    monkeypatch.setattr(authenticator_module, "BrowserManager", _forbidden)

    class DyingPage:
        url = ""

        async def goto(self, *args, **kwargs):
            raise StopNavigation()

    context = FakeCloakContext()
    context.new_page = AsyncMock(return_value=DyingPage())
    launched.return_value = context

    manager = AutoLoginManager(profile_dir=tmp_path / "profile", headless=True)
    with pytest.raises(StopNavigation):
        await manager.login(LoginCredentials("u@example.com", "pw"), timeout_seconds=1)

    launched.assert_awaited_once()


async def test_browser_manager_start_prefers_cloakbrowser(monkeypatch):
    import cloakbrowser

    launched = AsyncMock(return_value=FakeCloakContext())
    monkeypatch.setattr(cloakbrowser, "launch_persistent_context_async", launched)
    monkeypatch.setattr(browser_module, "async_playwright", lambda: FakePlaywrightCM())

    manager = BrowserManager(persistent=True, profile_dir="/tmp/cf-profile")
    context = await manager.start()

    launched.assert_awaited_once()
    assert context is launched.return_value
    assert manager.launch_backend == "cloakbrowser"
    await manager.stop()


async def test_vanilla_chromium_fallback_is_refused_by_default(monkeypatch, tmp_path):
    # Simulate a broken/absent cloakbrowser install.
    monkeypatch.setitem(sys.modules, "cloakbrowser", None)
    monkeypatch.delenv("WEBGPT_REQUIRE_CLOAKBROWSER", raising=False)

    manager = BrowserManager(persistent=True, profile_dir=tmp_path / "profile")
    with pytest.raises(BrowserDisconnected, match="CloakBrowser"):
        await manager.start()


async def test_vanilla_chromium_fallback_warns_loudly_when_opted_in(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setitem(sys.modules, "cloakbrowser", None)
    monkeypatch.setenv("WEBGPT_REQUIRE_CLOAKBROWSER", "0")
    monkeypatch.setattr(browser_module, "async_playwright", lambda: FakePlaywrightCM())

    manager = BrowserManager(persistent=True, profile_dir=tmp_path / "profile")
    with caplog.at_level("WARNING", logger="gpt.transport.browser"):
        await manager.start()

    assert manager.launch_backend == "chromium-fallback"
    assert any("WITHOUT anti-fingerprint" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# (b) Shared challenge classification from fixture HTML / page doubles
# ---------------------------------------------------------------------------


class FakeLocator:
    def __init__(self, page: FakeChallengePage, selector: str) -> None:
        self._page = page
        self._selector = selector

    async def count(self) -> int:
        parts = [part.strip() for part in self._selector.split(",")]
        return 1 if any(part in self._page.selectors_hit for part in parts) else 0

    async def inner_text(self, timeout: float | None = None) -> str:
        return self._page.body_text


class FakeChallengePage:
    def __init__(
        self,
        *,
        selectors_hit: tuple[str, ...] = (),
        title: str = "ChatGPT",
        body_text: str = "",
    ) -> None:
        self.selectors_hit = set(selectors_hit)
        self.title_text = title
        self.body_text = body_text

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def title(self) -> str:
        return self.title_text


TURNSTILE_FIXTURE = FakeChallengePage(
    selectors_hit=("#cf-turnstile",),
    body_text="<widget placeholder>",
)
CF_CHALLENGE_FIXTURE = FakeChallengePage(
    selectors_hit=('iframe[src*="challenges.cloudflare.com"]',),
)
TITLE_FIXTURE = FakeChallengePage(title="Just a moment...")
BODY_FIXTURE = FakeChallengePage(body_text="Please verify you are human to continue.")
CLEAN_FIXTURE = FakeChallengePage(body_text="How can I help you today?")


async def test_detector_classifies_turnstile_fixture():
    assert await detect_challenge(TURNSTILE_FIXTURE) is ChallengeKind.TURNSTILE


async def test_detector_classifies_cloudflare_challenge_fixtures():
    assert await detect_challenge(CF_CHALLENGE_FIXTURE) is ChallengeKind.CLOUDFLARE_CHALLENGE
    assert await detect_challenge(TITLE_FIXTURE) is ChallengeKind.CLOUDFLARE_CHALLENGE
    assert await detect_challenge(BODY_FIXTURE) is ChallengeKind.CLOUDFLARE_CHALLENGE


async def test_detector_returns_none_for_clean_page():
    assert await detect_challenge(CLEAN_FIXTURE) is ChallengeKind.NONE


def test_http_classifier_flags_only_real_challenge_bodies():
    just_a_moment = (
        "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
        '<body><div id="challenge-stage">Loading</div>'
        '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script></body></html>'
    )
    assert (
        classify_http_challenge(403, just_a_moment) is ChallengeKind.CLOUDFLARE_CHALLENGE
    )
    assert (
        classify_http_challenge(503, "<div id='cf-turnstile'></div>")
        is ChallengeKind.TURNSTILE
    )
    # API-style errors must stay on their existing handling paths.
    assert classify_http_challenge(403, '{"detail": "Unusual activity"}') is ChallengeKind.NONE
    assert classify_http_challenge(200, just_a_moment) is ChallengeKind.NONE
    assert classify_http_challenge(403, "") is ChallengeKind.NONE


async def test_authenticator_detection_delegates_to_shared_helper():
    manager = AutoLoginManager(profile_dir="/tmp/cf-profile")
    assert await manager._has_security_challenge(TURNSTILE_FIXTURE) is True
    assert await manager._has_security_challenge(CLEAN_FIXTURE) is False


# ---------------------------------------------------------------------------
# (c) 403 + challenge page -> re-mint via real browser, retry once, typed raise
# ---------------------------------------------------------------------------

CHALLENGE_HTML = (
    b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    b'<body>Attention Required</body></html>'
)


def _bundle() -> TokenBundle:
    return TokenBundle(
        access_token="access-token",
        cookies={"cf_clearance": "fresh-clearance"},
        cf_clearance="fresh-clearance",
        oai_device_id="device-id",
    )


class StubTokenManager:
    def __init__(self) -> None:
        self.extract_calls = 0
        self.invalidate_calls = 0

    async def refresh_if_needed(self) -> TokenBundle:
        return _bundle()

    async def get_sentinel_tokens(self, conversation_id) -> SentinelTokens:
        return SentinelTokens("requirements", "proof", "turnstile")

    def invalidate_sentinel(self) -> None:
        self.invalidate_calls += 1

    async def extract_all(self) -> TokenBundle:
        self.extract_calls += 1
        return _bundle()


class ChallengeResponse:
    status_code = 403

    def __init__(self) -> None:
        self.closed = False

    async def aiter_bytes(self):
        yield CHALLENGE_HTML

    async def aclose(self) -> None:
        self.closed = True


class OkSseResponse:
    status_code = 200

    def __init__(self) -> None:
        self.closed = False

    async def aiter_bytes(self):
        yield b'data: {"conversation_id":"conv-1","message":{"id":"turn-1","content":{"parts":["Hel"]}}}\n\n'
        yield b'data: {"message":{"content":{"parts":["Hello"]},"status":"finished_successfully"}}\n\n'

    async def aclose(self) -> None:
        self.closed = True


class ScriptedSession:
    """Returns queued responses in order and records every POST."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.posts = 0

    async def post(self, *args, **kwargs):
        self.posts += 1
        return self.responses.pop(0)


def _request() -> SendRequest:
    return SendRequest(
        text="Hello",
        conversation_id="conv-1",
        model=ModelInfo(id="gpt-test", label="GPT Test"),
    )


@pytest.mark.anyio
async def test_challenge_response_remints_once_then_succeeds():
    tokens = StubTokenManager()
    session = ScriptedSession([ChallengeResponse(), OkSseResponse()])
    transport = CurlCffiTransport(tokens, session=session)

    result = await transport.send(_request())

    assert result.text == "Hello"
    assert session.posts == 2
    assert tokens.extract_calls == 1  # exactly one real-browser re-mint
    assert tokens.invalidate_calls >= 1


@pytest.mark.anyio
async def test_persistent_challenge_raises_typed_error_after_single_retry():
    tokens = StubTokenManager()
    session = ScriptedSession([ChallengeResponse(), ChallengeResponse()])
    transport = CurlCffiTransport(tokens, session=session)

    with pytest.raises(ChallengeDetectedError) as excinfo:
        await transport.send(_request())

    assert excinfo.value.status_code == 403
    assert excinfo.value.kind is ChallengeKind.CLOUDFLARE_CHALLENGE
    assert session.posts == 2  # original attempt + exactly one retry
    assert tokens.extract_calls == 1
    assert not session.responses  # nothing left queued for blind retries


@pytest.mark.anyio
async def test_plain_api_error_keeps_legacy_authrequired_path():
    class PlainForbiddenResponse:
        status_code = 403

        async def aiter_bytes(self):
            yield b'{"detail": "Unusual activity"}'

        async def aclose(self) -> None:
            pass

    tokens = StubTokenManager()
    session = ScriptedSession([PlainForbiddenResponse()])
    transport = CurlCffiTransport(tokens, session=session)

    with pytest.raises(AuthRequired):
        await transport.send(_request())

    assert tokens.extract_calls == 0  # non-challenge errors never force a re-mint
    assert session.posts == 1


# ---------------------------------------------------------------------------
# (c2) LIMIT-SIGNATURE-TAXONOMY: only a pure 429 quota verdict may feed the
#      breaker; an HTML challenge on any status routes to typed challenge
#      recovery instead, and undecipherable bodies keep legacy mappings.
# ---------------------------------------------------------------------------


class _StatusBodyResponse:
    """Minimal error response double for direct ``_raise_for_status`` calls."""

    def __init__(self, status_code: int, body: bytes = b"") -> None:
        self.status_code = status_code
        self._body = body

    async def aiter_bytes(self):
        yield self._body

    async def aclose(self) -> None:
        pass


class ChallengeResponse429(ChallengeResponse):
    """Cloudflare interstitial delivered on a 429 envelope (rare but real)."""

    status_code = 429


def test_limit_signal_none_below_400():
    assert classify_limit_signal(200, CHALLENGE_HTML.decode()) is LimitSignal.NONE
    assert classify_limit_signal(None, "anything") is LimitSignal.NONE


def test_limit_signal_challenge_markers_win_on_any_status():
    html = CHALLENGE_HTML.decode()
    for status in (403, 429, 503):
        assert classify_limit_signal(status, html) is LimitSignal.CHALLENGE


def test_limit_signal_pure_bare_429():
    assert classify_limit_signal(429, b'{"detail":"slow down"}'.decode()) is (
        LimitSignal.PURE_RATE_LIMIT
    )
    # Even an unreadable/empty body keeps the bare-status leg.
    assert classify_limit_signal(429, None) is LimitSignal.PURE_RATE_LIMIT


def test_limit_signal_pure_json_signature():
    payload = '{"error":{"type":"usage_limit_reached","message":"Rate limit reached"}}'
    assert classify_limit_signal(429, payload) is LimitSignal.PURE_RATE_LIMIT


def test_limit_signal_undetermined_without_quota_proof():
    assert classify_limit_signal(403, b'{"detail":"Unusual activity"}'.decode()) is (
        LimitSignal.UNDETERMINED
    )
    assert classify_limit_signal(503, None) is LimitSignal.UNDETERMINED
    assert classify_limit_signal(500, b"<html><body>oops</body></html>".decode()) is (
        LimitSignal.UNDETERMINED
    )


@pytest.mark.anyio
async def test_pure_429_keeps_rate_limited_trip_signal():
    transport = CurlCffiTransport(StubTokenManager(), session=ScriptedSession([]))
    with pytest.raises(RateLimited):
        await transport._raise_for_status(
            _StatusBodyResponse(429, b'{"error":{"type":"rate_limit_exceeded"}}')
        )


@pytest.mark.anyio
async def test_429_challenge_page_reroutes_to_typed_challenge_error():
    transport = CurlCffiTransport(StubTokenManager(), session=ScriptedSession([]))
    with pytest.raises(ChallengeDetectedError) as excinfo:
        await transport._raise_for_status(_StatusBodyResponse(429, CHALLENGE_HTML))
    assert excinfo.value.status_code == 429
    assert excinfo.value.kind is ChallengeKind.CLOUDFLARE_CHALLENGE


@pytest.mark.anyio
async def test_403_challenge_page_reroutes_to_typed_challenge_error():
    transport = CurlCffiTransport(StubTokenManager(), session=ScriptedSession([]))
    with pytest.raises(ChallengeDetectedError) as excinfo:
        await transport._raise_for_status(_StatusBodyResponse(403, CHALLENGE_HTML))
    assert excinfo.value.status_code == 403


@pytest.mark.anyio
async def test_503_challenge_page_reroutes_to_typed_challenge_error():
    transport = CurlCffiTransport(StubTokenManager(), session=ScriptedSession([]))
    with pytest.raises(ChallengeDetectedError) as excinfo:
        await transport._raise_for_status(
            _StatusBodyResponse(503, b"<html>challenge-platform</html>")
        )
    assert excinfo.value.status_code == 503


@pytest.mark.anyio
async def test_plain_503_without_markers_keeps_protocol_changed_path():
    tokens = StubTokenManager()
    session = ScriptedSession([_StatusBodyResponse(503, b'{"detail":"overloaded"}')])
    transport = CurlCffiTransport(tokens, session=session)
    with pytest.raises(ProtocolChanged):
        await transport.send(_request())
    assert session.posts == 1
    assert tokens.extract_calls == 0


@pytest.mark.anyio
async def test_429_challenge_response_remints_once_then_succeeds():
    tokens = StubTokenManager()
    session = ScriptedSession([ChallengeResponse429(), OkSseResponse()])
    transport = CurlCffiTransport(tokens, session=session)

    result = await transport.send(_request())

    assert result.text == "Hello"
    assert session.posts == 2  # original attempt + exactly one re-mint retry
    assert tokens.extract_calls == 1
    assert tokens.invalidate_calls >= 1


@pytest.mark.anyio
async def test_persistent_429_challenge_raises_typed_error_never_rate_limited():
    tokens = StubTokenManager()
    session = ScriptedSession([ChallengeResponse429(), ChallengeResponse429()])
    transport = CurlCffiTransport(tokens, session=session)

    with pytest.raises(ChallengeDetectedError) as excinfo:
        await transport.send(_request())

    assert excinfo.value.status_code == 429
    assert not isinstance(excinfo.value, RateLimited)
    assert session.posts == 2
    assert tokens.extract_calls == 1


# ---------------------------------------------------------------------------
# (d) Fingerprint consistency: exact impersonate target + minting UA
# ---------------------------------------------------------------------------


def test_impersonate_target_matches_cloakbrowser_major_version():
    try:
        import typing

        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        supported = set(typing.get_args(BrowserTypeLiteral))
    except Exception:  # pragma: no cover - minimal installations
        pytest.skip("curl_cffi impersonation metadata unavailable")
    assert IMPERSONATE_TARGET in supported
    # Evidence: docs/reports/cf-clearance-lifecycle-2026-08-24.md — clearance is
    # bound to IP+UA+TLS hello; CloakBrowser is Chrome/146.0.7680.177.
    assert IMPERSONATE_TARGET == "chrome146"


def test_headers_carry_the_exact_minted_user_agent():
    headers = CurlCffiTransport._build_headers(_bundle(), SentinelTokens("requirements"))
    assert headers["User-Agent"] == CLOAKBROWSER_USER_AGENT
    assert headers["User-Agent"] != "Mozilla/5.0"


def test_user_agent_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("WEBGPT_CF_USER_AGENT", "Test-UA/1.0")
    headers = CurlCffiTransport._build_headers(_bundle(), SentinelTokens("requirements"))
    assert headers["User-Agent"] == "Test-UA/1.0"
