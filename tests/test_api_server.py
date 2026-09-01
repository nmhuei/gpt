import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from gpt.api.protocol_adapters import parse_anthropic_request
from gpt.api.server import create_api_app
from gpt.state import BrowserDisconnected, ConversationConflict, EmptyModelResponse
from gpt.types import TurnResult


def test_api_server_health_and_models():
    app = create_api_app(headless=True)
    client = TestClient(app)

    # 1. Healthz
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # 2. Models
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert any(m["id"] == "chatgpt-web" for m in data)


@pytest.mark.anyio
async def test_chat_completions_tool_call_flow(monkeypatch):
    app = create_api_app(headless=True)

    # Mock the canonical session boundary, not its UI implementation.
    mock_turn_result = TurnResult(
        turn_id="turn_test123",
        conversation_id="conv_123",
        text='<WEBGPT_TOOL_CALL>\n{"name":"get_weather","arguments":{"location":"Hanoi"}}\n</WEBGPT_TOOL_CALL>',
        status="completed",
        duration_ms=1200,
    )

    mock_send = AsyncMock(return_value=mock_turn_result)
    server_instance = app.routes[0].endpoint.__self__
    mock_session = MagicMock()
    mock_session.new_conversation = AsyncMock()
    mock_session.send = mock_send
    mock_session.drain_events = MagicMock(return_value=[])
    mock_session.conversation_id = None

    monkeypatch.setattr(server_instance, "get_or_create_session", AsyncMock(return_value=mock_session))

    client = TestClient(app)

    request_payload = {
        "model": "chatgpt-web",
        "messages": [{"role": "user", "content": "Thời tiết ở Hà Nội thế nào?"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Lấy thông tin thời tiết",
                    "parameters": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                    },
                },
            }
        ],
    }

    resp = client.post("/v1/chat/completions", json=request_payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["choices"][0]["finish_reason"] == "tool_calls"
    tool_calls = data["choices"][0]["message"]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"location": "Hanoi"}
    # Freshly booted blank sessions should not pay an extra New Chat click.
    mock_session.new_conversation.assert_not_awaited()
    mock_session.send.assert_awaited_once()


@pytest.mark.anyio
async def test_gateway_resolves_configured_model_alias_once_before_sending(monkeypatch):
    app = create_api_app(model_aliases={"coding": "GPT Coding"})
    server = app.state.server
    session = MagicMock()
    session.new_conversation = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="turn_alias",
            conversation_id="conv_alias",
            text="done",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "coding",
                "messages": [{"role": "user", "content": "write code"}],
                "reasoning_effort": "high",
            },
        )

    assert response.status_code == 200
    assert response.json()["model"] == "coding"
    session.select_model.assert_awaited_once_with("GPT Coding")
    session.select_reasoning_effort.assert_awaited_once_with("high")
    session.send.assert_awaited_once()
    assert len(session.send.await_args.args) == 1


@pytest.mark.anyio
async def test_commit_unknown_retry_reconciles_persisted_turn_without_resend(monkeypatch):
    from gpt.state import CommitUnknown
    from gpt.types import ReconciliationResult

    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = "conv_uncertain"
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        side_effect=CommitUnknown(
            "submitted but completion unknown",
            conversation_id="conv_uncertain",
        )
    )
    session.reconcile = AsyncMock(
        return_value=ReconciliationResult(
            user_turn_present=True,
            assistant_text="recovered answer",
            conversation_id="conv_uncertain",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    payload = {
        "model": "chatgpt-web",
        "messages": [{"role": "user", "content": "exactly-once marker"}],
    }
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", json=payload)
        assert first.status_code == 409
        assert first.json()["error"]["code"] == "commit_unknown"

        second = client.post("/v1/chat/completions", json=payload)

    assert second.status_code == 200
    assert second.json()["choices"][0]["message"]["content"] == "recovered answer"
    assert session.send.await_count == 1
    session.reconcile.assert_awaited_once()


@pytest.mark.anyio
async def test_commit_unknown_retry_resends_once_only_after_history_proves_absence(monkeypatch):
    from gpt.state import CommitUnknown
    from gpt.types import ReconciliationResult

    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = "conv_absent"
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        side_effect=[
            CommitUnknown(
                "submitted but completion unknown",
                conversation_id="conv_absent",
            ),
            TurnResult(
                turn_id="turn_retry",
                conversation_id="conv_absent",
                text="safe retry answer",
            ),
        ]
    )
    session.reconcile = AsyncMock(
        return_value=ReconciliationResult(
            user_turn_present=False,
            assistant_text=None,
            conversation_id="conv_absent",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    payload = {
        "model": "chatgpt-web",
        "messages": [{"role": "user", "content": "absence marker"}],
    }
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", json=payload)
        assert first.status_code == 409
        second = client.post("/v1/chat/completions", json=payload)

    assert second.status_code == 200
    assert second.json()["choices"][0]["message"]["content"] == "safe retry answer"
    assert session.send.await_count == 2
    session.reconcile.assert_awaited_once()


@pytest.mark.anyio
async def test_server_max_workers_path_leases_factory_session(monkeypatch):
    from contextlib import asynccontextmanager

    from gpt.api.requests import parse_chat_completion_request

    app = create_api_app(max_workers=2, warm_workers=1)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="turn_pool",
            conversation_id="conv_pool",
            text="pooled answer",
        )
    )

    class FakeFactory:
        def __init__(self):
            self.lease_count = 0
            self.close = AsyncMock()

        @asynccontextmanager
        async def lease(self):
            self.lease_count += 1
            yield session

    fake_factory = FakeFactory()
    server._worker_factory = fake_factory

    normalized = parse_chat_completion_request(
        {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": "pool marker"}],
        }
    )
    response, record = await server.complete_normalized(normalized)

    assert response["choices"][0]["message"]["content"] == "pooled answer"
    assert record.conversation_id == "conv_pool"
    assert fake_factory.lease_count == 1
    session.send.assert_awaited_once()
    await server.close()


def test_healthz_is_pure_liveness_and_does_not_boot_browser():
    app = create_api_app(headless=True)
    server = app.state.server
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "ok"}
    assert server._session is None


