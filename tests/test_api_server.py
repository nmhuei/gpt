import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from gpt.api.server import create_api_app
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
    mock_session.new_conversation.assert_awaited_once()
    mock_session.send.assert_awaited_once()
