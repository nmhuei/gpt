"""PORT-F-CONV-RECIPE — authed /f/conversation prepare chain (fake-session).

Covers: local bootstrapProof self-solve (+cache), SHA3-512 PoW solver, the
15-field prepare body, conduit-token header wiring, and the OFF-by-default
guarantee that legacy send() behavior is untouched.
Spec: docs/reports/f-conversation-recipe-fields.md
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import uuid
from unittest.mock import AsyncMock

import pytest

from gpt.state import AuthRequired, ProtocolChanged
from gpt.transport import token_manager as tm
from gpt.transport.curl_transport import (
    _FCONV_PREPARE_NOTOKEN,
    _FCONV_PREPARE_URL,
    _SENTINEL_CLASSIC_URL,
    _SENTINEL_PREPARE_URL,
    CONVERSATION_URL,
    CurlCffiTransport,
)
from gpt.transport.token_manager import (
    BOOTSTRAP_PROOF_PREFIX,
    PROOF_TOKEN_PREFIX,
    SENTINEL_PROOF_MAX_ATTEMPTS,
    SentinelPowExhausted,
    SentinelTokens,
    TokenBundle,
    TokenManager,
    build_fconv_prepare_body,
    encode_sentinel_config,
    solve_sentinel_pow,
)
from gpt.types import ModelInfo, SendRequest

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FconvTest/1.0"
DEVICE = "11111111-2222-3333-4444-555555555555"
CONVERSATION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

PREPARE_15_FIELDS = {
    "action",
    "client_contextual_info",
    "client_prepare_dispatch",
    "client_prepare_source",
    "client_prepare_state",
    "conversation_mode",
    "local_function_names",
    "model",
    "parent_message_id",
    "supported_encodings",
    "supports_buffering",
    "system_hints",
    "timezone",
    "timezone_offset_min",
    "conversation_id",  # conditional: present when continuing a conversation
}


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FlowTokenManager:
    """Token-manager double exposing exactly what the fconv flow touches."""

    def __init__(self) -> None:
        self.bootstrap_calls: list[tuple[str, str]] = []
        self.sentinel_calls = 0
        # Review round 12: fconv authenticates with the Bearer bundle, so its
        # 401/403 handling must hit invalidate_access_token (not just the
        # sentinel cache the fconv branch never uses).
        self.access_invalidations = 0
        self.sentinel_invalidations = 0

    async def refresh_if_needed(self) -> TokenBundle:
        return TokenBundle(
            access_token="access-token",
            cookies={"cf_clearance": "clearance"},
            cf_clearance="clearance",
            oai_device_id=DEVICE,
        )

    async def get_sentinel_tokens(self, conversation_id: str | None) -> SentinelTokens:
        self.sentinel_calls += 1
        return SentinelTokens("requirements", "proof", "turnstile")

    async def bootstrap_proof_token(self, user_agent: str, device_id: str) -> str:
        self.bootstrap_calls.append((user_agent, device_id))
        return BOOTSTRAP_PROOF_PREFIX + base64.b64encode(b'["fake"]').decode()

    def invalidate_sentinel(self) -> None:
        self.sentinel_invalidations += 1

    def invalidate_access_token(self) -> None:
        self.access_invalidations += 1


class FakeJSONResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def aclose(self):
        pass


class FakeSSEResponse:
    status_code = 200

    async def aiter_bytes(self):
        yield (
            b'data: {"conversation_id":"' + CONVERSATION.encode()
            + b'","message":{"id":"turn-fconv","content":{"parts":["Hello"]},'
              b'"status":"finished_successfully"}}\n\n'
        )

    async def aclose(self):
        pass


class RoutingSession:
    """session.post dispatching on exact URL: JSON map or the SSE response."""

    def __init__(self, json_by_url=None):
        self.json_by_url = json_by_url or {}
        self.sse_response = FakeSSEResponse()
        # (url, headers, json-body) for every POST, in order.
        self.calls: list[tuple[str, dict, dict]] = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs.get("headers") or {}, kwargs.get("json") or {}))
        entry = self.json_by_url.get(url)
        if entry is None:
            return self.sse_response
        payload, status = entry
        return FakeJSONResponse(payload, status)


def _sse_call(session: RoutingSession) -> tuple[str, dict]:
    for url, headers, _body in session.calls:
        if url == CONVERSATION_URL:
            return url, headers
    raise AssertionError("no SSE conversation call was made")


def _calls_to(session: RoutingSession, url: str) -> list[tuple[dict, dict]]:
    return [(headers, body) for c_url, headers, body in session.calls if c_url == url]


# ---------------------------------------------------------------------------
# (a) bootstrapProof — prefix, fingerprint shape, 10-minute cache
# ---------------------------------------------------------------------------


async def test_bootstrap_proof_prefix_fingerprint_and_cache_hit(monkeypatch):
    manager = TokenManager(AsyncMock())
    counter = {"solve": 0}
    real_solve = tm.solve_sentinel_pow

    def counting(seed, difficulty, ua, dev, **kwargs):
        counter["solve"] += 1
        return real_solve(seed, difficulty, ua, dev, **kwargs)

    monkeypatch.setattr(tm, "solve_sentinel_pow", counting)

    first = await manager.bootstrap_proof_token(UA, DEVICE)

    assert first.startswith("gAAAAAC")
    assert first.startswith(BOOTSTRAP_PROOF_PREFIX)
    encoded = first[len(BOOTSTRAP_PROOF_PREFIX):]
    raw = base64.b64decode(encoded).decode("utf-8")
    config = json.loads(raw)

    assert len(config) == 18
    # Bootstrap difficulty "0" matches ~1/16 of hashes, so the counter is
    # usually nonzero — assert budget bounds rather than a fixed value.
    assert isinstance(config[3], int) and 0 <= config[3] < SENTINEL_PROOF_MAX_ATTEMPTS
    assert config[4] == UA                      # [4] UA matches header identity
    assert config[6] == tm._SENTINEL_BUILD_HASH  # [6] prod-a696… build hash
    assert config[10] == (
        "webkitGetUserMedia−function webkitGetUserMedia() { [native code] }"  # noqa: RUF001
    )
    assert "−" in raw                      # noqa: RUF001 - raw UTF-8 minus sign rides along
    assert config[14] == DEVICE                 # [14] device id matches header
    # compact JSON: no spaces after separators
    assert ", " not in raw and '": ' not in raw

    # A different seed must not trigger a second solve inside the TTL.
    monkeypatch.setattr(tm.random, "random", lambda: 0.999999)
    second = await manager.bootstrap_proof_token(UA, DEVICE)
    assert second == first
    assert counter["solve"] == 1

    # Identity change invalidates the cache entry.
    await manager.bootstrap_proof_token(UA + "-x", DEVICE)
    assert counter["solve"] == 2

    # Expiry forces a re-solve too.
    proof, cached_ua, cached_dev, expire_at = manager._bootstrap_cache
    manager._bootstrap_cache = (proof, cached_ua, cached_dev, expire_at - 601)
    await manager.bootstrap_proof_token(UA + "-x", DEVICE)
    assert counter["solve"] == 3


def test_bootstrap_pow_fallback_shape():
    fallback = tm.bootstrap_pow_fallback("0.500000")
    assert fallback.startswith("wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D")
    assert base64.b64decode(fallback[len("wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"):]) == b'"0.500000"'


# ---------------------------------------------------------------------------
# (b) PoW solver — solution within budget, lexicographic check, exhaustion
# ---------------------------------------------------------------------------


def test_pow_solver_finds_solution_within_budget():
    seed = "0.654321"
    answer = solve_sentinel_pow(seed, "e", UA, DEVICE)

    raw = base64.b64decode(answer).decode("utf-8")
    config = json.loads(raw)
    assert 0 <= config[3] < SENTINEL_PROOF_MAX_ATTEMPTS

    digest = hashlib.sha3_512((seed + answer).encode()).hexdigest()
    assert digest[:1] <= "e"                    # lexicographic hex-prefix check

    # Integer-valued floats serialize without ".0" (Go shortest form).
    assert ".0," not in raw and ".0]" not in raw
    assert encode_sentinel_config(config) == answer  # stable round-trip


def test_pow_solver_exhaustion_raises():
    with pytest.raises(SentinelPowExhausted):
        solve_sentinel_pow("0.111111", "", UA, DEVICE)  # empty never matches
    with pytest.raises(SentinelPowExhausted):
        solve_sentinel_pow("0.111111", "00", UA, DEVICE, max_attempts=0)


def test_sentinel_date_string_shape():
    ts = 1756083789.0
    text = tm.sentinel_date_str(ts)
    assert re.match(
        r"^[A-Z][a-z]{2} [A-Z][a-z]{2} \d{2} \d{4} \d{2}:\d{2}:\d{2} GMT[+-]\d{4}$",
        text,
    )
    local = time.localtime(ts)
    assert text.startswith(tm._DATE_DAYS[local.tm_wday] + " ")
    offset_minutes = tm.local_utc_offset_minutes(ts)
    gmt = text.split("GMT", 1)[1]
    assert int(gmt[1:3]) * 60 + int(gmt[3:5]) == abs(offset_minutes)


def test_resolve_timezone_env_override(monkeypatch):
    monkeypatch.setenv("WEBGPT_TIMEZONE", "Asia/Ho_Chi_Minh")
    assert tm.resolve_local_timezone() == "Asia/Ho_Chi_Minh"


# ---------------------------------------------------------------------------
# (c) prepare flow — sentinel POST shape + full 15-field prepare body
# ---------------------------------------------------------------------------


async def test_prepare_body_carries_all_15_fields(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: ({"token": "req-token"}, 200),
        _FCONV_PREPARE_URL: ({"conduit_token": "c" * 350, "status": "ok"}, 200),
    })
    transport = CurlCffiTransport(FlowTokenManager(), session=session)
    request = SendRequest(
        text="Hello",
        conversation_id=CONVERSATION,
        model=ModelInfo(id="gpt-test", label="GPT Test"),
    )

    result = await transport.send(request)

    assert result.status == "completed"
    sentinel_posts = _calls_to(session, _SENTINEL_PREPARE_URL)
    assert len(sentinel_posts) == 1
    headers, body = sentinel_posts[0]
    # Exactly one field "p"; no sentinel headers on this step.
    assert list(body.keys()) == ["p"]
    assert body["p"].startswith(BOOTSTRAP_PROOF_PREFIX)
    assert "OpenAI-Sentinel" not in "".join(headers)
    assert headers["Authorization"] == "Bearer access-token"
    assert headers["OAI-Device-Id"] == DEVICE

    prepare_posts = _calls_to(session, _FCONV_PREPARE_URL)
    assert len(prepare_posts) == 1
    body = prepare_posts[0][1]
    assert set(body) == PREPARE_15_FIELDS
    assert len(body) == 15
    assert body["action"] == "next"
    assert body["model"] == "gpt-test"
    assert body["parent_message_id"] == "client-created-root"
    assert body["client_prepare_state"] == "none"
    assert body["client_prepare_dispatch"] == "conversation"
    assert body["client_prepare_source"] == "chatgpt_web_client"
    assert body["conversation_mode"] == {"kind": "primary_assistant"}
    assert body["client_contextual_info"] == {
        "app_name": "chatgpt.com",
        "has_web_push_capabilities": False,
        "web_push_notification_permission": "default",
    }
    assert body["local_function_names"] == []
    assert body["supported_encodings"] == ["v1"]
    assert body["supports_buffering"] is True
    assert body["system_hints"] == []
    assert isinstance(body["timezone_offset_min"], int)
    assert body["timezone_offset_min"] == tm.local_utc_offset_minutes()
    assert body["conversation_id"] == CONVERSATION


def test_prepare_body_drops_conversation_id_for_new_conversation():
    body = build_fconv_prepare_body(timezone_name="UTC", timezone_offset_min=0)
    assert set(body) == PREPARE_15_FIELDS - {"conversation_id"}
    assert len(body) == 14


async def test_sentinel_prepare_falls_back_to_classic_once(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: ({"error": "gone"}, 404),
        _SENTINEL_CLASSIC_URL: ({"token": "classic-token"}, 200),
        _FCONV_PREPARE_URL: ({"conduit_token": "c" * 350}, 200),
    })
    transport = CurlCffiTransport(FlowTokenManager(), session=session)

    await transport.send(SendRequest(text="Hello", conversation_id=CONVERSATION))

    classic = _calls_to(session, _SENTINEL_CLASSIC_URL)
    assert len(classic) == 1
    _url, sse_headers, _body = session.calls[-1]
    assert (
        sse_headers["openai-sentinel-chat-requirements-token"] == "classic-token"
    )


async def test_prepare_stage_token_uses_prepare_header_name(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: ({"prepare_token": "prep-token"}, 200),
        _FCONV_PREPARE_URL: ({"conduit_token": "c" * 350}, 200),
    })
    transport = CurlCffiTransport(FlowTokenManager(), session=session)

    await transport.send(SendRequest(text="Hello", conversation_id=CONVERSATION))

    _, sse_headers = _sse_call(session)
    assert (
        sse_headers["openai-sentinel-chat-requirements-prepare-token"]
        == "prep-token"
    )
    assert "openai-sentinel-chat-requirements-token" not in sse_headers


async def test_pow_required_solves_and_sets_gaaaaab_header(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: (
            {
                "token": "req-token",
                "proofofwork": {"required": True, "seed": "0.246810", "difficulty": "f"},
            },
            200,
        ),
        _FCONV_PREPARE_URL: ({"conduit_token": "c" * 350}, 200),
    })
    transport = CurlCffiTransport(FlowTokenManager(), session=session)

    await transport.send(SendRequest(text="Hello", conversation_id=CONVERSATION))

    _, sse_headers = _sse_call(session)
    proof = sse_headers["openai-sentinel-proof-token"]
    assert proof.startswith(PROOF_TOKEN_PREFIX)
    digest = hashlib.sha3_512(("0.246810" + proof[len(PROOF_TOKEN_PREFIX):]).encode()).hexdigest()
    assert digest[:1] <= "f"


# ---------------------------------------------------------------------------
# (d) conduit token rides X-Conduit-Token with one shared identity
# ---------------------------------------------------------------------------


async def test_conduit_token_rides_final_headers_with_shared_identity(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    conduit = "x" * 350
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: ({"token": "req-token"}, 200),
        _FCONV_PREPARE_URL: ({"conduit_token": conduit, "status": "ok"}, 200),
    })
    fake = FlowTokenManager()
    transport = CurlCffiTransport(fake, session=session)
    request = SendRequest(
        text="Hello",
        conversation_id=CONVERSATION,
        model=ModelInfo(id="gpt-test", label="GPT Test"),
    )

    await transport.send(request)

    assert fake.bootstrap_calls == [(CurlCffiTransport._envelope_user_agent(), DEVICE)]
    _, sse_headers = _sse_call(session)
    assert sse_headers["X-Conduit-Token"] == conduit
    assert uuid.UUID(sse_headers["oai-session-id"])
    assert uuid.UUID(sse_headers["x-oai-turn-trace-id"])
    assert sse_headers["Accept-Language"] == "en-US,en;q=0.9"
    assert sse_headers["Cache-Control"] == "no-cache"
    assert sse_headers["Pragma"] == "no-cache"

    # One identity across all three calls of the turn (sentinel comment:
    # UA / Device-Id / Session-Id must match requirements → prepare → SSE).
    assert len(session.calls) == 3
    user_agents = {headers.get("User-Agent") for _u, headers, _b in session.calls}
    assert len(user_agents) == 1
    devices = {
        headers.get("OAI-Device-Id", headers.get("oai-device-id"))
        for _u, headers, _b in session.calls
    }
    assert devices == {DEVICE}
    sessions = {
        headers.get("OAI-Session-Id", headers.get("oai-session-id"))
        for _u, headers, _b in session.calls
    }
    traces = {
        headers.get("X-OAI-Turn-Trace-Id", headers.get("x-oai-turn-trace-id"))
        for _u, headers, _b in session.calls
    }
    assert len(sessions) == 1 and len(traces) == 1
    assert sessions == {sse_headers["oai-session-id"]}
    assert traces == {sse_headers["x-oai-turn-trace-id"]}


async def test_conduit_failure_is_non_fatal(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: ({"token": "req-token"}, 200),
        _FCONV_PREPARE_URL: ({"error": "nope"}, 500),
    })
    transport = CurlCffiTransport(FlowTokenManager(), session=session)

    result = await transport.send(SendRequest(text="Hello", conversation_id=CONVERSATION))

    assert result.text == "Hello"
    _, sse_headers = _sse_call(session)
    assert "X-Conduit-Token" not in sse_headers
    assert sse_headers["openai-sentinel-chat-requirements-token"] == "req-token"


async def test_missing_requirements_token_aborts_flow(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: ({"unrelated": 1}, 200),
    })
    transport = CurlCffiTransport(FlowTokenManager(), session=session)

    from gpt.state import ProtocolChanged

    with pytest.raises(ProtocolChanged, match="no requirements token"):
        await transport.send(SendRequest(text="Hello", conversation_id=CONVERSATION))
    assert all(url != CONVERSATION_URL for url, _h, _b in session.calls)


# ---------------------------------------------------------------------------
# (d2) review round 12 — Bearer-bundle invalidation + off-loop PoW
# ---------------------------------------------------------------------------


async def test_prepare_auth_rejection_invalidates_bearer_bundle(monkeypatch):
    """A 401 on both sentinel prepare endpoints drops the Bearer bundle."""
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: ({"error": "unauthorized"}, 401),
        _SENTINEL_CLASSIC_URL: ({"error": "unauthorized"}, 401),
    })
    fake = FlowTokenManager()
    transport = CurlCffiTransport(fake, session=session)

    with pytest.raises(ProtocolChanged, match="prepare failed"):
        await transport.send(SendRequest(text="Hello", conversation_id=CONVERSATION))

    # The rejected credential snapshot must never be re-served from cache.
    assert fake.access_invalidations == 1


async def test_sse_rejection_invalidates_bearer_bundle_on_fconv_path(monkeypatch):
    """SSE-stage 403 under fconv hits the access-token cache, not sentinel."""
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: ({"token": "req-token"}, 200),
        CONVERSATION_URL: ({"error": "forbidden"}, 403),
    })
    fake = FlowTokenManager()
    transport = CurlCffiTransport(fake, session=session)

    with pytest.raises(AuthRequired, match="rejected"):
        await transport.send(SendRequest(text="Hello", conversation_id=CONVERSATION))

    assert fake.access_invalidations == 1
    # The fconv branch mints fresh per turn and never reads the sentinel
    # cache, so dropping it here would be a no-op misdirecting the fix.
    assert fake.sentinel_invalidations == 0


# ---------------------------------------------------------------------------
# (d3) codex review round 13 — conduit auth rejection + header-build target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_conduit_auth_rejection_invalidates_bearer_but_stays_non_fatal(
    monkeypatch, status
):
    """Conduit-stage 401/403 drops the Bearer cache yet the turn completes.

    Round 13: the failure path swallowed auth rejections without touching the
    credential cache, so the SSE request re-sent the exact bearer the server
    had just refused on the prepare call.
    """
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: ({"token": "req-token"}, 200),
        _FCONV_PREPARE_URL: ({"error": "unauthorized"}, status),
    })
    fake = FlowTokenManager()
    transport = CurlCffiTransport(fake, session=session)

    result = await transport.send(
        SendRequest(text="Hello", conversation_id=CONVERSATION)
    )

    assert result.text == "Hello"  # non-fatal continue preserved
    _, sse_headers = _sse_call(session)
    assert "X-Conduit-Token" not in sse_headers
    assert fake.access_invalidations == 1   # refused bearer dropped...
    assert fake.sentinel_invalidations == 0  # ...not the unused sentinel cache


class ClearancelessFlowTokenManager(FlowTokenManager):
    """Same flow, but the minted snapshot carries no cf_clearance."""

    async def refresh_if_needed(self) -> TokenBundle:
        bundle = await super().refresh_if_needed()
        return TokenBundle(
            access_token=bundle.access_token,
            cookies={},
            cf_clearance=None,
            oai_device_id=bundle.oai_device_id,
        )


async def test_header_build_failure_on_fconv_invalidates_bearer_not_sentinel(
    monkeypatch,
):
    """Header-build AuthRequired under fconv hits the Bearer-bundle cache.

    Round 13: this path branched only on ``codex`` and dropped the sentinel
    cache for fconv — a cache the fconv envelope never reads — while leaving
    the rejected Bearer bundle cached until refresh expiry.
    """
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: ({"token": "req-token"}, 200),
    })
    fake = ClearancelessFlowTokenManager()
    transport = CurlCffiTransport(fake, session=session)

    with pytest.raises(AuthRequired, match="cf_clearance"):
        await transport.send(SendRequest(text="Hello", conversation_id=CONVERSATION))

    assert fake.access_invalidations == 1    # what fconv authenticates with
    assert fake.sentinel_invalidations == 0  # wrong cache before round 13


async def test_pow_solves_off_the_event_loop_thread(monkeypatch):
    """solve_sentinel_pow runs on a worker thread, not the running loop."""
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    loop_thread = threading.get_ident()
    solver_threads: list[int] = []

    real_encode = tm.encode_sentinel_config
    real_fingerprint = tm.sentinel_fingerprint

    def fake_solve(seed, difficulty, ua, dev, **kwargs):
        solver_threads.append(threading.get_ident())
        return real_encode(real_fingerprint(ua, dev, time.time()))

    monkeypatch.setattr(
        "gpt.transport.curl_transport.solve_sentinel_pow", fake_solve
    )
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: (
            {
                "token": "req-token",
                "proofofwork": {"required": True, "seed": "0.246810",
                                "difficulty": "f"},
            },
            200,
        ),
        _FCONV_PREPARE_URL: ({"conduit_token": "c" * 350}, 200),
    })
    transport = CurlCffiTransport(FlowTokenManager(), session=session)

    result = await transport.send(SendRequest(text="Hello", conversation_id=CONVERSATION))

    assert result.status == "completed"
    assert solver_threads and all(t != loop_thread for t in solver_threads)
    _, sse_headers = _sse_call(session)
    proof = sse_headers["openai-sentinel-proof-token"]
    assert proof.startswith(PROOF_TOKEN_PREFIX)
    answer = proof[len(PROOF_TOKEN_PREFIX):]
    digest = hashlib.sha3_512(("0.246810" + answer).encode()).hexdigest()
    assert digest[:1] <= "f"


# ---------------------------------------------------------------------------
# (e) flag OFF — legacy behavior byte-for-byte
# ---------------------------------------------------------------------------


async def test_flag_off_keeps_legacy_behavior(monkeypatch):
    monkeypatch.delenv("WEBGPT_FCONV_PREPARE", raising=False)
    fake = FlowTokenManager()
    session = RoutingSession()  # any stray prepare POST would land here visibly
    transport = CurlCffiTransport(fake, session=session)
    request = SendRequest(
        text="Hello",
        conversation_id=CONVERSATION,
        model=ModelInfo(id="gpt-test", label="GPT Test"),
    )

    result = await transport.send(request)

    assert result.text == "Hello"
    # Exactly one HTTP call: the conversation POST itself.
    assert [url for url, _h, _b in session.calls] == [CONVERSATION_URL]
    assert fake.bootstrap_calls == []          # no bootstrap solve happened
    assert fake.sentinel_calls == 1            # classic in-page mint path ran
    _url, headers = _sse_call(session)
    assert headers["openai-sentinel-chat-requirements-token"] == "requirements"
    for absent in (
        "X-Conduit-Token",
        "oai-session-id",
        "x-oai-turn-trace-id",
        "Accept-Language",
        "Cache-Control",
        "Pragma",
    ):
        assert absent not in headers


# ---------------------------------------------------------------------------
# (f) FCONV-NOTOKEN-REPLAY — literal X-Conduit-Token: no-token on the prepare
#     call only (kymuco PR #40/#41 marker; research 2026-08-26 §Q(a))
# ---------------------------------------------------------------------------


async def test_prepare_request_carries_notoken_marker_header(monkeypatch):
    """Flag ON: the prepare POST opens the handshake with 'no-token'."""
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    conduit = "x" * 350
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: ({"token": "req-token"}, 200),
        _FCONV_PREPARE_URL: ({"conduit_token": conduit, "status": "ok"}, 200),
    })
    transport = CurlCffiTransport(FlowTokenManager(), session=session)

    await transport.send(SendRequest(text="Hello", conversation_id=CONVERSATION))

    # Prepare call: exact literal marker from the kymuco observation.
    prepare_headers = _calls_to(session, _FCONV_PREPARE_URL)[0][0]
    assert prepare_headers["X-Conduit-Token"] == "no-token"
    assert _FCONV_PREPARE_NOTOKEN == "no-token"
    # Sentinel stage runs BEFORE any conduit exists — no marker there.
    sentinel_headers = _calls_to(session, _SENTINEL_PREPARE_URL)[0][0]
    assert "X-Conduit-Token" not in sentinel_headers
    # Final SSE envelope carries the REAL token, never the literal marker.
    _, sse_headers = _sse_call(session)
    assert sse_headers["X-Conduit-Token"] == conduit
    assert sse_headers["X-Conduit-Token"] != _FCONV_PREPARE_NOTOKEN


async def test_prepare_marker_sent_even_when_prepare_fails(monkeypatch):
    """The marker rides the request regardless of the response outcome."""
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    session = RoutingSession({
        _SENTINEL_PREPARE_URL: ({"token": "req-token"}, 200),
        _FCONV_PREPARE_URL: ({"error": "nope"}, 500),
    })
    transport = CurlCffiTransport(FlowTokenManager(), session=session)

    result = await transport.send(SendRequest(text="Hello", conversation_id=CONVERSATION))

    assert result.text == "Hello"  # non-fatal continue unchanged
    prepare_headers = _calls_to(session, _FCONV_PREPARE_URL)[0][0]
    assert prepare_headers["X-Conduit-Token"] == "no-token"
    _, sse_headers = _sse_call(session)
    assert "X-Conduit-Token" not in sse_headers  # no real token -> header absent


async def test_flag_off_never_sends_prepare_or_marker(monkeypatch):
    """Flag OFF: no prepare call at all and no marker anywhere outbound."""
    monkeypatch.delenv("WEBGPT_FCONV_PREPARE", raising=False)
    session = RoutingSession()
    transport = CurlCffiTransport(FlowTokenManager(), session=session)

    await transport.send(SendRequest(text="Hello", conversation_id=CONVERSATION))

    assert [url for url, _h, _b in session.calls] == [CONVERSATION_URL]
    for _url, headers, body in session.calls:
        assert "X-Conduit-Token" not in headers
        assert "no-token" not in json.dumps(body)