@pytest.mark.anyio
async def test_readyz_checks_a_real_session_lease_without_sending(monkeypatch):
    from contextlib import asynccontextmanager

    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.state.value = "ready"
    session.browser_manager.connected = True
    session.ui_driver.auth_status = AsyncMock(return_value="anonymous")

    @asynccontextmanager
    async def lease():
        yield session

    monkeypatch.setattr(server, "_lease_session", lease)
    request = MagicMock()
    response = await server.readiness(request)
    assert response.status_code == 200
    payload = json.loads(bytes(response.body))
    assert payload["ready"] is True
    assert payload["auth_status"] == "anonymous"
    assert not hasattr(session, "send") or session.send.call_count == 0


@pytest.mark.anyio
async def test_two_logical_sessions_use_distinct_factory_workers_without_cross_routing():
    import asyncio
    from contextlib import asynccontextmanager

    from gpt.api.requests import parse_chat_completion_request

    app = create_api_app(max_workers=2, warm_workers=2)
    server = app.state.server

    class RoutedSession:
        def __init__(self, name):
            self.name = name
            self.conversation_id = None
            self.new_conversation = AsyncMock()
            self.open = AsyncMock()
            self.select_model = AsyncMock()
            self.select_reasoning_effort = AsyncMock()

        async def send(self, prompt, timeout_seconds=None):
            await asyncio.sleep(0.02)
            marker = "alpha" if "alpha" in prompt else "beta"
            self.conversation_id = f"conv_{marker}"
            return TurnResult(
                turn_id=f"turn_{marker}",
                conversation_id=self.conversation_id,
                text=f"answer_{marker}_via_{self.name}",
            )

    sessions = [RoutedSession("worker_a"), RoutedSession("worker_b")]

    class FakeFactory:
        def __init__(self):
            self.next_index = 0
            self.active = 0
            self.peak = 0

        @asynccontextmanager
        async def lease(self):
            session = sessions[self.next_index]
            self.next_index += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                yield session
            finally:
                self.active -= 1

    factory = FakeFactory()
    server._worker_factory = factory

    alpha = parse_chat_completion_request(
        {"model": "chatgpt-web", "messages": [{"role": "user", "content": "alpha"}]}
    )
    beta = parse_chat_completion_request(
        {"model": "chatgpt-web", "messages": [{"role": "user", "content": "beta"}]}
    )
    (alpha_result, alpha_record), (beta_result, beta_record) = await asyncio.gather(
        server.complete_normalized(alpha),
        server.complete_normalized(beta),
    )

    assert factory.peak == 2
    assert alpha_record.session_id != beta_record.session_id
    assert alpha_record.conversation_id == "conv_alpha"
    assert beta_record.conversation_id == "conv_beta"
    assert "alpha" in alpha_result["choices"][0]["message"]["content"]
    assert "beta" in beta_result["choices"][0]["message"]["content"]
    assert "beta" not in alpha_result["choices"][0]["message"]["content"]
    assert "alpha" not in beta_result["choices"][0]["message"]["content"]


def _parse_openai_sse(body: str):
    events = []
    done = 0
    for block in body.split("\n\n"):
        if not block.startswith("data: "):
            continue
        payload = block[6:]
        if payload == "[DONE]":
            done += 1
        else:
            events.append(json.loads(payload))
    return events, done


def test_openai_stream_contract_has_role_stable_id_finish_and_single_done(monkeypatch):
    app = create_api_app()
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="stream_text",
            conversation_id="conv_stream_text",
            text="stream contract text",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "stream"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    events, done = _parse_openai_sse(response.text)
    assert done == 1
    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    ids = {event["id"] for event in events}
    assert len(ids) == 1
    content = "".join(
        event["choices"][0]["delta"].get("content", "") for event in events
    )
    assert content == "stream contract text"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["choices"][0]["delta"] == {}


def test_openai_tool_stream_never_leaks_sentinel_and_finishes_separately(monkeypatch):
    app = create_api_app()
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    raw = (
        '<WEBGPT_TOOL_CALL>\n{"name":"lookup","arguments":{"q":"x"}}\n'
        "</WEBGPT_TOOL_CALL>"
    )
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="stream_tool",
            conversation_id="conv_stream_tool",
            text=raw,
        )
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            },
        }
    ]
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "lookup"}],
                "tools": tools,
                "stream": True,
            },
        )

    events, done = _parse_openai_sse(response.text)
    assert done == 1
    serialized = response.text
    assert "WEBGPT_TOOL_CALL" not in serialized
    tool_events = [
        event
        for event in events
        if event["choices"][0]["delta"].get("tool_calls")
    ]
    assert len(tool_events) == 1
    assert tool_events[0]["choices"][0]["finish_reason"] is None
    assert tool_events[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "lookup"
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert events[-1]["choices"][0]["delta"] == {}


@pytest.mark.anyio
async def test_lifespan_prewarm_failure_keeps_app_startable(monkeypatch):
    from starlette.testclient import TestClient

    app = create_api_app(prewarm=True)
    server = app.state.server
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(side_effect=RuntimeError("warmup transient")))

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert any(
        event.component == "webchat" and event.kind == "prewarm_failed"
        for event in server.trace.snapshot()
    )


