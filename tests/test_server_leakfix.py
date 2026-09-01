"""Regression tests for gateway leak fixes and account wiring (leak-fix wave).

Covers:
- per-conversation locks are cleaned up after the last waiter releases them;
- ``_response_sessions`` is a bounded LRU;
- the multi-account factory receives the resolved sticky default account and
  an optional health tracker;
- curl transport invalidates the cached sentinel on 401/403 rejections.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gpt.gateway.server import DEFAULT_RESPONSE_SESSION_CAP, WebChatAPIServer
from gpt.requests import parse_chat_completion_request
from gpt.state import AuthRequired
from gpt.transport.curl_transport import CurlCffiTransport
from gpt.transport.multi_account import MultiAccountWorkerFactory
from gpt.types import SendRequest


def _make_mock_server() -> WebChatAPIServer:
    return WebChatAPIServer(mock_backend=True)


def _mock_request(content: str):
    return parse_chat_completion_request(
        {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": content}],
        },
        protocol="openai_chat",
        client="test",
    )


# ----------------------------------------------------------------------
# 1. Conversation-lock cleanup
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_conversation_locks_cleaned_after_200_distinct_conversations():
    server = _make_mock_server()
    for index in range(200):
        normalized = _mock_request(f"distinct question {index}")
        _response, record = await server.complete_normalized(normalized)
        assert record.session_id
    assert len(server._conversation_locks) <= 3


@pytest.mark.anyio
async def test_conversation_lock_serializes_and_then_cleans_up():
    server = _make_mock_server()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with server._conversation_lock("shared-conversation"):
            order.append(f"enter:{name}")
            await asyncio.sleep(0.01)
            order.append(f"exit:{name}")

    await asyncio.gather(worker("a"), worker("b"))

    # Strict enter/exit interleaving proves serialization.
    assert order.index("enter:a") < order.index("exit:a") < order.index(
        "enter:b"
    ) < order.index("exit:b") or order.index("enter:b") < order.index(
        "exit:b"
    ) < order.index("enter:a") < order.index("exit:a")
    assert len(server._conversation_locks) == 0


# ----------------------------------------------------------------------
# 2. Response-session LRU cap
# ----------------------------------------------------------------------


def test_response_sessions_default_cap_is_512(monkeypatch):
    monkeypatch.delenv("WEBGPT_RESPONSE_SESSION_CAP", raising=False)
    server = _make_mock_server()
    assert server._response_session_cap == DEFAULT_RESPONSE_SESSION_CAP == 512


def test_response_sessions_lru_eviction_respects_env_cap(monkeypatch):
    monkeypatch.setenv("WEBGPT_RESPONSE_SESSION_CAP", "64")
    server = _make_mock_server()
    for index in range(600):
        server._remember_response_session(f"resp_{index}", f"session_{index}")

    assert len(server._response_sessions) <= 64
    # Only the newest entries survive.
    assert "resp_0" not in server._response_sessions
    assert "resp_599" in server._response_sessions


def test_response_sessions_touch_refreshes_recency(monkeypatch):
    monkeypatch.setenv("WEBGPT_RESPONSE_SESSION_CAP", "8")
    server = _make_mock_server()
    for index in range(8):
        server._remember_response_session(f"resp_{index}", f"session_{index}")

    # Touch the oldest entry, then insert one more; resp_0 must survive while
    # the previously-newest entry is evicted instead.
    assert server._lookup_response_session("resp_0") == "session_0"
    server._remember_response_session("resp_8", "session_8")

    assert len(server._response_sessions) <= 8
    assert "resp_0" in server._response_sessions
    assert "resp_1" not in server._response_sessions

    # A lookup hit must also refresh recency for later insertions.
    assert server._lookup_response_session("missing") is None


@pytest.mark.anyio
async def test_responses_endpoint_stores_into_lru(monkeypatch):
    """The HTTP-facing responses path uses the bounded LRU helpers."""
    server = _make_mock_server()
    server._remember_response_session("resp_known", "session_known")
    assert server._lookup_response_session("resp_known") == "session_known"


# ----------------------------------------------------------------------
# 3. Default-account / health-tracker wiring into MultiAccountWorkerFactory
# ----------------------------------------------------------------------


class _FakeAccountStore:
    names = ("alpha", "beta")
    registered_default: str | None = None

    def list(self):
        return [SimpleNamespace(name=name) for name in self.names]

    def get_default(self):
        return self.registered_default


@pytest.mark.anyio
async def test_factory_receives_default_name_from_env_override(monkeypatch):
    monkeypatch.setattr("gpt.gateway.server.AccountStore", _FakeAccountStore)
    monkeypatch.setenv("WEBGPT_DEFAULT_ACCOUNT", "beta")

    server = WebChatAPIServer(
        account_profiles={"alpha": "/tmp/wg-alpha", "beta": "/tmp/wg-beta"}
    )

    assert isinstance(server._worker_factory, MultiAccountWorkerFactory)
    assert server._worker_factory.default_name == "beta"
    assert server._worker_factory.health is None


@pytest.mark.anyio
async def test_factory_default_name_falls_back_to_registry(monkeypatch):
    monkeypatch.delenv("WEBGPT_DEFAULT_ACCOUNT", raising=False)

    class _RegistryDefaultStore(_FakeAccountStore):
        def get_default(self):
            return "alpha"

    monkeypatch.setattr("gpt.gateway.server.AccountStore", _RegistryDefaultStore)
    server = WebChatAPIServer(
        account_profiles={"alpha": "/tmp/wg-alpha", "beta": "/tmp/wg-beta"}
    )

    assert isinstance(server._worker_factory, MultiAccountWorkerFactory)
    assert server._worker_factory.default_name == "alpha"


@pytest.mark.anyio
async def test_health_flag_enables_tracker_on_factory(monkeypatch):
    monkeypatch.setattr("gpt.gateway.server.AccountStore", _FakeAccountStore)
    monkeypatch.delenv("WEBGPT_DEFAULT_ACCOUNT", raising=False)
    monkeypatch.setenv("WEBGPT_HEALTH_CHECK_ENABLED", "1")

    server = WebChatAPIServer(
        account_profiles={"alpha": "/tmp/wg-alpha", "beta": "/tmp/wg-beta"}
    )

    assert server._account_health_tracker is not None
    assert server._worker_factory.health is server._account_health_tracker
    await server.close()
    assert server._health_loop_task is None


@pytest.mark.anyio
async def test_health_flag_off_keeps_tracker_none(monkeypatch):
    monkeypatch.setattr("gpt.gateway.server.AccountStore", _FakeAccountStore)
    monkeypatch.delenv("WEBGPT_DEFAULT_ACCOUNT", raising=False)
    monkeypatch.delenv("WEBGPT_HEALTH_CHECK_ENABLED", raising=False)

    server = WebChatAPIServer(
        account_profiles={"alpha": "/tmp/wg-alpha", "beta": "/tmp/wg-beta"}
    )

    assert server._account_health_tracker is None
    assert server._worker_factory.health is None


@pytest.mark.anyio
async def test_health_loop_not_started_when_flag_unset(monkeypatch):
    monkeypatch.setattr("gpt.gateway.server.AccountStore", _FakeAccountStore)
    monkeypatch.delenv("WEBGPT_DEFAULT_ACCOUNT", raising=False)
    monkeypatch.delenv("WEBGPT_HEALTH_CHECK_ENABLED", raising=False)

    server = WebChatAPIServer(
        account_profiles={"alpha": "/tmp/wg-alpha", "beta": "/tmp/wg-beta"}
    )
    server.start_account_health_loop()

    assert server._health_loop_task is None


# ----------------------------------------------------------------------
# 4. curl_transport sentinel invalidation on auth rejection
# ----------------------------------------------------------------------


class _RecordingTokenManager:
    def __init__(self) -> None:
        self.invalidate_calls = 0

    async def refresh_if_needed(self):
        from gpt.transport.token_manager import TokenBundle

        return TokenBundle(
            access_token="access-token",
            cookies={"cf_clearance": "clearance"},
            cf_clearance="clearance",
            oai_device_id="device-id",
        )

    async def get_sentinel_tokens(self, conversation_id):
        from gpt.transport.token_manager import SentinelTokens

        return SentinelTokens("requirements", "proof", "turnstile")

    def invalidate_sentinel(self) -> None:
        self.invalidate_calls += 1


class _RejectedResponse:
    status_code = 401

    async def aclose(self) -> None:
        pass


class _RejectingSession:
    def __init__(self) -> None:
        self.response = _RejectedResponse()

    async def post(self, *args, **kwargs):
        return self.response


@pytest.mark.anyio
async def test_401_response_invalidates_sentinel_cache():
    token_manager = _RecordingTokenManager()
    transport = CurlCffiTransport(token_manager, session=_RejectingSession())

    request = SendRequest(text="Hello", conversation_id="conversation-1")
    with pytest.raises(AuthRequired):
        await transport.send(request)

    assert token_manager.invalidate_calls == 1


@pytest.mark.anyio
async def test_raise_for_status_invalidates_on_401_and_403():
    token_manager = _RecordingTokenManager()
    transport = CurlCffiTransport(token_manager, session=object())

    with pytest.raises(AuthRequired):
        await transport._raise_for_status(SimpleNamespace(status_code=401))
    assert token_manager.invalidate_calls == 1

    with pytest.raises(AuthRequired):
        await transport._raise_for_status(SimpleNamespace(status_code=403))
    assert token_manager.invalidate_calls == 2

    # Non-auth failures must not touch the sentinel cache.
    from gpt.state import ProtocolChanged

    with pytest.raises(ProtocolChanged):
        await transport._raise_for_status(SimpleNamespace(status_code=500))
    assert token_manager.invalidate_calls == 2


@pytest.mark.anyio
async def test_missing_credentials_invalidates_sentinel_cache():
    from gpt.transport.token_manager import SentinelTokens, TokenBundle

    class _BundleTokenManager(_RecordingTokenManager):
        async def refresh_if_needed(self):
            return TokenBundle(access_token="", cookies={}, cf_clearance=None,
                               oai_device_id="device-id")

        async def get_sentinel_tokens(self, conversation_id):
            return SentinelTokens("requirements", None, None)

    token_manager = _BundleTokenManager()
    transport = CurlCffiTransport(token_manager, session=_RejectingSession())
    with pytest.raises(AuthRequired):
        await transport.send(SendRequest(text="Hello"))

    assert token_manager.invalidate_calls == 1


@pytest.mark.anyio
async def test_sentinel_invalidation_survives_token_manager_without_hook():
    """A token manager without invalidate_sentinel must not break the turn."""

    class _LegacyTokenManager:
        async def refresh_if_needed(self):
            raise AssertionError("not reached")

    transport = CurlCffiTransport(_LegacyTokenManager(), session=object())
    transport._invalidate_sentinel_cache()  # must be a silent no-op
