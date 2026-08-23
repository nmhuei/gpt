from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from gpt.api.protocol_adapters import parse_anthropic_request, parse_responses_request
from gpt.api.server import create_api_app
from gpt.state import RateLimited
from gpt.types import TurnResult


def _fake_session(text: str = "hello") -> MagicMock:
    session = MagicMock()
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(turn_id="turn_1", conversation_id="conv_1", text=text)
    )
    session.conversation_id = None
    return session


def test_anthropic_rate_limit_does_not_advertise_retry(monkeypatch):
    app = create_api_app()
    server = app.state.server
    session = _fake_session()
    session.send = AsyncMock(side_effect=RateLimited("free quota reached"))
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-fable-5",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"
    assert response.headers["x-should-retry"] == "false"
    assert "retry-after" not in response.headers


def test_parse_responses_function_output_and_anthropic_tool_result():
    responses = parse_responses_request(
        {
            "model": "chatgpt-web",
            "input": [
                {"type": "function_call_output", "call_id": "call_1", "output": "42"},
            ],
        }
    )
    assert responses.request.messages == [
        {"role": "tool", "tool_call_id": "call_1", "content": "42"}
    ]

    anthropic = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "42"}],
                }
            ],
        }
    )
    assert anthropic.request.messages == [
        {"role": "tool", "tool_call_id": "call_1", "content": "42"}
    ]


def test_responses_api_and_previous_response_continuation(monkeypatch):
    app = create_api_app()
    session = _fake_session("first answer")
    monkeypatch.setattr(app.state.server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        first = client.post(
            "/v1/responses",
            json={"model": "chatgpt-web", "input": "hello"},
        )
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["object"] == "response"
        assert first_body["output"][0]["content"][0]["text"] == "first answer"

        session.send.return_value = TurnResult(
            turn_id="turn_2", conversation_id="conv_1", text="second answer"
        )
        second = client.post(
            "/v1/responses",
            json={
                "model": "chatgpt-web",
                "previous_response_id": first_body["id"],
                "input": "continue",
            },
        )

    assert second.status_code == 200, second.text
    assert second.json()["output"][0]["content"][0]["text"] == "second answer"
    assert session.open.await_count == 1
    assert session.send.await_count == 2


def test_responses_stream_has_protocol_events(monkeypatch):
    app = create_api_app()
    session = _fake_session("stream answer")
    monkeypatch.setattr(app.state.server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={"model": "chatgpt-web", "input": "hello", "stream": True},
        )

    assert response.status_code == 200
    assert "event: response.created" in response.text
    assert "event: response.output_text.delta" in response.text
    assert "event: response.completed" in response.text


def test_responses_function_call_output_continues_previous_response(monkeypatch):
    app = create_api_app()
    session = _fake_session(
        '<WEBGPT_TOOL_CALL>\n{"name":"lookup","arguments":{"q":"Hanoi"}}\n</WEBGPT_TOOL_CALL>'
    )
    monkeypatch.setattr(app.state.server, "get_or_create_session", AsyncMock(return_value=session))
    with TestClient(app) as client:
        first = client.post(
            "/v1/responses",
            json={
                "model": "chatgpt-web",
                "input": "lookup Hanoi",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup",
                        "parameters": {"type": "object"},
                    }
                ],
            },
        )
        assert first.status_code == 200
        call = first.json()["output"][0]
        session.send.return_value = TurnResult(
            turn_id="turn_2", conversation_id="conv_1", text="Hanoi result"
        )
        second = client.post(
            "/v1/responses",
            json={
                "model": "chatgpt-web",
                "previous_response_id": first.json()["id"],
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": "sunny",
                    }
                ],
            },
        )

    assert second.status_code == 200, second.text
    assert second.json()["output"][0]["content"][0]["text"] == "Hanoi result"
    assert session.send.await_count == 2


def test_anthropic_messages_and_stream(monkeypatch):
    app = create_api_app()
    session = _fake_session("anthropic answer")
    monkeypatch.setattr(app.state.server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "chatgpt-web",
                "max_tokens": 32,
                "system": "be concise",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        streamed = client.post(
            "/v1/messages",
            json={
                "model": "chatgpt-web",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hello again"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "message"
    assert payload["content"] == [{"type": "text", "text": "anthropic answer"}]
    assert streamed.status_code == 200
    assert "event: message_start" in streamed.text
    assert "event: message_stop" in streamed.text


def test_anthropic_errors_use_anthropic_envelope():
    app = create_api_app()
    with TestClient(app) as client:
        response = client.post("/v1/messages", json={"model": "chatgpt-web", "messages": []})

    assert response.status_code == 400
    assert response.json()["type"] == "error"
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_anthropic_tool_conversion():
    request = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": "weather"}],
            "tools": [
                {
                    "name": "weather",
                    "description": "lookup",
                    "input_schema": {"type": "object"},
                }
            ],
            "tool_choice": {"type": "tool", "name": "weather"},
        }
    )
    assert request.request.tool_choice == {"type": "function", "function": {"name": "weather"}}
    assert request.request.tools[0]["function"]["parameters"] == {"type": "object"}


def test_anthropic_assistant_text_and_tool_use_stay_one_message():
    request = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I will check."},
                        {"type": "tool_use", "id": "tool_1", "name": "read", "input": {"path": "a"}},
                    ],
                }
            ],
        }
    )
    assert request.request.messages == [
        {
            "role": "assistant",
            "content": "I will check.",
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path": "a"}'},
                }
            ],
        }
    ]