@pytest.mark.anyio
async def test_multiple_tool_calls_are_corrected_to_single_invoke(monkeypatch):
    # CODEX#8: pin the strict single-invoke contract explicitly -- the P1-3
    # default (WEBGPT_MAX_TOOL_CALLS_PER_TURN=3) would otherwise admit the
    # double invoke without any correction.
    monkeypatch.setenv("WEBGPT_MAX_TOOL_CALLS_PER_TURN", "1")
    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        side_effect=[
            TurnResult(
                turn_id="turn_many",
                conversation_id="conv_many",
                text=(
                    '<tool_calls><invoke name="get_weather"><parameter name="location"><![CDATA[Hanoi]]></parameter>'
                    '</invoke><invoke name="get_weather"><parameter name="location"><![CDATA[Hue]]></parameter>'
                    '</invoke></tool_calls>'
                ),
            ),
            TurnResult(
                turn_id="turn_one",
                conversation_id="conv_many",
                text='<tool_calls><invoke name="get_weather"><parameter name="location"><![CDATA[Hanoi]]></parameter></invoke></tool_calls>',
            ),
        ]
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "call one tool at a time"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "parameters": {
                                "type": "object",
                                "properties": {"location": {"type": "string"}},
                                "required": ["location"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
            },
        )

    assert response.status_code == 200
    data = response.json()
    calls = data["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"location": "Hanoi"}
    assert session.send.await_count == 2
    assert any(
        event.component == "completionruntime"
        and event.kind == "tool_correction"
        and event.metadata.get("reason") == "MULTI_TOOL"
        and event.metadata.get("correction_index") == 1
        for event in server.trace.snapshot()
    )


@pytest.mark.anyio
async def test_server_passes_configured_generation_timeout_to_session(monkeypatch):
    app = create_api_app(headless=True, generation_timeout_seconds=7.5)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="turn_timeout_cfg",
            conversation_id="conv_timeout_cfg",
            text="configured timeout ok",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "chatgpt-web", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    session.send.assert_awaited_once()
    assert session.send.await_args.kwargs["timeout_seconds"] == 7.5


@pytest.mark.anyio
async def test_tool_correction_budget_is_bounded_and_returns_structured_error(monkeypatch):
    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    refusal = TurnResult(
        turn_id="turn_refusal",
        conversation_id="conv_refusal",
        text="I cannot execute that tool because tools are unavailable.",
    )
    session.send = AsyncMock(side_effect=[refusal, refusal, refusal])
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "Use Bash to run pwd"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "Bash",
                            "parameters": {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                                "required": ["command"],
                            },
                        },
                    }
                ],
            },
        )

    # R5-FIX: an identical adjacent hard refusal now raises early after 2 sends
    # (persistent_tool_failure) instead of draining the full budget.
    assert response.status_code == 502
    payload = response.json()["error"]
    assert payload["code"] == "malformed_model_tool_call"
    assert payload["retryable"] is False
    assert "persistent" in payload["message"].lower()
    assert session.send.await_count == 2
    corrections = [
        event
        for event in server.trace.snapshot()
        if event.component == "completionruntime" and event.kind == "tool_correction"
    ]
    assert [event.metadata["correction_index"] for event in corrections] == [1]


def test_lifespan_shutdown_is_bounded(monkeypatch):
    app = create_api_app(headless=True)

    async def slow_close():
        await asyncio.sleep(60)

    app.state.server.close = slow_close
    monkeypatch.setenv("WEBGPT_SERVER_CLOSE_TIMEOUT", "0.01")

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# R4-DOUBLING: the Anthropic SDK retries 408/409/429/5xx responses and
# connection errors, and Claude Code's outer loop re-POSTs after stream
# failures. Each retry is a fresh POST and therefore a duplicate ChatGPT Web
# generation, so the gateway must never emit a retryable failure signal.
# ---------------------------------------------------------------------------


def _anthropic_stream_payload(**overrides):
    payload = {
        "model": "chatgpt-web",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }
    payload.update(overrides)
    return payload


def test_anthropic_pre_stream_error_is_non_retryable_http_error():
    """(a) Errors before the stream opens must use a status/header pair the
    SDK refuses to retry (400 + x-should-retry: false), not an SSE error."""
    app = create_api_app(headless=True)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages", json=_anthropic_stream_payload(messages=[])
        )

    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    # The SDK consults x-should-retry before any status heuristic; false is
    # the authoritative no-retry signal for Claude Code's client.
    assert response.headers["x-should-retry"] == "false"


@pytest.mark.anyio
async def test_anthropic_pre_content_error_surfaces_standard_error_event(monkeypatch):
    """(b) A failure after message_start but BEFORE any content was delivered
    surfaces a standard Anthropic ``event: error`` (LATE-FAIL-SURFACE):
    nothing was sent, so an explicit error loses nothing and stops the CLI
    from treating a dead turn as complete. Only failures after content has
    streamed take the R4-DOUBLING clean-close path."""
    app = create_api_app(headless=True)
    server = app.state.server

    async def fail(_adapted, stream_callback=None):
        raise ConversationConflict(
            "tool_call_id does not match the pending assistant tool call."
        )

    monkeypatch.setattr(server, "_complete_anthropic", fail)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    adapted = parse_anthropic_request(_anthropic_stream_payload())

    events = [event async for event in server._anthropic_live_stream(request, adapted)]

    assert events[0].startswith("event: message_start")
    error_events = [e for e in events if e.startswith("event: error")]
    assert len(error_events) == 1, events
    # The terminal frame is the error event itself: no clean terminator and
    # no reason-text block may follow it.
    assert events[-1].startswith("event: error"), events[-1]
    payload = json.loads(error_events[0].split("\ndata: ", 1)[1])
    assert payload["type"] == "error"
    assert set(payload["error"]) == {"type", "message"}
    assert payload["error"]["type"] in {
        "api_error",
        "overloaded_error",
        "rate_limit_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
        "invalid_request_error",
        "request_too_large",
    }
    assert "tool_call_id" in payload["error"]["message"]
    assert not any(e.startswith("event: content_block") for e in events), events
    assert not any(
        e.startswith("event: message_delta") or e.startswith("event: message_stop")
        for e in events
    ), events
    tail = events[events.index(events[-1]) :]
    assert all(
        event.strip() != ": ping" and not event.strip().startswith("event: ping")
        for event in tail
    )
    # Nothing was masked -- nothing had been delivered yet.
    assert server.late_failure_masked == 0


