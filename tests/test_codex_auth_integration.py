"""CODEX-AUTH-INTEGRATION tests (docs/reports/codex-auth-integration-2026-08-26.md).

Fake HTTP only — the network is never touched.  Verifies that wiring
``codex_auth.get_access_token()`` into the CODEX-SSE branch:

- keeps byte-for-byte old behavior when either flag is OFF,
- swaps the Bearer to the OAuth bundle token when BOTH
  ``WEBGPT_CODEX_SSE=1`` AND ``WEBGPT_CODEX_AUTH_JSON`` are set,
- rotates once and retries exactly once on a 401 from codex/responses,
- propagates ``CodexAuthDead`` (invalid_grant) without any retry loop.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from gpt.state import AuthRequired
from gpt.transport.codex_auth import ENV_AUTH_JSON, CodexAuthDead
from gpt.transport.curl_transport import CODEX_RESPONSES_URL, CurlCffiTransport
from gpt.transport.token_manager import SentinelTokens, TokenBundle
from gpt.types import SendRequest

CODEX_EVENTS = (
    '{"type":"response.created","response":{"id":"resp_oauth1"}}',
    '{"type":"response.output_text.delta","delta":"ok"}',
    '{"type":"response.completed","response":{"id":"resp_oauth1","output":[]}}',
)

PROMPT = (
    '<WEBGPT_MESSAGE role="system">\n{"content":"sys"}\n</WEBGPT_MESSAGE>\n\n'
    '<WEBGPT_MESSAGE role="user">\n{"content":"hello"}\n</WEBGPT_MESSAGE>'
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scrub_flags(monkeypatch):
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)
    monkeypatch.delenv(ENV_AUTH_JSON, raising=False)
    monkeypatch.delenv("WEBGPT_CODEX_CLIENT_ID", raising=False)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_jwt(expires_in: float = 3600.0) -> str:
    payload = {"sub": "auth|user", "exp": int(time.time() + expires_in)}
    return ".".join(
        [
            _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()),
            _b64url(json.dumps(payload).encode()),
            _b64url(b"signature"),
        ]
    )


class FakeTokenManager:
    def __init__(self) -> None:
        self.invalidated = False
        self.access_invalidations = 0
        self.sentinel_calls = 0

    async def refresh_if_needed(self):
        return TokenBundle(
            access_token="web-session-at",
            cookies={"cf_clearance": "clearance"},
            cf_clearance="clearance",
            oai_device_id="device-id",
        )

    async def get_sentinel_tokens(self, conversation_id):
        self.sentinel_calls += 1
        return SentinelTokens("requirements")

    def invalidate_sentinel(self) -> None:
        self.invalidated = True

    def invalidate_access_token(self) -> None:
        self.access_invalidations += 1


class StubCodexAuth:
    """OAuth source stub: pops one bearer per get_access_token call.

    Round 13: also records ``force_refresh`` fetches and the untrusted/
    trusted latch signals the transport is required to forward.
    """

    def __init__(self, tokens: list[str], dead_reason: str | None = None) -> None:
        self._tokens = list(tokens)
        self._dead_reason = dead_reason
        self.get_calls = 0
        self.forced_calls = 0
        self.invalidate_calls = 0
        self.untrusted_marks: list[str] = []
        self.trusted_marks = 0

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        self.get_calls += 1
        if force_refresh:
            self.forced_calls += 1
        if self._dead_reason is not None and not self._tokens:
            raise CodexAuthDead(self._dead_reason)
        if not self._tokens:
            return f"rotated-{self.get_calls}"
        return self._tokens.pop(0)

    def invalidate(self) -> None:
        # Mirrors the real manager: drops the cached snapshot; a DEAD mark
        # (dead_reason set) survives so later calls fail fast again.
        self.invalidate_calls += 1

    def mark_untrusted(self, reason: str | None = None) -> None:
        self.untrusted_marks.append(reason or "")

    def mark_trusted(self) -> None:
        self.trusted_marks += 1


class DeadOnFirstCall(StubCodexAuth):
    def __init__(self, reason: str) -> None:
        super().__init__([], dead_reason=reason)

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        self.get_calls += 1
        raise CodexAuthDead(self._dead_reason or "dead")


def _sse_bytes(*records: str) -> bytes:
    return "".join(f"data: {record}\n\n" for record in records).encode("utf-8")


class FakeResponse:
    status_code = 200

    def __init__(self, payload: bytes = b"", status_code: int | None = None):
        self._payload = payload
        if status_code is not None:
            self.status_code = status_code
        self.closed = False

    async def aiter_bytes(self):
        yield self._payload

    async def aclose(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse] | None = None):
        self.responses = responses or [FakeResponse(_sse_bytes(*CODEX_EVENTS))]
        self.calls: list[tuple[tuple, dict]] = []

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# Flag gating
# ---------------------------------------------------------------------------


async def test_sse_on_without_auth_json_keeps_web_session_bearer(monkeypatch):
    # WEBGPT_CODEX_SSE=1 alone must reproduce the pre-integration behavior
    # exactly: Bearer from the web-session snapshot, no OAuth involvement.
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    session = FakeSession()
    manager = FakeTokenManager()
    transport = CurlCffiTransport(manager, session=session)

    result = await transport.send(SendRequest(text=PROMPT))

    headers = session.calls[0][1]["headers"]
    assert headers["Authorization"] == "Bearer web-session-at"
    assert args_url(session, 0) == CODEX_RESPONSES_URL
    assert result.text == "ok"
    # The lazy OAuth source was never constructed (still None).
    assert transport._codex_auth is None


async def test_auth_json_without_sse_keeps_legacy_path(monkeypatch, tmp_path):
    # AUTH_JSON set but SSE off → legacy /f/conversation path, web-session
    # Bearer, and the OAuth source is never consulted even though enabled.
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(ENV_AUTH_JSON, str(auth_path))

    class ExplodingStub(StubCodexAuth):
        async def get_access_token(self, *, force_refresh: bool = False) -> str:
            raise AssertionError("codex auth must not be consulted with SSE off")

    class LegacyResponse(FakeResponse):
        async def aiter_bytes(self):
            yield b'data: {"conversation_id":"conversation-1","message":{"id":"turn-1","content":{"parts":["Hel"]}}}\n\n'
            yield b'data: {"message":{"content":{"parts":["lo"]},"status":"finished_successfully"}}\n\n'

    session = FakeSession([LegacyResponse()])
    manager = FakeTokenManager()
    transport = CurlCffiTransport(
        manager, session=session, codex_auth=ExplodingStub(["x"])
    )

    result = await transport.send(SendRequest(text="Hello"))

    args, kwargs = session.calls[0]
    assert args[0].endswith("/backend-api/f/conversation")
    assert kwargs["headers"]["Authorization"] == "Bearer web-session-at"
    assert manager.sentinel_calls == 1
    assert result.text == "Hello"


async def test_both_flags_use_oauth_bundle_bearer_end_to_end(monkeypatch, tmp_path):
    # Full wiring without injection: env flag points at a real-shaped
    # auth.json whose JWT is still fresh → its access_token becomes the
    # Bearer via a lazily-constructed CodexAuthManager (no refresh HTTP).
    access = _make_jwt()
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": access,
                    "refresh_token": "refresh-token",
                },
                "last_refresh": "2026-08-26T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    monkeypatch.setenv(ENV_AUTH_JSON, str(auth_path))

    session = FakeSession()
    manager = FakeTokenManager()
    transport = CurlCffiTransport(manager, session=session)

    result = await transport.send(SendRequest(text=PROMPT))

    headers = session.calls[0][1]["headers"]
    assert headers["Authorization"] == f"Bearer {access}"
    assert headers["Authorization"] != "Bearer web-session-at"
    # Still the codex envelope; no sentinel mint, cookies kept from bundle.
    assert args_url(session, 0) == CODEX_RESPONSES_URL
    assert manager.sentinel_calls == 0
    assert "cf_clearance=clearance" in headers["Cookie"]
    assert result.text == "ok"


def args_url(session: FakeSession, index: int) -> str:
    return session.calls[index][0][0]


# ---------------------------------------------------------------------------
# 401 → rotate once → retry exactly once
# ---------------------------------------------------------------------------


async def test_401_rotates_once_and_retries_with_new_bearer(monkeypatch):
    expired = _make_jwt(expires_in=-10)
    fresh = _make_jwt(expires_in=3600)
    stub = StubCodexAuth([expired, fresh])
    session = FakeSession(
        [
            FakeResponse(status_code=401),
            FakeResponse(_sse_bytes(*CODEX_EVENTS)),
        ]
    )
    transport = CurlCffiTransport(
        FakeTokenManager(), session=session, codex_auth=stub
    )
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    monkeypatch.setenv(ENV_AUTH_JSON, "/tmp/unused-auth.json")

    result = await transport.send(SendRequest(text=PROMPT))

    assert len(session.calls) == 2
    first = session.calls[0][1]["headers"]
    second = session.calls[1][1]["headers"]
    assert first["Authorization"] == f"Bearer {expired}"
    assert second["Authorization"] == f"Bearer {fresh}"
    assert session.calls[1][0][0] == CODEX_RESPONSES_URL
    # Exactly one cache drop between the attempts; exactly two token fetches.
    assert stub.invalidate_calls == 1
    assert stub.get_calls == 2
    # Round 13: the retry fetch must FORCE a real refresh — invalidate()
    # alone would re-serve the same still-fresh bearer that was just 401'd.
    assert stub.forced_calls == 1
    assert result.text == "ok"


async def test_second_401_marks_oauth_source_untrusted(monkeypatch):
    """A twice-rejected bearer flags the source instead of leaving it cached."""
    fresh_a = _make_jwt()
    fresh_b = _make_jwt()
    stub = StubCodexAuth([fresh_a, fresh_b])
    session = FakeSession([FakeResponse(status_code=401), FakeResponse(status_code=401)])
    manager = FakeTokenManager()
    transport = CurlCffiTransport(manager, session=session, codex_auth=stub)
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    monkeypatch.setenv(ENV_AUTH_JSON, "/tmp/unused-auth.json")

    with pytest.raises(AuthRequired, match="rejected"):
        await transport.send(SendRequest(text=PROMPT))

    # Round 13: the OAuth source itself is marked untrusted (later turns must
    # not keep replaying the rejected snapshot), in addition to the generic
    # browser-side cache drop.
    assert stub.untrusted_marks
    assert stub.invalidate_calls == 1
    assert stub.get_calls == 2
    assert manager.access_invalidations == 1
    assert len(session.calls) == 2


async def test_accepted_request_clears_untrusted_latch(monkeypatch):
    """One fully accepted codex request re-validates a distrusted source."""
    fresh_a = _make_jwt()
    fresh_b = _make_jwt()
    stub = StubCodexAuth([fresh_a, fresh_b])
    session = FakeSession(
        [
            FakeResponse(status_code=401),
            FakeResponse(status_code=401),            # send #1: double rejection
            FakeResponse(_sse_bytes(*CODEX_EVENTS)),  # send #2: accepted
        ]
    )
    transport = CurlCffiTransport(
        FakeTokenManager(), session=session, codex_auth=stub
    )
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    monkeypatch.setenv(ENV_AUTH_JSON, "/tmp/unused-auth.json")

    with pytest.raises(AuthRequired):
        await transport.send(SendRequest(text=PROMPT))
    assert stub.untrusted_marks

    result = await transport.send(SendRequest(text=PROMPT))

    assert result.text == "ok"
    assert stub.trusted_marks == 1


async def test_second_401_falls_through_to_auth_required_without_third_post(monkeypatch):
    fresh_a = _make_jwt()
    fresh_b = _make_jwt()
    stub = StubCodexAuth([fresh_a, fresh_b])
    session = FakeSession([FakeResponse(status_code=401), FakeResponse(status_code=401)])
    manager = FakeTokenManager()
    transport = CurlCffiTransport(manager, session=session, codex_auth=stub)
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    monkeypatch.setenv(ENV_AUTH_JSON, "/tmp/unused-auth.json")

    with pytest.raises(AuthRequired, match="rejected"):
        await transport.send(SendRequest(text=PROMPT))

    # Retried exactly once, then gave up through the existing 401 path.
    assert len(session.calls) == 2
    assert stub.invalidate_calls == 1
    assert stub.get_calls == 2
    assert manager.access_invalidations == 1


async def test_invalid_grant_after_401_propagates_dead_without_loop(monkeypatch):
    expired = _make_jwt(expires_in=-10)
    stub = StubCodexAuth(
        [expired], dead_reason="codex refresh rejected with HTTP 400 (invalid_grant)"
    )
    session = FakeSession([FakeResponse(status_code=401)])
    transport = CurlCffiTransport(
        FakeTokenManager(), session=session, codex_auth=stub
    )
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    monkeypatch.setenv(ENV_AUTH_JSON, "/tmp/unused-auth.json")

    with pytest.raises(CodexAuthDead, match="invalid_grant"):
        await transport.send(SendRequest(text=PROMPT))

    # One POST, one rotation attempt, then DEAD propagated — never a loop.
    assert len(session.calls) == 1
    assert stub.invalidate_calls == 1
    assert stub.get_calls == 2


async def test_dead_grant_before_first_post_fails_fast_with_zero_posts(monkeypatch):
    stub = DeadOnFirstCall("codex credential is DEAD — run codex login")
    session = FakeSession()
    transport = CurlCffiTransport(FakeTokenManager(), session=session, codex_auth=stub)
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    monkeypatch.setenv(ENV_AUTH_JSON, "/tmp/unused-auth.json")

    with pytest.raises(CodexAuthDead, match="codex login"):
        await transport.send(SendRequest(text=PROMPT))

    assert session.calls == []
