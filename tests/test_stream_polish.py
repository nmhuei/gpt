"""Targeted regression gates for the stream-polish bundle (2026-08-26).

Rows closed from docs/reports/parity-delta-audit-2026-08-26.md:
* PING-WIRE        -- idle heartbeats are canonical ``event: ping`` frames.
* JSON-DELTA-CHUNK -- tool_use ``partial_json`` streams in ~512-char chunks.
* OVERLOADED-529   -- overload-flagged RateLimited maps to HTTP 529
  (``overloaded_error``) while plain RateLimited stays 429.
* HEADER-PARITY    -- ``request-id`` + ``anthropic-ratelimit-*`` advisory
  response headers on every /v1/ response of both servers.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from gpt.api.protocol_adapters import parse_anthropic_request
from gpt.api.server import create_api_app as create_standalone_app
from gpt.gateway import create_api_app as create_gateway_app


def _messages_payload(**overrides: object) -> dict[str, object]:
    return {
        "model": "claude-code-local",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hello"}],
        **overrides,
    }


def _wedged_stream_server(app, monkeypatch):
    """Server whose backend generation never completes; short idle/deadline."""
    server = app.state.server
    server.stream_idle_seconds = 0.05
    server.stream_deadline_seconds = 0.3

    async def never_complete(_adapted, stream_callback=None):
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "_complete_anthropic", never_complete)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    return server, request


# ---------------------------------------------------------------------------
# PING-WIRE
# ---------------------------------------------------------------------------


async def test_gateway_idle_heartbeat_is_canonical_ping_event(monkeypatch):
    app = create_gateway_app()
    server, request = _wedged_stream_server(app, monkeypatch)
    adapted = parse_anthropic_request(_messages_payload(stream=True))

    events = [event async for event in server._anthropic_live_stream(request, adapted)]

    pings = [event for event in events if event.strip().startswith("event: ping")]
    assert pings, events
    for frame in pings:
        event_line, data_line = frame.strip().split("\n", 1)
        assert event_line == "event: ping"
        assert json.loads(data_line.removeprefix("data: ")) == {"type": "ping"}
    # The legacy SSE-comment heartbeat must be gone entirely.
    assert not any(event.strip() == ": ping" for event in events)


async def test_standalone_idle_heartbeat_is_canonical_ping_event(monkeypatch):
    app = create_standalone_app(headless=True)
    server, request = _wedged_stream_server(app, monkeypatch)
    adapted = parse_anthropic_request(_messages_payload(stream=True))

    events = [event async for event in server._anthropic_live_stream(request, adapted)]

    pings = [event for event in events if event.strip().startswith("event: ping")]
    assert pings, events
    for frame in pings:
        event_line, data_line = frame.strip().split("\n", 1)
        assert event_line == "event: ping"
        assert json.loads(data_line.removeprefix("data: ")) == {"type": "ping"}
    assert not any(event.strip() == ": ping" for event in events)


# ---------------------------------------------------------------------------
# JSON-DELTA-CHUNK
# ---------------------------------------------------------------------------


def _tool_use_payload(input_obj: dict[str, object]) -> dict[str, object]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-code-local",
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Write", "input": input_obj},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


async def _collect_partial_json(events: list[str]) -> list[str]:
    pieces = []
    for event in events:
        if not event.strip().startswith("event: content_block_delta"):
            continue
        if '"input_json_delta"' not in event:
            continue
        frame = json.loads(event.split("\ndata: ", 1)[1])
        assert frame["delta"]["type"] == "input_json_delta"
        pieces.append(frame["delta"]["partial_json"])
    return pieces


async def test_gateway_input_json_delta_streams_in_chunks():
    from gpt.gateway.server import _JSON_DELTA_CHUNK_CHARS, WebChatAPIServer

    big_text = "x" * 2000
    expected = json.dumps({"content": big_text}, ensure_ascii=False)
    assert len(expected) > _JSON_DELTA_CHUNK_CHARS
    events = [
        event
        async for event in WebChatAPIServer._anthropic_block_events(
            _tool_use_payload({"content": big_text})
        )
    ]
    pieces = await _collect_partial_json(events)

    assert len(pieces) > 1
    assert all(len(piece) <= _JSON_DELTA_CHUNK_CHARS for piece in pieces)
    # Reassembled client-side the chunks reproduce the exact same JSON.
    assert "".join(pieces) == expected


async def test_standalone_input_json_delta_streams_in_chunks():
    from gpt.api.server import _JSON_DELTA_CHUNK_CHARS, WebChatAPIServer

    big_text = "y" * 1500
    expected = json.dumps({"path": "/tmp/f", "content": big_text}, ensure_ascii=False)
    events = [
        event
        async for event in WebChatAPIServer._anthropic_content_events(
            _tool_use_payload({"path": "/tmp/f", "content": big_text})
        )
    ]
    pieces = await _collect_partial_json(events)

    assert len(pieces) > 1
    assert all(len(piece) <= _JSON_DELTA_CHUNK_CHARS for piece in pieces)
    assert "".join(pieces) == expected


async def test_small_tool_input_stays_single_frame():
    from gpt.api.server import WebChatAPIServer as StandaloneServer
    from gpt.gateway.server import WebChatAPIServer as GatewayServer

    payload = _tool_use_payload({"cmd": "ls"})
    gateway_events = [
        event async for event in GatewayServer._anthropic_block_events(payload)
    ]
    standalone_events = [
        event async for event in StandaloneServer._anthropic_content_events(payload)
    ]
    expected_piece = json.dumps({"cmd": "ls"}, ensure_ascii=False)
    assert await _collect_partial_json(gateway_events) == [expected_piece]
    assert await _collect_partial_json(standalone_events) == [expected_piece]


# ---------------------------------------------------------------------------
# OVERLOADED-529
# ---------------------------------------------------------------------------


async def test_overloaded_rate_limit_maps_to_529_both_servers():
    from gpt.api.server import WebChatAPIServer as StandaloneServer
    from gpt.gateway.server import WebChatAPIServer as GatewayServer
    from gpt.state import RateLimited

    for server_cls in (GatewayServer, StandaloneServer):
        # Message-marker path.
        mapped = server_cls._map_exception(
            RateLimited("backend is overloaded, try later")
        )
        assert mapped.status_code == 529, (server_cls, bytes(mapped.body))
        assert json.loads(bytes(mapped.body))["error"]["code"] == "overloaded_error"

        # Explicit attribute-flag path (no marker needed).
        flagged = RateLimited("ChatGPT Web request was rate limited.")
        flagged.overloaded = True  # type: ignore[attr-defined]
        assert server_cls._map_exception(flagged).status_code == 529

        # Plain RateLimited keeps the personal-quota mapping untouched.
        plain = server_cls._map_exception(
            RateLimited("ChatGPT anonymous quota exhausted; redirected to login wall.")
        )
        assert plain.status_code == 429, (server_cls, bytes(plain.body))
        assert json.loads(bytes(plain.body))["error"]["code"] == "rate_limit"


async def test_anthropic_error_envelope_uses_overloaded_error_type():
    from gpt.api.server import _anthropic_error as standalone_anthropic_error
    from gpt.api.server import _error as standalone_error
    from gpt.gateway.server import _anthropic_error as gateway_anthropic_error
    from gpt.gateway.server import _error as gateway_error

    for anthropic_error, error in (
        (gateway_anthropic_error, gateway_error),
        (standalone_anthropic_error, standalone_error),
    ):
        response = anthropic_error(error("backend is overloaded", 529, "overloaded_error"))
        assert response.status_code == 529
        assert json.loads(bytes(response.body)) == {
            "type": "error",
            "error": {"type": "overloaded_error", "message": "backend is overloaded"},
        }


# ---------------------------------------------------------------------------
# HEADER-PARITY
# ---------------------------------------------------------------------------


async def test_response_headers_request_id_and_advisory_ratelimit(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    import gpt.transport.breaker as breaker_mod

    monkeypatch.setenv("WEBGPT_RATELIMIT_COOLDOWN_SECONDS", "90")
    breaker_mod.reset_global_rate_limit_breaker()
    apps = [create_gateway_app(), create_standalone_app(headless=True)]
    try:
        # Breaker closed: full advisory budget, zero reset.
        for app in apps:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                response = await client.get("/v1/models")
                assert response.status_code == 200
                # request-id echoes the middleware's internal trace uuid.
                assert response.headers["request-id"].startswith("req_")
                assert response.headers["anthropic-ratelimit-requests-limit"] == "100"
                assert response.headers["anthropic-ratelimit-requests-remaining"] == "100"
                assert response.headers["anthropic-ratelimit-requests-reset"] == "0s"

        # Breaker tripped: advisory remaining reads exhausted until reset.
        breaker_mod.global_rate_limit_breaker().trip()
        for app in apps:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                throttled = await client.get("/v1/models")
                assert throttled.status_code == 200
                assert throttled.headers["anthropic-ratelimit-requests-limit"] == "100"
                assert throttled.headers["anthropic-ratelimit-requests-remaining"] == "0"
                reset_seconds = int(
                    throttled.headers["anthropic-ratelimit-requests-reset"].removesuffix("s")
                )
                assert 1 <= reset_seconds <= 90
                # Every response carries its own fresh trace uuid.
                assert (
                    throttled.headers["request-id"].startswith("req_")
                    and throttled.headers["request-id"] != response.headers["request-id"]
                )
    finally:
        # Never leak a tripped global breaker into unrelated suites.
        breaker_mod.reset_global_rate_limit_breaker()