@pytest.mark.anyio
async def test_anthropic_mid_stream_error_after_deltas_appends_reason_and_stops(monkeypatch):
    """(b2) With a content block already open, the reason is appended to that
    block -- no second block, no error event, stream ends as a clean turn."""
    app = create_api_app(headless=True)
    server = app.state.server

    async def stream_partial_then_fail(_adapted, stream_callback=None):
        assert stream_callback is not None
        await stream_callback("partial answer ")
        await stream_callback("text")
        raise EmptyModelResponse("upstream produced nothing usable")

    monkeypatch.setattr(server, "_complete_anthropic", stream_partial_then_fail)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    adapted = parse_anthropic_request(_anthropic_stream_payload())

    events = [event async for event in server._anthropic_live_stream(request, adapted)]

    assert events[0].startswith("event: message_start")
    assert not any(event.startswith("event: error") for event in events), events
    assert events[-1].startswith("event: message_stop")
    deltas = [
        json.loads(event.split("\ndata: ", 1)[1])
        for event in events
        if event.startswith("event: content_block_delta")
    ]
    streamed_text = "".join(d["delta"]["text"] for d in deltas)
    assert streamed_text.startswith("partial answer text")
    assert "\n\n[webgpt-gateway:" in streamed_text
    starts = [e for e in events if e.startswith("event: content_block_start")]
    stops = [e for e in events if e.startswith("event: content_block_stop")]
    assert len(starts) == len(stops) == 1
    # LATE-FAIL-SURFACE: masking the truncated turn behind the clean close is
    # counted, so the frequency stays observable even though the wire behavior
    # (R4-DOUBLING) is unchanged.
    assert server.late_failure_masked == 1


def test_anthropic_retryable_infra_error_keeps_sdk_retry_contract(monkeypatch):
    """(c) Regression: genuinely transient infrastructure failures keep their
    retryable wire contract (503 + x-should-retry: true) so the SDK can still
    recover them. Non-streaming only: streaming failures after message_start
    are closed as completed turns by design (see R4-DOUBLING tests above)."""
    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.send = AsyncMock(side_effect=BrowserDisconnected("Chromium died mid-turn"))
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={k: v for k, v in _anthropic_stream_payload().items() if k != "stream"},
        )

    assert response.status_code == 503
    body = response.json()
    assert body["type"] == "error"
    assert response.headers["x-should-retry"] == "true"


# ----------------------------------------------------------------------
# P2-server: per-conversation lock cleanup, bounded response-session LRU,
# and multi-account lease wiring (parity with the gateway/server.py
# leak-fix wave; see tests/test_server_leakfix.py).
# ----------------------------------------------------------------------


def _p2_mock_request(content: str):
    from gpt.requests import parse_chat_completion_request

    return parse_chat_completion_request(
        {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": content}],
        },
        protocol="openai_chat",
        client="test",
    )


@pytest.mark.anyio
async def test_conversation_locks_cleaned_after_repeated_turns():
    """Lock entries must not accumulate one-per-conversation forever."""
    from contextlib import asynccontextmanager

    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="turn_p2",
            conversation_id="conv_p2",
            text="p2 answer",
        )
    )

    class FakeFactory:
        @asynccontextmanager
        async def lease(self):
            yield session

    server._worker_factory = FakeFactory()

    for index in range(200):
        _, record = await server.complete_normalized(
            _p2_mock_request(f"distinct p2 question {index}")
        )
        assert record.session_id

    assert len(server._conversation_locks) <= 3


@pytest.mark.anyio
async def test_conversation_lock_serializes_and_then_cleans_up():
    app = create_api_app(headless=True)
    server = app.state.server
    order: list[str] = []

    async def worker(name: str) -> None:
        async with server._conversation_lock("shared-p2-conversation"):
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


def test_response_sessions_default_cap_is_512(monkeypatch):
    from gpt.api.server import DEFAULT_RESPONSE_SESSION_CAP

    monkeypatch.delenv("WEBGPT_RESPONSE_SESSION_CAP", raising=False)
    server = create_api_app(headless=True).state.server
    assert server._response_session_cap == DEFAULT_RESPONSE_SESSION_CAP == 512


def test_response_sessions_lru_eviction_respects_env_cap(monkeypatch):
    monkeypatch.setenv("WEBGPT_RESPONSE_SESSION_CAP", "64")
    server = create_api_app(headless=True).state.server
    for index in range(600):
        server._remember_response_session(f"resp_{index}", f"session_{index}")

    assert len(server._response_sessions) <= 64
    assert "resp_0" not in server._response_sessions
    assert "resp_599" in server._response_sessions


def test_response_sessions_touch_refreshes_recency(monkeypatch):
    monkeypatch.setenv("WEBGPT_RESPONSE_SESSION_CAP", "8")
    server = create_api_app(headless=True).state.server
    for index in range(8):
        server._remember_response_session(f"resp_{index}", f"session_{index}")

    assert server._lookup_response_session("resp_0") == "session_0"
    server._remember_response_session("resp_8", "session_8")

    assert len(server._response_sessions) <= 8
    assert "resp_0" in server._response_sessions
    assert "resp_1" not in server._response_sessions
    assert server._lookup_response_session("missing") is None


class _FakeApiAccountStore:
    names = ("alpha", "beta")
    registered_default: str | None = None

    def list(self):
        from types import SimpleNamespace

        return [SimpleNamespace(name=name) for name in self.names]

    def get_default(self):
        return self.registered_default


def _p2_account_profiles_server():
    return create_api_app(
        headless=True,
        account_profiles={"alpha": "/tmp/wg-alpha", "beta": "/tmp/wg-beta"},
    ).state.server


