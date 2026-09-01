"""Tests for the sentinel token cache (TTL-based minting).

The page context HTTP calls are mocked at ``page.evaluate`` level, mirroring
``test_sentinel_flow.py``.  Time is controlled by injecting a fake clock into
the ``time`` reference of ``gpt.transport.token_manager`` so expiry can be
simulated without sleeping.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

import gpt.transport.token_manager as token_manager_module
from gpt.transport.token_manager import TokenManager, _local_mock_bundle

PREPARE_PATH = "/backend-anon/sentinel/chat-requirements/prepare"
FINALIZE_PATH = "/backend-anon/sentinel/chat-requirements/finalize"


class FakeClock:
    """Deterministic monotonic clock injectable over ``time``."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _envelope_counter(prefix: str = "gAAAAAB-", **extra: Any):
    """Factory returning a fresh requirements envelope per mint."""
    state = {"n": 0}

    def factory() -> dict[str, Any]:
        state["n"] += 1
        envelope = {
            "token": f"{prefix}{state['n']}",
            "proof_token": f"proof-{state['n']}",
            "turnstile_token": f"ts-{state['n']}",
        }
        envelope.update(extra)
        return envelope

    return factory


def _mock_page(envelope_factory):
    """Page whose evaluate dispatches prepare/finalize, minting per finalize."""

    async def route(script, arg=None):
        if isinstance(arg, dict) and arg.get("path") == PREPARE_PATH:
            await asyncio.sleep(0)
            return {"prepare_token": "PREP-TOKEN"}
        if isinstance(arg, dict) and arg.get("path") == FINALIZE_PATH:
            await asyncio.sleep(0)
            return envelope_factory()
        raise AssertionError(f"unexpected evaluate call: {arg!r}")

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=route)
    return page


def _finalize_mints(page: Any) -> int:
    """Number of sentinel mints = number of finalize POSTs."""
    return sum(
        1
        for call in page.evaluate.call_args_list
        if isinstance(call[0][1], dict) and call[0][1].get("path") == FINALIZE_PATH
    )


@pytest.fixture()
def clock(monkeypatch):
    """Replace the module's ``time`` reference with a controllable clock."""
    fake = FakeClock()

    class _TimeShim:
        @staticmethod
        def monotonic() -> float:
            return fake.monotonic()

    monkeypatch.setattr(token_manager_module, "time", _TimeShim)
    return fake


@pytest.mark.asyncio
async def test_cached_sentinel_mints_once_within_ttl(clock):
    page = _mock_page(_envelope_counter(expire_after=540))
    manager = TokenManager(page)

    first = await manager.get_sentinel_tokens("conv-1")
    second = await manager.get_sentinel_tokens("conv-1")

    assert second is first
    assert first.requirements_token == "gAAAAAB-1"
    assert _finalize_mints(page) == 1


@pytest.mark.asyncio
async def test_expired_cache_mints_again(clock):
    page = _mock_page(_envelope_counter(expire_after=540))
    manager = TokenManager(page)

    await manager.get_sentinel_tokens()
    assert _finalize_mints(page) == 1

    # TTL = expire_after(540) - margin(60) = 480s: still valid at +479s.
    clock.advance(479)
    await manager.get_sentinel_tokens()
    assert _finalize_mints(page) == 1

    clock.advance(2)
    refreshed = await manager.get_sentinel_tokens()
    assert refreshed.requirements_token == "gAAAAAB-2"
    assert _finalize_mints(page) == 2


@pytest.mark.asyncio
async def test_missing_expire_after_uses_default_ttl_480(clock):
    page = _mock_page(_envelope_counter())  # no expire_after key anywhere
    manager = TokenManager(page)

    await manager.get_sentinel_tokens()

    clock.advance(479)
    await manager.get_sentinel_tokens()
    assert _finalize_mints(page) == 1

    clock.advance(2)
    await manager.get_sentinel_tokens()
    assert _finalize_mints(page) == 2


@pytest.mark.asyncio
async def test_ttl_margin_env_shrinks_effective_ttl(clock, monkeypatch):
    monkeypatch.setenv("WEBGPT_SENTINEL_TTL_MARGIN", "300")
    page = _mock_page(_envelope_counter(expire_after=540))
    manager = TokenManager(page)

    await manager.get_sentinel_tokens()
    clock.advance(239)  # TTL = 540 - 300 = 240s
    await manager.get_sentinel_tokens()
    assert _finalize_mints(page) == 1

    clock.advance(2)
    await manager.get_sentinel_tokens()
    assert _finalize_mints(page) == 2


@pytest.mark.asyncio
async def test_margin_larger_than_lifetime_falls_back_to_default(clock, monkeypatch):
    monkeypatch.setenv("WEBGPT_SENTINEL_TTL_MARGIN", "600")
    page = _mock_page(_envelope_counter(expire_after=540))
    manager = TokenManager(page)

    await manager.get_sentinel_tokens()
    clock.advance(479)  # fallback TTL 480, not the negative 540-600
    await manager.get_sentinel_tokens()
    assert _finalize_mints(page) == 1

    clock.advance(2)
    await manager.get_sentinel_tokens()
    assert _finalize_mints(page) == 2


@pytest.mark.asyncio
async def test_cache_disabled_env_mints_every_turn(clock, monkeypatch):
    monkeypatch.setenv("WEBGPT_SENTINEL_CACHE", "0")
    page = _mock_page(_envelope_counter(expire_after=540))
    manager = TokenManager(page)

    first = await manager.get_sentinel_tokens()
    second = await manager.get_sentinel_tokens()

    assert first.requirements_token == "gAAAAAB-1"
    assert second.requirements_token == "gAAAAAB-2"
    assert _finalize_mints(page) == 2


@pytest.mark.asyncio
async def test_invalidate_sentinel_clears_cache(clock):
    page = _mock_page(_envelope_counter(expire_after=540))
    manager = TokenManager(page)

    await manager.get_sentinel_tokens()
    assert _finalize_mints(page) == 1

    manager.invalidate_sentinel()
    refreshed = await manager.get_sentinel_tokens()

    assert refreshed.requirements_token == "gAAAAAB-2"
    assert _finalize_mints(page) == 2


@pytest.mark.asyncio
async def test_local_mock_bypasses_cache_without_page_calls():
    page = AsyncMock()
    manager = TokenManager(page)
    manager._bundle = _local_mock_bundle()

    tokens = await manager.get_sentinel_tokens()

    assert tokens.requirements_token == "local-mock-sentinel"
    page.evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_getters_share_single_mint(clock):
    page = _mock_page(_envelope_counter(expire_after=540))
    manager = TokenManager(page)

    first, second = await asyncio.gather(
        manager.get_sentinel_tokens(),
        manager.get_sentinel_tokens(),
    )

    assert first is second
    assert _finalize_mints(page) == 1
