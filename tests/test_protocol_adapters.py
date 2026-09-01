import base64
import logging
from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from gpt.api.protocol_adapters import parse_anthropic_request, parse_responses_request
from gpt.api.requests import RequestValidationError
from gpt.api.server import create_api_app
from gpt.state import RateLimited
from gpt.types import TurnResult


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode()


def _anthropic_image(media_type: str, payload: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": _b64(payload)},
    }


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


def test_anthropic_tool_result_is_error_renders_into_prompt():
    from gpt.utils.promptcompat import render_messages

    adapted = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_err",
                            "content": "boom",
                            "is_error": True,
                        }
                    ],
                }
            ],
        }
    )
    assert adapted.request.messages == [
        {"role": "tool", "tool_call_id": "call_err", "content": "boom", "is_error": True}
    ]
    rendered = render_messages(
        adapted.request.messages, initial=False, tools=[], tool_choice="auto"
    )
    assert '"is_error": true' in rendered


def test_anthropic_tool_result_without_is_error_stays_clean():
    from gpt.utils.promptcompat import render_messages

    adapted = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_ok", "content": "42"}
                    ],
                }
            ],
        }
    )
    assert adapted.request.messages == [
        {"role": "tool", "tool_call_id": "call_ok", "content": "42"}
    ]
    rendered = render_messages(
        adapted.request.messages, initial=False, tools=[], tool_choice="auto"
    )
    assert '"is_error": true' not in rendered
    assert "is_error" not in rendered


# ---------------------------------------------------------------------------
# ANTHROPIC-INGRESS-IMAGE (2026-08-26): /v1/messages image blocks must reach
# the render layer as the shared P1-2A placeholder instead of being stripped
# at ingress.  Text-only payloads stay byte-identical.
# ---------------------------------------------------------------------------


def test_anthropic_image_block_becomes_placeholder_marker():
    from gpt.utils.promptcompat import render_messages

    # b64(b"x"*4096) -> len 5464 chars -> ~4098 bytes -> ceil 5KB.
    marker = "[image omitted: image/png ~5KB — image upload not supported yet]"
    adapted = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        _anthropic_image("image/png", b"x" * 4096),
                    ],
                }
            ],
        }
    )
    assert adapted.request.messages == [
        {"role": "user", "content": f"what is this?\n{marker}"}
    ]
    rendered = render_messages(
        adapted.request.messages, initial=False, tools=[], tool_choice="auto"
    )
    assert "[image omitted: image/png ~5KB" in rendered


def test_anthropic_image_upload_flag_emits_transport_marker(monkeypatch):
    monkeypatch.setenv("WEBGPT_IMAGE_UPLOAD_WEB", "1")
    payload = b"png-bytes"
    adapted = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect"},
                        _anthropic_image("image/png", payload),
                    ],
                }
            ],
        }
    )
    marker = (
        '<WEBGPT_IMAGE_DATA mime="image/png">'
        + _b64(payload)
        + '</WEBGPT_IMAGE_DATA>'
    )
    assert adapted.request.messages == [
        {"role": "user", "content": f"inspect\n{marker}"}
    ]


def test_anthropic_image_upload_flag_keeps_remote_url_as_placeholder(monkeypatch):
    monkeypatch.setenv("WEBGPT_IMAGE_UPLOAD_WEB", "1")
    adapted = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "url", "url": "https://example.com/x.png"},
                        }
                    ],
                }
            ],
        }
    )
    assert adapted.request.messages == [
        {"role": "user", "content": "[image omitted: unknown — image upload not supported yet]"}
    ]


def test_anthropic_text_only_blocks_render_unchanged():
    """No images -> output byte-identical to the pre-change _text_blocks path."""
    from gpt.utils.promptcompat import render_messages

    adapted = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
                }
            ],
        }
    )
    assert adapted.request.messages == [{"role": "user", "content": "a\nb"}]
    rendered = render_messages(
        adapted.request.messages, initial=False, tools=[], tool_choice="auto"
    )
    assert rendered == render_messages(
        [{"role": "user", "content": "a\nb"}], initial=False, tools=[], tool_choice="auto"
    )