@pytest.mark.anyio
async def test_factory_receives_default_name_from_env_override(monkeypatch):
    from gpt.transport.multi_account import MultiAccountWorkerFactory

    monkeypatch.setattr("gpt.api.server.AccountStore", _FakeApiAccountStore)
    monkeypatch.setenv("WEBGPT_DEFAULT_ACCOUNT", "beta")

    server = _p2_account_profiles_server()

    assert isinstance(server._worker_factory, MultiAccountWorkerFactory)
    assert server._worker_factory.default_name == "beta"


@pytest.mark.anyio
async def test_factory_default_name_falls_back_to_registry(monkeypatch):
    from gpt.transport.multi_account import MultiAccountWorkerFactory

    class _RegistryDefaultStore(_FakeApiAccountStore):
        def get_default(self):
            return "alpha"

    monkeypatch.setattr("gpt.api.server.AccountStore", _RegistryDefaultStore)
    monkeypatch.delenv("WEBGPT_DEFAULT_ACCOUNT", raising=False)

    server = _p2_account_profiles_server()

    assert isinstance(server._worker_factory, MultiAccountWorkerFactory)
    assert server._worker_factory.default_name == "alpha"


@pytest.mark.anyio
async def test_health_flag_enables_tracker_on_factory(monkeypatch):
    monkeypatch.setattr("gpt.api.server.AccountStore", _FakeApiAccountStore)
    monkeypatch.delenv("WEBGPT_DEFAULT_ACCOUNT", raising=False)
    monkeypatch.setenv("WEBGPT_HEALTH_CHECK_ENABLED", "1")

    server = _p2_account_profiles_server()

    assert server._account_health_tracker is not None
    assert server._worker_factory.health is server._account_health_tracker
    await server.close()
    assert server._health_loop_task is None


@pytest.mark.anyio
async def test_health_flag_off_keeps_tracker_none(monkeypatch):
    monkeypatch.setattr("gpt.api.server.AccountStore", _FakeApiAccountStore)
    monkeypatch.delenv("WEBGPT_DEFAULT_ACCOUNT", raising=False)
    monkeypatch.delenv("WEBGPT_HEALTH_CHECK_ENABLED", raising=False)

    server = _p2_account_profiles_server()

    assert server._account_health_tracker is None
    assert server._worker_factory.health is None


@pytest.mark.anyio
async def test_multi_account_lease_pins_record_and_avoids_cooldown_worker():
    """Unhealthy default is avoided and the picked account sticks to record."""
    from contextlib import asynccontextmanager

    from gpt.conversations import ConversationRecord
    from gpt.transport.account_health import AccountHealthTracker
    from gpt.transport.multi_account import MultiAccountWorkerFactory

    server = create_api_app(headless=True).state.server
    from types import SimpleNamespace

    sessions = {"alpha": SimpleNamespace(), "beta": SimpleNamespace()}

    class LeafFactory:
        def __init__(self, name: str):
            self.name = name

        @asynccontextmanager
        async def lease(self):
            yield sessions[self.name]

    tracker = AccountHealthTracker()
    server._worker_factory = MultiAccountWorkerFactory(
        {"alpha": LeafFactory("alpha"), "beta": LeafFactory("beta")},
        health=tracker,
        default_name="alpha",
    )

    # Healthy sticky default wins for an unpinned record.
    record = ConversationRecord()
    async with server._lease_session(record) as leased:
        assert leased is sessions["alpha"]
        assert record.account_name == "alpha"

    # Once the default is cooling down, the pool must avoid it and pin beta.
    tracker.mark_result("alpha", ok=False, cooldown_seconds=900)
    record2 = ConversationRecord()
    async with server._lease_session(record2) as leased2:
        assert leased2 is sessions["beta"]
        assert record2.account_name == "beta"


@pytest.mark.anyio
async def test_multi_account_lease_honours_explicit_record_pin():
    from contextlib import asynccontextmanager

    from gpt.conversations import ConversationRecord
    from gpt.transport.multi_account import MultiAccountWorkerFactory

    server = create_api_app(headless=True).state.server
    from types import SimpleNamespace

    sessions = {"alpha": SimpleNamespace(), "beta": SimpleNamespace()}

    class LeafFactory:
        def __init__(self, name: str):
            self.name = name

        @asynccontextmanager
        async def lease(self):
            yield sessions[self.name]

    server._worker_factory = MultiAccountWorkerFactory(
        {"alpha": LeafFactory("alpha"), "beta": LeafFactory("beta")},
        default_name="alpha",
    )

    record = ConversationRecord(account_name="beta")
    async with server._lease_session(record) as leased:
        assert leased is sessions["beta"]
        assert record.account_name == "beta"


# ---------------------------------------------------------------------------
# PARITY-P0-1: Anthropic usage estimation wiring.  Claude Code's auto-compact
# never triggered because every usage figure on the /v1/messages path was
# zero; these tests pin the chars/4 estimate (gpt/api/protocol_adapters.py)
# onto the non-stream response, the SSE stream envelopes, and the envelope
# shape itself.
# ---------------------------------------------------------------------------


def _p01_prompt_chars() -> int:
    return len("x" * 400)


def _p01_anthropic_payload(**overrides):
    body = {
        "model": "chatgpt-web",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "x" * _p01_prompt_chars()}],
    }
    body.update(overrides)
    return body


def _p01_completion(text: str) -> dict:
    """OpenAI-shaped completion whose only output is ``text``."""
    return {
        "model": "chatgpt-web",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text, "tool_calls": None},
            }
        ],
    }