def test_anthropic_tool_use_round_trip_preserves_conversation_prefix(monkeypatch):
    app = create_api_app()
    session = _fake_session(
        '<WEBGPT_TOOL_CALL>\n{"name":"read","arguments":{"path":"README.md"}}\n</WEBGPT_TOOL_CALL>'
    )
    monkeypatch.setattr(app.state.server, "get_or_create_session", AsyncMock(return_value=session))
    tools = [
        {
            "name": "read",
            "description": "read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    with TestClient(app) as client:
        first = client.post(
            "/v1/messages?beta=true",
            json={
                "model": "claude-fable-5",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "read the file"}],
                "tools": tools,
            },
        )
        assert first.status_code == 200
        tool_use = first.json()["content"][0]
        record = next(iter(app.state.server.conversations._records.values()))
        assert record.messages[-1]["tool_calls"][0]["id"] == tool_use["id"]
        session.send.return_value = TurnResult(
            turn_id="turn_2", conversation_id="conv_1", text="the file is read"
        )
        second = client.post(
            "/v1/messages?beta=true",
            json={
                "model": "claude-fable-5",
                "max_tokens": 32,
                "messages": [
                    {"role": "user", "content": "read the file"},
                    {"role": "assistant", "content": [tool_use]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use["id"],
                                "content": "# Title",
                            }
                        ],
                    },
                ],
                "tools": tools,
            },
        )

    assert second.status_code == 200, second.text
    assert second.json()["content"] == [{"type": "text", "text": "the file is read"}]
    assert session.send.await_count == 2


def test_anthropic_force_initial_tool_is_opt_in(monkeypatch):
    app = create_api_app(force_anthropic_initial_tool=True)
    session = _fake_session("this should not be called for forced initial Read")
    monkeypatch.setattr(app.state.server, "get_or_create_session", AsyncMock(return_value=session))
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-fable-5",
                "messages": [{"role": "user", "content": "read specification"}],
                "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["stop_reason"] == "tool_use"
    assert payload["content"][0]["type"] == "tool_use"
    assert payload["content"][0]["name"] == "Read"
    assert payload["content"][0]["input"] == {"file_path": "SPEC.md"}
    session.send.assert_not_awaited()


def test_anthropic_force_initial_tool_skips_conversational_prompt(monkeypatch):
    app = create_api_app(force_anthropic_initial_tool=True)
    session = _fake_session("normal conversational completion")
    monkeypatch.setattr(app.state.server, "get_or_create_session", AsyncMock(return_value=session))
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-fable-5",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
            },
        )

    assert response.status_code == 200
    assert response.json()["stop_reason"] == "end_turn"
    assert response.json()["content"] == [
        {"type": "text", "text": "normal conversational completion"}
    ]
    session.send.assert_awaited_once()


def test_anthropic_full_history_ignores_already_committed_tool_results(monkeypatch):
    app = create_api_app()
    session = _fake_session(
        '<WEBGPT_TOOL_CALL>\n{"name":"read","arguments":{"path":"one"}}\n</WEBGPT_TOOL_CALL>'
    )
    monkeypatch.setattr(app.state.server, "get_or_create_session", AsyncMock(return_value=session))
    tools = [{"name": "read", "input_schema": {"type": "object"}}]
    initial_messages = [{"role": "user", "content": "read two files"}]
    with TestClient(app) as client:
        first = client.post(
            "/v1/messages", json={"model": "claude-fable-5", "messages": initial_messages, "tools": tools}
        )
        first_call = first.json()["content"][0]
        session.send.return_value = TurnResult(
            turn_id="turn_2",
            conversation_id="conv_1",
            text='<WEBGPT_TOOL_CALL>\n{"name":"read","arguments":{"path":"two"}}\n</WEBGPT_TOOL_CALL>',
        )
        first_result = {"type": "tool_result", "tool_use_id": first_call["id"], "content": "one"}
        second = client.post(
            "/v1/messages",
            json={
                "model": "claude-fable-5",
                "messages": [
                    *initial_messages,
                    {"role": "assistant", "content": [first_call]},
                    {"role": "user", "content": [first_result]},
                ],
                "tools": tools,
            },
        )
        assert second.status_code == 200, second.text
        second_call = second.json()["content"][0]
        session.send.return_value = TurnResult(
            turn_id="turn_3", conversation_id="conv_1", text="all files read"
        )
        third = client.post(
            "/v1/messages",
            json={
                "model": "claude-fable-5",
                "messages": [
                    *initial_messages,
                    {"role": "assistant", "content": [first_call]},
                    {"role": "user", "content": [first_result]},
                    {"role": "assistant", "content": [second_call]},
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": second_call["id"], "content": "two"}
                        ],
                    },
                ],
                "tools": tools,
            },
        )

    assert third.status_code == 200, third.text
    assert third.json()["content"] == [{"type": "text", "text": "all files read"}]
    assert session.send.await_count == 3