def test_anthropic_image_kill_switch_restores_silent_drop(monkeypatch):
    monkeypatch.setenv("WEBGPT_IMAGE_PLACEHOLDER", "0")
    with_text = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        _anthropic_image("image/png", b"x" * 4096),
                    ],
                }
            ],
        }
    )
    assert with_text.request.messages == [{"role": "user", "content": "what is this?"}]

    from gpt.api.requests import RequestValidationError

    try:
        parse_anthropic_request(
            {
                "model": "chatgpt-web",
                "messages": [
                    {"role": "user", "content": [_anthropic_image("image/png", b"x")]}
                ],
            }
        )
    except RequestValidationError:
        pass  # pre-change behavior: image-only message had no supported content
    else:
        raise AssertionError("image-only request must fail like before kill switch")


def test_anthropic_tool_result_image_block_becomes_marker():
    # b64(b"y"*2048) -> len 2732 chars -> ~2049 bytes -> ceil 3KB.
    adapted = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_shot",
                            "content": [
                                {"type": "text", "text": "shot"},
                                _anthropic_image("image/jpeg", b"y" * 2048),
                            ],
                        }
                    ],
                }
            ],
        }
    )
    assert adapted.request.messages == [
        {
            "role": "tool",
            "tool_call_id": "call_shot",
            "content": "shot\n[image omitted: image/jpeg ~3KB — image upload not supported yet]",
        }
    ]


def test_anthropic_url_source_image_marker_without_fetch():
    from gpt.utils.promptcompat import render_messages

    adapted = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "url", "url": "https://example.com/cat.png"},
                        }
                    ],
                }
            ],
        }
    )
    assert adapted.request.messages == [
        {"role": "user", "content": "[image omitted: unknown — image upload not supported yet]"}
    ]
    rendered = render_messages(
        adapted.request.messages, initial=False, tools=[], tool_choice="auto"
    )
    assert "[image omitted:" in rendered
    # Metadata-only marker: the remote image is never fetched.
    assert "example.com" not in rendered


def test_anthropic_multiple_images_keep_block_order():
    adapted = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "one"},
                        _anthropic_image("image/png", b"x" * 1024),  # ~2KB
                        {"type": "text", "text": "two"},
                        _anthropic_image("image/jpeg", b"z" * 512),  # ~1KB
                    ],
                }
            ],
        }
    )
    assert adapted.request.messages == [
        {
            "role": "user",
            "content": (
                "one\n"
                "[image omitted: image/png ~2KB — image upload not supported yet]\n"
                "two\n"
                "[image omitted: image/jpeg ~1KB — image upload not supported yet]"
            ),
        }
    ]


# ---------------------------------------------------------------------------
# ANTHROPIC-FIELDS-EXPLICIT (2026-08-26): request-level fields the real
# Anthropic API handles explicitly must not vanish silently here.
# ---------------------------------------------------------------------------


def _anthropic_document(title: str | None = None) -> dict:
    block: dict = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": _b64(b"%PDF-1.4 not really a pdf"),
        },
    }
    if title is not None:
        block["title"] = title
    return block


def test_anthropic_stop_sequences_non_empty_rejected_with_envelope():
    app = create_api_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "chatgpt-web",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hello"}],
                "stop_sequences": ["STOP", "DONE"],
            },
        )

    assert response.status_code == 400
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "invalid_request_error"
    message = payload["error"]["message"]
    assert "stop_sequences" in message
    assert "not supported" in message


def test_anthropic_stop_sequences_parser_rejects_but_empty_array_passes():
    with_rejection = {"model": "chatgpt-web", "messages": [{"role": "user", "content": "hi"}]}
    try:
        parse_anthropic_request({**with_rejection, "stop_sequences": ["STOP"]})
    except RequestValidationError as exc:
        assert "stop_sequences" in str(exc)
        assert "not supported" in str(exc)
    else:
        raise AssertionError("non-empty stop_sequences must be rejected explicitly")

    accepted = parse_anthropic_request({**with_rejection, "stop_sequences": []})
    assert accepted.request.messages == [{"role": "user", "content": "hi"}]


def test_anthropic_thinking_enabled_rejected_with_envelope():
    app = create_api_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "chatgpt-web",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hello"}],
                "thinking": {"type": "enabled", "budget_tokens": 2048},
            },
        )

    assert response.status_code == 400
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "invalid_request_error"
    message = payload["error"]["message"]
    assert "thinking" in message
    assert "not supported" in message


