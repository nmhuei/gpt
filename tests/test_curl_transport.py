from __future__ import annotations

import pytest

from gpt.state import AuthRequired
from gpt.transport.curl_transport import CurlCffiTransport
from gpt.transport.token_manager import SentinelTokens, TokenBundle
from gpt.types import ModelInfo, SendRequest


class FakeTokenManager:
    async def refresh_if_needed(self):
        return TokenBundle(
            access_token="access-token",
            cookies={"cf_clearance": "clearance", "session": "session-cookie"},
            cf_clearance="clearance",
            oai_device_id="device-id",
        )

    async def get_sentinel_tokens(self, conversation_id):
        assert conversation_id == "conversation-1"
        return SentinelTokens("requirements", "proof", "turnstile")


class FakeResponse:
    status_code = 200

    def __init__(self):
        self.closed = False

    async def aiter_bytes(self):
        yield b'data: {"conversation_id":"conversation-1","message":{"id":"turn-1","content":{"parts":["Hel"]}}}\n\n'
        yield b'data: {"message":{"metadata":{"model_slug":"gpt-test"},"content":{"parts":["Hello"]},"status":"finished_successfully"}}\n\n'

    async def aclose(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.response = FakeResponse()
        self.calls = []

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


@pytest.mark.anyio
async def test_posts_chrome_style_headers_and_accumulates_sse():
    session = FakeSession()
    transport = CurlCffiTransport(FakeTokenManager(), session=session)
    request = SendRequest(
        text="Hello",
        conversation_id="conversation-1",
        model=ModelInfo(id="gpt-test", label="GPT Test"),
        reasoning_effort="high",
    )

    result = await transport.send(request)

    assert result.text == "Hello"
    assert result.turn_id == "turn-1"
    assert result.conversation_id == "conversation-1"
    assert result.model == "gpt-test"
    assert session.response.closed
    args, kwargs = session.calls[0]
    assert args[0].endswith("/backend-api/f/conversation")
    assert kwargs["stream"] is True
    assert kwargs["headers"]["Authorization"] == "Bearer access-token"
    assert kwargs["headers"]["oai-device-id"] == "device-id"
    assert "cf_clearance=clearance" in kwargs["headers"]["Cookie"]
    assert kwargs["headers"]["openai-sentinel-chat-requirements-token"] == "requirements"
    assert kwargs["headers"]["openai-sentinel-proof-token"] == "proof"
    assert kwargs["headers"]["openai-sentinel-turnstile-token"] == "turnstile"
    assert kwargs["json"]["model"] == "gpt-test"
    assert kwargs["json"]["thinking_effort"] == "high"


@pytest.mark.anyio
async def test_sse_snapshots_emit_only_their_direct_text_deltas():
    session = FakeSession()
    transport = CurlCffiTransport(FakeTokenManager(), session=session)
    deltas: list[tuple[str, str]] = []

    async def on_delta(text: str, turn_id: str) -> None:
        deltas.append((text, turn_id))

    result = await transport.send(
        SendRequest(text="Hello", conversation_id="conversation-1"), on_delta=on_delta
    )

    assert result.text == "Hello"
    assert deltas == [("Hel", "turn-1"), ("lo", "turn-1")]


def test_direct_backend_fails_closed_without_required_credentials():
    bundle = TokenBundle(
        access_token="",
        cookies={},
        cf_clearance=None,
        oai_device_id=None,
    )

    with pytest.raises(AuthRequired, match="Direct backend generation is missing required credentials"):
        CurlCffiTransport._build_headers(bundle, SentinelTokens())


@pytest.mark.anyio
async def test_local_mock_bundle_streams_without_an_http_request():
    class LocalTokenManager:
        async def refresh_if_needed(self):
            return TokenBundle(access_token="local-mock-token", cookies={}, is_local_mock=True)

    class NoNetworkSession:
        async def post(self, *args, **kwargs):
            raise AssertionError("local mock token must never be sent upstream")

    deltas: list[str] = []
    transport = CurlCffiTransport(LocalTokenManager(), session=NoNetworkSession())
    result = await transport.send(
        SendRequest(text="<WEBGPT_MESSAGE role=\"user\">\n{\"content\":\"hello\"}\n</WEBGPT_MESSAGE>"),
        on_delta=lambda delta, _turn_id: deltas.append(delta),
    )

    assert "Xin chào" in result.text
    assert "".join(deltas) == result.text


def test_local_mock_only_calls_tools_for_explicit_tool_requests():
    tools = '[{"name":"Read","parameters":{"type":"object"}}]'
    conversational_prompt = (
        f"Available tools: {tools}\n"
        '<WEBGPT_MESSAGE role="user">\n{"content":"toio laf ai"}\n</WEBGPT_MESSAGE>'
    )
    tool_prompt = (
        f"Available tools: {tools}\n"
        '<WEBGPT_MESSAGE role="user">\n{"content":"Read pyproject.toml"}\n</WEBGPT_MESSAGE>'
    )
    question_prompt = (
        f"Available tools: {tools}\n"
        '<WEBGPT_MESSAGE role="user">\n{"content":"How are you?"}\n</WEBGPT_MESSAGE>'
    )

    assert "người dùng" in CurlCffiTransport._local_mock_text(conversational_prompt)
    assert "tiếp nhận" in CurlCffiTransport._local_mock_text(question_prompt)
    assert "<WEBGPT_TOOL_CALL>" in CurlCffiTransport._local_mock_text(tool_prompt)


def test_local_mock_uses_the_last_user_message_for_intent_and_response():
    tools = '[{"name":"Read","parameters":{"type":"object"}}]'
    prompt = (
        f"Available tools: {tools}\n"
        '<WEBGPT_MESSAGE role="user">\n{"content":"Read README.md"}\n</WEBGPT_MESSAGE>\n'
        '<WEBGPT_MESSAGE role="user">\n{"content":"hello"}\n</WEBGPT_MESSAGE>'
    )

    assert "Xin chào" in CurlCffiTransport._local_mock_text(prompt)
