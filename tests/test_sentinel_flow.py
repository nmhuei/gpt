"""Tests for the two-step sentinel flow and conduit preparation.

The page context HTTP calls are mocked at ``page.evaluate`` level so the
scenarios mirror real capture envelopes without a browser.
"""

from unittest.mock import AsyncMock

import pytest

from gpt.state import AuthRequired
from gpt.transport.token_manager import TokenManager


def _make_manager(responses):
    """Build a TokenManager whose evaluate dispatches on the fetch path."""

    async def route(script, arg=None):
        if isinstance(arg, dict) and "path" in arg:
            handler = responses.get(arg["path"])
        else:
            handler = responses.get("legacy")
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(arg)
        return handler

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=route)
    return TokenManager(page), page


PREPARE_PATH = "/backend-anon/sentinel/chat-requirements/prepare"
FINALIZE_PATH = "/backend-anon/sentinel/chat-requirements/finalize"
CONDUIT_PATH = "/backend-anon/f/conversation/prepare"


@pytest.mark.asyncio
async def test_prepare_finalize_success():
    manager, page = _make_manager(
        {
            PREPARE_PATH: {"prepare_token": "PREP-TOKEN", "persona": "chatgpt-noauth"},
            FINALIZE_PATH: {
                "token": "gAAAAAB-final",
                "proof_token": "proof-1",
                "turnstile_token": "ts-1",
            },
        }
    )
    tokens = await manager.get_sentinel_tokens("conv-1")
    assert tokens.requirements_token == "gAAAAAB-final"
    assert tokens.proof_token == "proof-1"
    assert tokens.turnstile_token == "ts-1"
    # Finalize must receive the prepare_token under the observed "prepare_token" key
    # (live probe 2026-08-24: {"prepare_token": …} → 200, {"p": …} → 500).
    finalize_arg = page.evaluate.call_args_list[1][0][1]
    assert finalize_arg["path"] == FINALIZE_PATH
    assert finalize_arg["payload"] == {"prepare_token": "PREP-TOKEN"}
    # Legacy endpoint must not have been touched on success.
    assert all(
        call[0][1].get("path") != "/backend-anon/sentinel/chat-requirements"
        for call in page.evaluate.call_args_list
        if isinstance(call[0][1], dict)
    )


@pytest.mark.asyncio
async def test_prepare_failure_falls_back_to_legacy():
    manager, _ = _make_manager(
        {
            PREPARE_PATH: {"status": 404},
            "legacy": {"requirements_token": "legacy-token"},
        }
    )
    tokens = await manager.get_sentinel_tokens()
    assert tokens.requirements_token == "legacy-token"


@pytest.mark.asyncio
async def test_finalize_missing_token_falls_back_to_legacy():
    manager, _ = _make_manager(
        {
            PREPARE_PATH: {"prepare_token": "PREP-TOKEN"},
            FINALIZE_PATH: {"status": 200},
            "legacy": {"sentinel_token": "legacy-token"},
        }
    )
    tokens = await manager.get_sentinel_tokens()
    assert tokens.requirements_token == "legacy-token"


@pytest.mark.asyncio
async def test_legacy_rejection_still_raises_auth_required():
    """A failing fallback keeps the original AuthRequired contract."""
    manager, _ = _make_manager({"legacy": {"status": 403}})
    with pytest.raises(AuthRequired):
        await manager.get_sentinel_tokens()


@pytest.mark.asyncio
async def test_prepare_conduit_returns_token():
    manager, _ = _make_manager({CONDUIT_PATH: {"conduit_token": "conduit-1"}})
    assert await manager.prepare_conduit() == "conduit-1"


@pytest.mark.asyncio
async def test_prepare_conduit_returns_none_on_error():
    manager, _ = _make_manager({CONDUIT_PATH: RuntimeError("page closed")})
    assert await manager.prepare_conduit() is None


@pytest.mark.asyncio
async def test_prepare_conduit_returns_none_without_token():
    manager, _ = _make_manager({CONDUIT_PATH: {"status": 204}})
    assert await manager.prepare_conduit() is None