def test_anthropic_thinking_disabled_and_absent_accepted():
    base = {"model": "chatgpt-web", "messages": [{"role": "user", "content": "hi"}]}
    disabled = parse_anthropic_request(
        {**base, "thinking": {"type": "disabled"}}
    )
    assert disabled.request.messages == [{"role": "user", "content": "hi"}]
    absent = parse_anthropic_request(base)
    assert absent.request.messages == [{"role": "user", "content": "hi"}]


def test_anthropic_thinking_adaptive_accepted_like_current_client(caplog):
    """Current Claude Code ships thinking.type='adaptive' on every request.

    Rejecting it would break the production client, so it stays accepted and
    only logged (explicit, not silent, but never a 400).
    """
    with caplog.at_level(logging.DEBUG, logger="gpt.api.protocol_adapters"):
        adapted = parse_anthropic_request(
            {
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "hi"}],
                "thinking": {"type": "adaptive", "display": "omitted"},
            }
        )
    assert adapted.request.messages == [{"role": "user", "content": "hi"}]
    assert "accepted-and-ignored" in caplog.text
    assert "adaptive" in caplog.text


def test_anthropic_metadata_accept_and_ignore_logged(caplog):
    base = {"model": "chatgpt-web", "messages": [{"role": "user", "content": "hi"}]}
    with caplog.at_level(logging.DEBUG, logger="gpt.api.protocol_adapters"):
        adapted = parse_anthropic_request(
            {**base, "metadata": {"user_id": "user_abc", "request_id": "req_1"}}
        )
    assert adapted.request.messages == [{"role": "user", "content": "hi"}]
    assert "accepted-and-ignored" in caplog.text
    assert "user_id" in caplog.text

    # Non-dict metadata must also be tolerated (logged, never raised).
    tolerated = parse_anthropic_request({**base, "metadata": "weird"})
    assert tolerated.request.messages == [{"role": "user", "content": "hi"}]


def test_anthropic_document_block_becomes_placeholder_marker():
    from gpt.utils.promptcompat import render_messages

    adapted = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "summarize this"},
                        _anthropic_document("Spec"),
                    ],
                }
            ],
        }
    )
    assert adapted.request.messages == [
        {
            "role": "user",
            "content": "summarize this\n[document omitted: Spec, application/pdf]",
        }
    ]
    rendered = render_messages(
        adapted.request.messages, initial=False, tools=[], tool_choice="auto"
    )
    assert "[document omitted: Spec, application/pdf]" in rendered

    # Mime only (no title) and unknown (no media_type) variants.
    mime_only = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": [_anthropic_document()]}],
        }
    )
    assert mime_only.request.messages == [
        {"role": "user", "content": "[document omitted: application/pdf]"}
    ]

    untitled_url = {
        "type": "document",
        "source": {"type": "url", "url": "https://example.com/doc.pdf"},
    }
    unknown = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": [untitled_url]}],
        }
    )
    assert unknown.request.messages == [
        {"role": "user", "content": "[document omitted: unknown]"}
    ]

    # tool_result block arrays get the same treatment.
    tool_result = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_doc",
                            "content": [_anthropic_document("Report")],
                        }
                    ],
                }
            ],
        }
    )
    assert tool_result.request.messages == [
        {
            "role": "tool",
            "tool_call_id": "call_doc",
            "content": "[document omitted: Report, application/pdf]",
        }
    ]


def test_anthropic_document_kill_switch_drops_silently(monkeypatch):
    monkeypatch.setenv("WEBGPT_IMAGE_PLACEHOLDER", "0")
    mixed = parse_anthropic_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "summarize this"},
                        _anthropic_document("Spec"),
                    ],
                }
            ],
        }
    )
    assert mixed.request.messages == [{"role": "user", "content": "summarize this"}]

    try:
        parse_anthropic_request(
            {
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": [_anthropic_document()]}],
            }
        )
    except RequestValidationError:
        pass  # document-only message had no supported content before the marker
    else:
        raise AssertionError("document-only request must fail like before kill switch")