@pytest.mark.anyio
async def test_anthropic_non_stream_usage_is_estimated_not_zero(monkeypatch):
    """(a) Non-stream /v1/messages responses carry input/output estimates that
    match ceil(chars/4) over the fully rendered prompt (FIX-CODEX7) and the
    completion text."""
    from gpt.api.protocol_adapters import estimate_anthropic_input_tokens

    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.drain_events = MagicMock(return_value=[])
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="turn_p01",
            conversation_id="conv_p01",
            text="y" * 200,
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={k: v for k, v in _p01_anthropic_payload().items() if k != "stream"},
        )

    assert response.status_code == 200
    body = response.json()
    # (c) regression: still a valid Anthropic message envelope.
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["stop_reason"] == "end_turn"
    assert body["content"][0]["type"] == "text"
    usage = body["usage"]
    # Render-based contract: same formula as count_tokens over the rendered turn.
    assert usage["input_tokens"] == estimate_anthropic_input_tokens(
        _p01_anthropic_payload()
    ) > 0
    assert usage["output_tokens"] == -(-200 // 4) > 0
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0


@pytest.mark.anyio
async def test_anthropic_stream_message_delta_accumulates_output_usage(monkeypatch):
    """(b) The SSE stream reports the prompt estimate at message_start and a
    monotonically accumulated (never zero) output estimate at message_delta."""
    from gpt.api.protocol_adapters import (
        estimate_rendered_input_tokens,
        response_to_anthropic,
    )

    app = create_api_app(headless=True)
    server = app.state.server

    async def stream_two_chunks(_adapted, stream_callback=None):
        assert stream_callback is not None
        await stream_callback("z" * 100)
        await stream_callback("z" * 100)
        payload = response_to_anthropic(
            _p01_completion("z" * 200),
            prompt_text="x" * _p01_prompt_chars(),
        )
        return payload, MagicMock()

    monkeypatch.setattr(server, "_complete_anthropic", stream_two_chunks)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    adapted = parse_anthropic_request(_p01_anthropic_payload(stream=True))

    events = [event async for event in server._anthropic_live_stream(request, adapted)]

    def data_of(event: str) -> dict:
        return json.loads(event.split("\ndata: ", 1)[1])

    start = data_of(events[0])
    assert events[0].startswith("event: message_start")
    # Input estimate is present from the first envelope (rendered prompt)...
    assert start["message"]["usage"]["input_tokens"] == estimate_rendered_input_tokens(
        adapted.request
    )
    # ...and output starts at zero before any delta.
    assert start["message"]["usage"]["output_tokens"] == 0
    deltas = [
        data_of(e)["delta"]["text"]
        for e in events
        if e.startswith("event: content_block_delta")
    ]
    assert "".join(deltas) == "z" * 200
    final_delta_event = next(
        e for e in reversed(events) if e.startswith("event: message_delta")
    )
    usage = data_of(final_delta_event)["usage"]
    # Accumulated output matches ceil(chars/4); the old wiring emitted 0.
    assert usage["output_tokens"] == -(-200 // 4)
    assert usage["output_tokens"] > 0
    assert usage["input_tokens"] == estimate_rendered_input_tokens(adapted.request)
    # (c) regression: terminator sequence stays well-formed.
    assert final_delta_event.index('"stop_reason": "end_turn"') > 0
    assert '"stop_sequence": null' in final_delta_event
    assert events[-1].startswith("event: message_stop")


# ---------------------------------------------------------------------------
# OPENAI-USAGE-WIRE: /v1/chat/completions emits a locally estimated usage
# object (chars/4, PARITY-P0-1) instead of 0/0 chunks and ``"usage": null``.
# ---------------------------------------------------------------------------


def _usage_wire_app(monkeypatch, text):
    app = create_api_app()
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="usage_wire",
            conversation_id="conv_usage_wire",
            text=text,
        )
    )
    monkeypatch.setattr(
        server, "get_or_create_session", AsyncMock(return_value=session)
    )
    return app


def _expected_openai_usage(prompt_text: str, content: str) -> dict:
    from gpt.api.protocol_adapters import (
        estimate_text_chars_to_tokens,
        estimate_tokens_from_chars,
    )

    prompt_tokens = estimate_tokens_from_chars(len(prompt_text))
    completion_tokens = estimate_text_chars_to_tokens(len(content))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def test_openai_non_stream_response_reports_estimated_usage(monkeypatch):
    """(a) Non-stream OpenAI responses carry a chars/4 usage object > 0."""
    content = "u" * 200
    app = _usage_wire_app(monkeypatch, content)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "usage wire"}],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    expected = _expected_openai_usage("usage wire", content)
    assert data["usage"] is not None
    assert data["usage"]["prompt_tokens"] > 0
    assert data["usage"]["completion_tokens"] > 0
    assert data["usage"] == expected


def test_openai_stream_final_chunk_carries_total_usage(monkeypatch):
    """(b) With include_usage the final chunk totals prompt + completion."""
    content = "s" * 200
    app = _usage_wire_app(monkeypatch, content)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "usage wire"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
    assert resp.status_code == 200
    events, done = _parse_openai_sse(resp.text)
    assert done == 1
    usage_events = [event for event in events if event.get("usage")]
    assert len(usage_events) == 1
    assert usage_events[0] is events[-1]
    assert usage_events[0]["usage"] == _expected_openai_usage("usage wire", content)


