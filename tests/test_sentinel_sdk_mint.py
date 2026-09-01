"""Tests for the in-page SentinelSDK mint path and its transport wiring.

Evidence baseline: ``docs/reports/sentinel-sdk-probe-2026-08-24.md`` —
injecting ``/backend-api/sentinel/sdk.js`` exposes ``globalThis.SentinelSDK``
whose ``token('chatgpt')`` resolves a JSON string ``{p, t, c}`` (proof,
turnstile, chat-requirements token); a conversation POST carrying all three
``openai-sentinel-*`` headers streamed 200 SSE on ``/backend-anon``.

Everything is mocked at ``page.evaluate`` level; no browser is involved.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

import gpt.transport.token_manager as token_manager_module
from gpt.transport.curl_transport import CurlCffiTransport
from gpt.transport.token_manager import (
    SentinelTokens,
    TokenBundle,
    TokenManager,
)

PREPARE_PATH = "/backend-anon/sentinel/chat-requirements/prepare"
FINALIZE_PATH = "/backend-anon/sentinel/chat-requirements/finalize"

SDK_PAYLOAD = {
    "p": "gAAAAAB-sdk-proof",
    "t": "0.turnstile-sdk-result",
    "c": "gAAAAAB-sdk-requirements",
    "expire_after": 540,
}


class FakeClock:
    """Deterministic monotonic clock injectable over ``time``."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


_DEFAULT_PREPARE: dict[str, Any] = {"prepare_token": "PREP"}
_DEFAULT_FINALIZE: dict[str, Any] = {"token": "gAAAAAB-final", "expire_after": 540}
_DEFAULT_SDK_RESULT: str = json.dumps(SDK_PAYLOAD)


def make_page(
    *,
    sdk_result: Any = _DEFAULT_SDK_RESULT,
    prepare_response: dict[str, Any] | Exception | None = _DEFAULT_PREPARE,
    finalize_response: dict[str, Any] | Exception | None = _DEFAULT_FINALIZE,
):
    """Page whose evaluate dispatches SDK scripts vs. sentinel HTTP paths."""
    calls: list[tuple[str, Any]] = []

    async def route(script, arg=None):
        calls.append((script, arg))
        if isinstance(script, str) and "SentinelSDK" in script:
            if isinstance(sdk_result, Exception):
                raise sdk_result
            return sdk_result
        if isinstance(arg, dict) and arg.get("path") == PREPARE_PATH:
            await asyncio.sleep(0)
            if isinstance(prepare_response, Exception):
                raise prepare_response
            return prepare_response
        if isinstance(arg, dict) and arg.get("path") == FINALIZE_PATH:
            await asyncio.sleep(0)
            if isinstance(finalize_response, Exception):
                raise finalize_response
            return finalize_response
        raise AssertionError(f"unexpected evaluate call: {script!r} / {arg!r}")

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=route)
    page.calls = calls
    return page


def sdk_calls(page: Any) -> int:
    return sum(1 for script, _ in page.calls if "SentinelSDK" in script)


def finalize_calls(page: Any) -> int:
    return sum(
        1
        for _, arg in page.calls
        if isinstance(arg, dict) and arg.get("path") == FINALIZE_PATH
    )


@pytest.fixture()
def clock(monkeypatch):
    fake = FakeClock()

    class _TimeShim:
        @staticmethod
        def monotonic() -> float:
            return fake.monotonic()

    monkeypatch.setattr(token_manager_module, "time", _TimeShim)
    return fake


@pytest.fixture(autouse=True)
def sdk_env(monkeypatch):
    """Default to the SDK being enabled unless a test overrides it."""
    monkeypatch.delenv("WEBGPT_SENTINEL_SDK", raising=False)


