from __future__ import annotations

import pytest

from gpt.transport.token_manager import TokenManager


class FakeContext:
    async def cookies(self):
        return [
            {"name": "cf_clearance", "value": "clearance"},
            {"name": "oai-device-id", "value": "device-from-cookie"},
            {"name": "session", "value": "session-cookie"},
        ]


class FakePage:
    def __init__(self):
        self.context = FakeContext()
        self.calls: list[tuple[str, object | None]] = []

    async def evaluate(self, script, argument=None):
        self.calls.append((script, argument))
        if "/api/auth/session" in script:
            return {"user": {"accessToken": "access-token"}}
        if "localStorage.getItem" in script:
            return "device-from-storage"
        if "chat-requirements" in script:
            return {
                "requirements_token": "requirements",
                "proof_token": "proof",
                "turnstile_token": "turnstile",
            }
        raise AssertionError("unexpected page script")


@pytest.mark.anyio
async def test_extracts_cookie_access_and_device_tokens_from_browser_context():
    page = FakePage()
    manager = TokenManager(page)

    bundle = await manager.extract_all()

    assert bundle.access_token == "access-token"
    assert bundle.cf_clearance == "clearance"
    assert bundle.oai_device_id == "device-from-storage"
    assert bundle.cookie_header == "cf_clearance=clearance; oai-device-id=device-from-cookie; session=session-cookie"

    sentinel = await manager.get_sentinel_tokens("conversation-1")
    assert sentinel.requirements_token == "requirements"
    assert sentinel.proof_token == "proof"
    assert sentinel.turnstile_token == "turnstile"
    assert page.calls[-1][1] == "conversation-1"


@pytest.mark.anyio
async def test_refresh_uses_cached_bundle_within_refresh_interval():
    page = FakePage()
    manager = TokenManager(page, refresh_interval=60)

    first = await manager.refresh_if_needed()
    second = await manager.refresh_if_needed()

    assert first is second
    assert sum("/api/auth/session" in script for script, _ in page.calls) == 1


@pytest.mark.anyio
async def test_missing_access_token_retries_after_configured_auto_login():
    page = FakePage()
    logged_in = False

    async def evaluate(script, argument=None):
        page.calls.append((script, argument))
        if "/api/auth/session" in script:
            return {"accessToken": "fresh-access-token"} if logged_in else {}
        if "localStorage.getItem" in script:
            return "device-from-storage"
        raise AssertionError("unexpected page script")

    async def login() -> bool:
        nonlocal logged_in
        logged_in = True
        return True

    page.evaluate = evaluate
    bundle = await TokenManager(page, auto_login=login).extract_all()

    assert bundle.access_token == "fresh-access-token"
    assert sum("/api/auth/session" in script for script, _ in page.calls) == 2


@pytest.mark.anyio
async def test_missing_access_token_uses_marked_local_bundle_only_when_enabled():
    page = FakePage()

    async def unauthenticated(script, argument=None):
        page.calls.append((script, argument))
        if "/api/auth/session" in script:
            return {}
        raise AssertionError("local mock must not read browser storage")

    page.evaluate = unauthenticated
    manager = TokenManager(page, auto_login=lambda: _false(), allow_local_mock=True)

    bundle = await manager.extract_all()
    sentinel = await manager.get_sentinel_tokens()

    assert bundle.is_local_mock is True
    assert bundle.access_token == "local-mock-token"
    assert sentinel.requirements_token == "local-mock-sentinel"


async def _false() -> bool:
    return False