def test_openai_usage_shape_matches_openai_standard(monkeypatch):
    """(c) Regression: usage keeps the standard OpenAI field set on both paths."""
    content = "h" * 40
    app = _usage_wire_app(monkeypatch, content)
    with TestClient(app) as client:
        non_stream = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "shape"}],
            },
        )
        streamed = client.post(
            "/v1/chat/completions",
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "shape"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
    assert non_stream.status_code == 200
    assert streamed.status_code == 200

    data = non_stream.json()
    assert set(data) == {
        "id",
        "object",
        "created",
        "model",
        "system_fingerprint",
        "choices",
        "usage",
    }
    assert data["object"] == "chat.completion"
    assert data["id"].startswith("chatcmpl-")
    assert set(data["choices"][0]) == {"index", "message", "finish_reason"}
    usage = data["usage"]
    assert set(usage) == {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert all(isinstance(value, int) for value in usage.values())
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    events, done = _parse_openai_sse(streamed.text)
    assert done == 1
    usage_event = next(event for event in events if event.get("usage"))
    assert usage_event["object"] == "chat.completion.chunk"
    assert usage_event["choices"] == []
    assert len({event["id"] for event in events}) == 1  # stable completion id
    chunk_usage = usage_event["usage"]
    assert set(chunk_usage) == {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert all(isinstance(value, int) for value in chunk_usage.values())


# ---------------------------------------------------------------------------
# CODEX-FIX-3: even an error-only turn reports a full Anthropic usage object.
# The streamed reason text is counted into the chars/4 estimator before the
# terminal ``message_delta`` snapshot -- input from the prompt estimate,
# output > 0 -- never a bare zeroed stub.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_anthropic_no_retry_close_reports_full_usage_object():
    """_anthropic_no_retry_close counts the reason text before the terminal
    message_delta so the usage object carries input and output tokens."""
    from gpt.api.protocol_adapters import (
        StreamUsageEstimator,
        estimate_tokens_from_chars,
    )

    app = create_api_app(headless=True)
    server = app.state.server
    error_payload = {
        "type": "error",
        "error": {"type": "api_error", "message": "m" * 80},
    }

    events = [
        event
        async for event in server._anthropic_no_retry_close(
            error_payload,
            started_content=False,
            estimator=StreamUsageEstimator("x" * _p01_prompt_chars()),
        )
    ]

    def data_of(event: str) -> dict:
        return json.loads(event.split("\ndata: ", 1)[1])

    joined = "".join(events)
    # The reason still reaches the CLI verbatim inside the message text...
    assert "[webgpt-gateway:api_error]" in joined
    assert "m" * 80 in joined
    # ...and the terminator sequence stays well-formed.
    assert events[-1].startswith("event: message_stop")
    final_delta = next(
        e for e in reversed(events) if e.startswith("event: message_delta")
    )
    usage = data_of(final_delta)["usage"]
    # Full Anthropic schema, not a bare zeroed stub.
    assert set(usage) == {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
    assert usage["input_tokens"] == estimate_tokens_from_chars(_p01_prompt_chars()) > 0
    # Output is ceil(chars/4) over the exact emitted reason text, never 0.
    expected_text = "[webgpt-gateway:api_error] " + "m" * 80
    assert usage["output_tokens"] == -(-len(expected_text) // 4) > 0


@pytest.mark.anyio
async def test_anthropic_live_stream_error_reports_output_usage(monkeypatch):
    """A failure AFTER content was streamed ends with a terminal
    message_delta whose output_tokens counts the streamed text plus the
    reason text (> 0); pre-content failures surface an SSE error event with
    no usage instead (see LATE-FAIL-SURFACE)."""
    from gpt.api.protocol_adapters import estimate_rendered_input_tokens

    app = create_api_app(headless=True)
    server = app.state.server

    async def stream_partial_then_fail(_adapted, stream_callback=None):
        await stream_callback("partial ")
        raise ConversationConflict(
            "tool_call_id does not match the pending assistant tool call."
        )

    monkeypatch.setattr(server, "_complete_anthropic", stream_partial_then_fail)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    adapted = parse_anthropic_request(
        _anthropic_stream_payload(
            messages=[{"role": "user", "content": "x" * _p01_prompt_chars()}]
        )
    )

    events = [event async for event in server._anthropic_live_stream(request, adapted)]

    assert not any(event.startswith("event: error") for event in events), events
    assert events[-1].startswith("event: message_stop")
    final_delta = next(
        e for e in reversed(events) if e.startswith("event: message_delta")
    )
    usage = json.loads(final_delta.split("\ndata: ", 1)[1])["usage"]
    assert set(usage) == {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
    # Error-only turn: input estimate comes from the rendered client prompt...
    assert usage["input_tokens"] == estimate_rendered_input_tokens(
        adapted.request
    ) > 0
    # ...and output counts the streamed reason text instead of staying 0.
    assert usage["output_tokens"] > 0
    # The truncated turn was masked behind the clean close: counted.
    assert server.late_failure_masked == 1


@pytest.mark.anyio
async def test_anthropic_stream_error_event_coerces_unknown_type():
    """LATE-FAIL-SURFACE helper: types outside the Anthropic contract (e.g.
    generation_timeout) collapse to api_error so every SDK build can parse
    the ``event: error`` frame."""
    app = create_api_app(headless=True)
    server = app.state.server

    events = [
        event
        async for event in server._anthropic_stream_error_event(
            {
                "type": "error",
                "error": {"type": "generation_timeout", "message": "boom"},
            }
        )
    ]

    assert len(events) == 1
    payload = json.loads(events[0].split("\ndata: ", 1)[1])
    assert payload["type"] == "error"
    assert payload["error"] == {"type": "api_error", "message": "boom"}

    known = [
        event
        async for event in server._anthropic_stream_error_event(
            {
                "type": "error",
                "error": {"type": "overloaded_error", "message": "hot path"},
            }
        )
    ]
    payload = json.loads(known[0].split("\ndata: ", 1)[1])
    assert payload["error"] == {"type": "overloaded_error", "message": "hot path"}


# ---------------------------------------------------------------------------
# CODEX-FIX-7 (review-batch-evening-7): usage-contract alignment with
# count_tokens, floor-to-one for known-empty prompts, and turn_id on
# failure-path traces.
# ---------------------------------------------------------------------------


def _cf7_completion(content: str | None) -> dict:
    """OpenAI-shaped completion whose only output is ``content``."""
    return {
        "model": "chatgpt-web",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": None},
            }
        ],
    }


def test_non_stream_known_empty_prompt_floors_at_one_input_token():
    """(a) Floor-to-one: a valid non-stream envelope whose prompt is
    KNOWN-EMPTY ("") reports input_tokens=1 exactly like the streaming
    estimator; only an unobserved prompt (None) stays at 0."""
    from gpt.api.protocol_adapters import response_to_anthropic

    empty_prompt = response_to_anthropic(_cf7_completion(""), prompt_text="")
    assert empty_prompt["usage"]["input_tokens"] == 1

    unknown_prompt = response_to_anthropic(_cf7_completion(""), prompt_text=None)
    assert unknown_prompt["usage"]["input_tokens"] == 0


def test_non_stream_empty_content_request_reports_nonzero_input_tokens(monkeypatch):
    """(a) End to end: a valid non-stream request with empty user content
    never regresses to the pre-fix input_tokens=0."""
    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.drain_events = MagicMock(return_value=[])
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="turn_cf7_empty",
            conversation_id="conv_cf7_empty",
            text="ok",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        resp = client.post(
            "/v1/messages",
            json={
                "model": "chatgpt-web",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": ""}],
            },
        )

    assert resp.status_code == 200
    usage = resp.json()["usage"]
    assert usage["input_tokens"] >= 1


def test_count_tokens_and_usage_input_agree_for_same_request(monkeypatch):
    """(b) USAGE-CONTRACT-ALIGN: count_tokens and the /v1/messages usage
    input estimate share the rendered-prompt chars/4 formula, so identical
    payloads agree within a small tolerance on both sides (scaffolding
    included everywhere, not raw message content only)."""
    body = {
        "model": "chatgpt-web",
        "max_tokens": 128,
        "system": "You are a careful controller.",
        "tools": [
            {
                "name": "browser_snapshot",
                "description": "Take an accessibility snapshot.",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        "messages": [{"role": "user", "content": "hello"}],
    }
    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.drain_events = MagicMock(return_value=[])
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="turn_cf7_align",
            conversation_id="conv_cf7_align",
            text="done",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        counted = client.post("/v1/messages/count_tokens", json=body)
        used = client.post("/v1/messages", json=body)

    assert counted.status_code == 200
    assert used.status_code == 200
    counted_tokens = counted.json()["input_tokens"]
    usage_tokens = used.json()["usage"]["input_tokens"]
    assert counted_tokens >= 1
    assert usage_tokens >= 1
    # Same request -> same rendered prompt -> at most ceil-rounding drift.
    assert abs(counted_tokens - usage_tokens) <= 1


def test_failure_trace_event_carries_turn_id(monkeypatch):
    """(c) TURN-ID-FAILURE-TRACE: the terminal request_completed trace of a
    failed turn carries the runtime turn id reported by
    submit_failed_before_commit_unknown instead of turn_id=None."""
    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.drain_events = MagicMock(return_value=[])
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="turn_cf7_fail",
            conversation_id="conv_cf7_fail",
            text="",  # web response landed but is empty -> EmptyModelResponse
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        resp = client.post(
            "/v1/messages",
            json={
                "model": "chatgpt-web",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert resp.status_code == 502
    # /v1/messages errors use the Anthropic error envelope.
    assert resp.json()["error"]["type"] == "api_error"

    completed = [
        event
        for event in server.trace.snapshot()
        if event.component == "api" and event.kind == "request_completed"
    ]
    assert completed, "middleware must emit a terminal request_completed event"
    meta = completed[-1].metadata
    assert meta["http_status"] == 502
    assert meta["status"] == "error"
    assert meta["turn_id"] == "turn_cf7_fail"


def test_anti_repeat_abort_request_telemetry_matches_terminal_count(monkeypatch):
    """CORRECTION-TELEMETRY-PARITY (codex14 #1): ``request_completed``
    correction telemetry must match the runtime's terminal spend.

    Runtime emits ``tool_correction`` BEFORE the anti-repeat check and rolls
    its own pre-check increment back when the abort fires, so counting raw
    events overcounted by exactly the number of aborted attempts.  The
    middleware must instead trust the terminal metadata (submit_failed_
    before_commit_unknown here carries the net value).
    """
    monkeypatch.setenv("WEBGPT_MAX_CORRECTIONS", "3")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    app = create_api_app(headless=True)
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.drain_events = MagicMock(return_value=[])
    # Deterministic prose claim -> byte-identical correction prompts every
    # round, which trips the CORRECTION-TIGHTEN escalation then fail-fast.
    claim_text = "The fizzbuzz script works exactly as requested."
    session.send = AsyncMock(
        side_effect=[
            TurnResult(
                turn_id=f"turn_parity_{n}",
                conversation_id="conv_parity",
                text=claim_text,
            )
            for n in range(3)
        ]
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        resp = client.post(
            "/v1/messages",
            json={
                "model": "chatgpt-web",
                "max_tokens": 16,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "<system-reminder>\nWorking directory: /tmp/cc-parity\n"
                            "</system-reminder>\n\n"
                            "# Task\n"
                            "Create a python script `fizzbuzz.py` printing 1..15,\n"
                            "run it, and write the output to `output.txt`.\n"
                        ),
                    }
                ],
                "tools": [
                    {
                        "name": "Bash",
                        "description": "Run a shell command.",
                        "input_schema": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    }
                ],
            },
        )

    assert resp.status_code == 502
    assert session.send.await_count == 3

    snapshot = server.trace.snapshot()
    raw_corrections = [
        event
        for event in snapshot
        if event.component == "completionruntime" and event.kind == "tool_correction"
    ]
    repeats = [
        event
        for event in snapshot
        if event.component == "completionruntime"
        and event.kind == "persistent_correction_repeat"
    ]
    # Raw event stream still shows the aborted third attempt...
    assert [event.metadata["correction_index"] for event in raw_corrections] == [1, 2, 3]
    assert len(repeats) == 1

    completed = [
        event
        for event in snapshot
        if event.component == "api" and event.kind == "request_completed"
    ]
    assert completed, "middleware must emit a terminal request_completed event"
    meta = completed[-1].metadata
    assert meta["http_status"] == 502
    assert meta["status"] == "error"
    # ...but request-level telemetry reports the NET terminal spend of two
    # actually-sent corrections, matching submit_failed_before_commit_unknown.
    assert meta["correction_count"] == 2