@pytest.mark.asyncio
async def test_sdk_mint_fills_proof_and_turnstile_with_ttl_from_envelope(clock):
    """(a) SDK success: full token set + TTL derived from expire_after."""
    page = make_page()  # finalize carries no proof/turnstile → SDK fills them.
    manager = TokenManager(page)

    first = await manager.get_sentinel_tokens("conv-1")
    second = await manager.get_sentinel_tokens("conv-1")

    assert first.requirements_token == SDK_PAYLOAD["c"]
    assert first.proof_token == SDK_PAYLOAD["p"]
    assert first.turnstile_token == SDK_PAYLOAD["t"]

    # TTL = expire_after(540) - margin(60) = 480s, sourced from the SDK mint.
    assert second is first
    assert sdk_calls(page) == 1
    clock.advance(479)
    await manager.get_sentinel_tokens("conv-1")
    assert sdk_calls(page) == 1

    clock.advance(2)
    refreshed = await manager.get_sentinel_tokens("conv-1")
    assert refreshed.proof_token == SDK_PAYLOAD["p"]
    assert sdk_calls(page) == 2


@pytest.mark.asyncio
async def test_sdk_failure_falls_back_to_prepare_finalize_flow():
    """(b) SDK throwing leaves the existing prepare→finalize flow intact."""
    page = make_page(sdk_result=RuntimeError("page closed"))
    manager = TokenManager(page)

    tokens = await manager.get_sentinel_tokens("conv-1")

    assert tokens.requirements_token == "gAAAAAB-final"
    assert tokens.proof_token is None
    assert tokens.turnstile_token is None
    assert finalize_calls(page) == 1
    assert sdk_calls(page) == 1


@pytest.mark.asyncio
async def test_sdk_payload_missing_tokens_falls_back():
    """An SDK answer without p/t/c is rejected and the legacy result kept."""
    page = make_page(sdk_result=json.dumps({"c": "only-requirements"}))
    manager = TokenManager(page)

    tokens = await manager.get_sentinel_tokens()

    assert tokens.requirements_token == "gAAAAAB-final"
    assert tokens.proof_token is None
    assert tokens.turnstile_token is None


@pytest.mark.asyncio
async def test_full_finalize_envelope_skips_sdk_entirely():
    """When requirements already carry proof+turnstile, sdk.js never loads."""
    page = make_page(
        finalize_response={
            "token": "gAAAAAB-full",
            "proof_token": "proof-legacy",
            "turnstile_token": "ts-legacy",
            "expire_after": 540,
        }
    )
    manager = TokenManager(page)

    tokens = await manager.get_sentinel_tokens()

    assert tokens.requirements_token == "gAAAAAB-full"
    assert tokens.proof_token == "proof-legacy"
    assert tokens.turnstile_token == "ts-legacy"
    assert sdk_calls(page) == 0


@pytest.mark.asyncio
async def test_sdk_disabled_env_never_injects_script(monkeypatch):
    """(c) WEBGPT_SENTINEL_SDK=0 rolls back to the pure legacy flow."""
    monkeypatch.setenv("WEBGPT_SENTINEL_SDK", "0")
    page = make_page()
    manager = TokenManager(page)

    tokens = await manager.get_sentinel_tokens()

    assert tokens.requirements_token == "gAAAAAB-final"
    assert sdk_calls(page) == 0
    assert finalize_calls(page) == 1


def _bundle() -> TokenBundle:
    cookies = {"cf_clearance": "clearance", "oai-device-id": "device"}
    return TokenBundle(
        access_token="access-token",
        cookies=cookies,
        cf_clearance="clearance",
        oai_device_id="device",
    )


class TestCurlTransportHeaders:
    def test_all_three_sentinel_headers_when_complete(self):
        """(d1) Full SDK-minted set maps onto all three sentinel headers."""
        headers = CurlCffiTransport._build_headers(
            _bundle(),
            SentinelTokens(
                requirements_token="req-token",
                proof_token="proof-token",
                turnstile_token="ts-token",
            ),
        )
        assert headers["openai-sentinel-chat-requirements-token"] == "req-token"
        assert headers["openai-sentinel-proof-token"] == "proof-token"
        assert headers["openai-sentinel-turnstile-token"] == "ts-token"

    def test_requirements_only_omits_missing_headers_without_crash(self):
        """(d2) Missing proof/turnstile keeps the old requirements-only shape."""
        headers = CurlCffiTransport._build_headers(
            _bundle(),
            SentinelTokens(requirements_token="req-token"),
        )
        assert headers["openai-sentinel-chat-requirements-token"] == "req-token"
        assert "openai-sentinel-proof-token" not in headers
        assert "openai-sentinel-turnstile-token" not in headers
