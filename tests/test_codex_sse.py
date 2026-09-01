"""CODEX-SSE branch tests (docs/reports/codex-sse-spec-2026-08-25.md).

Fake session only — never touches the network.  Covers the four required
assertions: (a) headers, (b) body shape, (c) SSE parse → ordered deltas +
completion, (d) WEBGPT_CODEX_SSE OFF keeps the legacy f/conversation path.
Review round 10 adds sentinel-mint counting, full legacy header pinning,
403 → access-token invalidation, challenge remint with fresh retry headers,
and pre-created delta buffering.
"""

from __future__ import annotations

import json

import pytest

from gpt.state import AuthRequired
from gpt.transport.curl_transport import (
    CLOAKBROWSER_USER_AGENT,
    CODEX_RESPONSES_URL,
    CurlCffiTransport,
)
from gpt.transport.token_manager import SentinelTokens, TokenBundle
from gpt.types import ModelInfo, SendRequest

CODEX_EVENTS = (
    '{"type":"response.created","response":{"id":"resp_abc123"}}',
    '{"type":"response.output_item.added","item":{"type":"message","role":"assistant"}}',
    '{"type":"response.output_text.delta","delta":"Xin "}',
    '{"type":"response.output_text.delta","delta":"chào!"}',
    '{"type":"response.function_call_arguments.delta","delta":"{\\"a\\""}',
    '{"type":"response.completed",'
    '"response":{"id":"resp_abc123","output":[],"usage":{"input_tokens":1,"output_tokens":2}}}',
)

SYSTEM_USER_PROMPT = (
    '<WEBGPT_MESSAGE role="system">\n{"content":"You are WebGPT."}\n</WEBGPT_MESSAGE>\n\n'
    '<WEBGPT_MESSAGE role="user">\n{"content":"hello"}\n</WEBGPT_MESSAGE>'
)


class FakeTokenManager:
    def __init__(self) -> None:
        self.invalidated = False
        self.access_invalidations = 0
        self.sentinel_calls = 0
        self.extract_calls = 0
        # Mutable so the remint flow can hand out a fresh snapshot.
        self.access_token = "access-token"
        self.cf_clearance = "clearance"

    async def refresh_if_needed(self):
        return TokenBundle(
            access_token=self.access_token,
            cookies={"cf_clearance": self.cf_clearance, "session": "session-cookie"},
            cf_clearance=self.cf_clearance,
            oai_device_id="device-id",
        )

    async def get_sentinel_tokens(self, conversation_id):
        self.sentinel_calls += 1
        return SentinelTokens("requirements", "proof", "turnstile")

    def invalidate_sentinel(self) -> None:
        self.invalidated = True

    def invalidate_access_token(self) -> None:
        self.access_invalidations += 1
        self.access_token = "access-token-v2"
        self.cf_clearance = "fresh-clearance"

    async def extract_all(self):
        """Browser-backed re-mint: bypasses the refresh interval entirely."""
        self.extract_calls += 1
        self.access_token = "access-token-v3"
        self.cf_clearance = "reminted-clearance"


class FakeResponse:
    status_code = 200

    def __init__(self, payload: bytes = b""):
        self._payload = payload
        self.closed = False

    async def aiter_bytes(self):
        # Split mid-record so incremental SSE decoding is exercised.
        half = max(len(self._payload) // 2, 1)
        yield self._payload[:half]
        if self._payload[half:]:
            yield self._payload[half:]

    async def aclose(self):
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeResponse | None = None):
        self.response = response or FakeResponse(_sse_bytes(*CODEX_EVENTS))
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


def _sse_bytes(*records: str) -> bytes:
    return "".join(f"data: {record}\n\n" for record in records).encode("utf-8")


@pytest.fixture(autouse=True)
def _isolate_flag(monkeypatch):
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)


@pytest.mark.anyio
async def test_codex_branch_posts_responses_shape_and_headers(monkeypatch):
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    manager = FakeTokenManager()
    session = FakeSession()
    transport = CurlCffiTransport(manager, session=session)
    request = SendRequest(
        text=SYSTEM_USER_PROMPT,
        conversation_id=None,
        model=ModelInfo(id="gpt-5.2", label="GPT 5.2"),
        reasoning_effort="high",
    )

    result = await transport.send(request)

    # Codex must never mint a sentinel (spec §1: no browser round-trip).
    assert manager.sentinel_calls == 0

    args, kwargs = session.calls[0]
    assert args[0] == CODEX_RESPONSES_URL
    headers = kwargs["headers"]
    assert headers["Authorization"] == "Bearer access-token"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "text/event-stream"
    assert "cf_clearance=clearance" in headers["Cookie"]
    for forbidden in (
        "openai-sentinel-chat-requirements-token",
        "openai-sentinel-proof-token",
        "openai-sentinel-turnstile-token",
        "oai-device-id",
    ):
        assert forbidden not in headers

    body = kwargs["json"]
    assert kwargs["stream"] is True
    assert body["store"] is False
    assert body["stream"] is True
    assert body["tools"] == []
    assert body["tool_choice"] == "auto"
    assert body["model"] == "gpt-5.2"
    assert body["instructions"] == "You are WebGPT."
    assert len(body["input"]) == 1
    first = body["input"][0]
    assert first["type"] == "message"
    assert first["role"] == "user"
    assert first["content"][0]["type"] == "input_text"
    assert first["content"][0]["text"] == "hello"
    assert "You are WebGPT." not in json.dumps(body["input"])

    assert result.text == "Xin chào!"
    assert result.turn_id == "resp_abc123"
    assert result.status == "completed"
    assert session.response.closed


@pytest.mark.anyio
async def test_codex_stream_emits_deltas_in_order_with_completion(monkeypatch):
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    session = FakeSession()
    transport = CurlCffiTransport(FakeTokenManager(), session=session)
    deltas: list[tuple[str, str]] = []

    async def on_delta(text: str, turn_id: str) -> None:
        deltas.append((text, turn_id))

    result = await transport.send(
        SendRequest(text=SYSTEM_USER_PROMPT), on_delta=on_delta
    )

    assert "".join(delta for delta, _ in deltas) == result.text == "Xin chào!"
    assert deltas == [("Xin ", "resp_abc123"), ("chào!", "resp_abc123")]
    assert result.status == "completed"


@pytest.mark.anyio
async def test_codex_completed_empty_output_still_yields_delta_text(monkeypatch):
    # Regression hermes-agent#5678: completed.response.output may be empty even
    # though text streamed through deltas — text must come from deltas only.
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    payload = _sse_bytes(
        '{"type":"response.created","response":{"id":"resp_hermes"}}',
        '{"type":"response.output_text.delta","delta":"only "}',
        '{"type":"response.output_text.delta","delta":"deltas"}',
        '{"type":"response.completed","response":{"id":"resp_hermes","output":[]}}',
    )
    session = FakeSession(FakeResponse(payload))
    transport = CurlCffiTransport(FakeTokenManager(), session=session)

    result = await transport.send(SendRequest(text=SYSTEM_USER_PROMPT))

    assert result.text == "only deltas"
    assert result.status == "completed"
    assert result.turn_id == "resp_hermes"


@pytest.mark.anyio
async def test_codex_payload_maps_tool_history_to_function_calls(monkeypatch):
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    prompt = (
        '<WEBGPT_MESSAGE role="system">\n{"content":"sys"}\n</WEBGPT_MESSAGE>\n\n'
        '<WEBGPT_MESSAGE role="assistant">\n'
        '{"content":"","tool_calls":[{"id":"call_1","name":"Read",'
        '"arguments":"{\\"file_path\\":\\"README.md\\"}"}]}\n</WEBGPT_MESSAGE>\n\n'
        '<WEBGPT_TOOL_RESULT>\n{"id":"call_1","content":"file body"}\n</WEBGPT_TOOL_RESULT>\n\n'
        '<WEBGPT_MESSAGE role="user">\n{"content":"next"}\n</WEBGPT_MESSAGE>'
    )
    session = FakeSession()
    transport = CurlCffiTransport(FakeTokenManager(), session=session)

    payload = transport._build_codex_payload(SendRequest(text=prompt))

    types = [item["type"] for item in payload["input"]]
    assert types == ["function_call", "function_call_output", "message"]
    call = payload["input"][0]
    assert call["call_id"] == "call_1"
    assert call["name"] == "Read"
    output = payload["input"][1]
    assert output["call_id"] == "call_1"
    assert output["output"] == "file body"
    assert payload["input"][2]["content"][0]["text"] == "next"


@pytest.mark.anyio
async def test_codex_model_falls_back_without_model_info(monkeypatch):
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    transport = CurlCffiTransport(FakeTokenManager(), session=FakeSession())
    payload = transport._build_codex_payload(
        SendRequest(text='<WEBGPT_MESSAGE role="user">\n{"content":"hi"}\n</WEBGPT_MESSAGE>')
    )
    assert payload["model"] == "gpt-5"
    assert payload["instructions"] == ""


@pytest.mark.anyio
async def test_codex_disabled_keeps_legacy_conversation_path(monkeypatch):
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)

    # Reuse the exact legacy fixture bytes from test_curl_transport.py.
    class LegacyResponse(FakeResponse):
        async def aiter_bytes(self):
            yield b'data: {"conversation_id":"conversation-1","message":{"id":"turn-1","content":{"parts":["Hel"]}}}\n\n'
            yield b'data: {"message":{"metadata":{"model_slug":"gpt-test"},"content":{"parts":["Hello"]},"status":"finished_successfully"}}\n\n'

    session = FakeSession(LegacyResponse())
    manager = FakeTokenManager()
    transport = CurlCffiTransport(manager, session=session)

    request = SendRequest(
        text="Hello",
        conversation_id="conversation-1",
        model=ModelInfo(id="gpt-test", label="GPT Test"),
        reasoning_effort="high",
    )
    result = await transport.send(request)

    assert result.text == "Hello"
    # Gate OFF must keep minting the sentinel exactly like the legacy path.
    assert manager.sentinel_calls >= 1
    args, kwargs = session.calls[0]
    assert args[0].endswith("/backend-api/f/conversation")
    assert args[0] != CODEX_RESPONSES_URL
    headers = kwargs["headers"]
    # Full envelope assertion (review round 10): a partial subset let the
    # legacy User-Agent drift from Mozilla/5.0 to the Chrome-146 minting UA
    # unnoticed — pin every header value and the exact key set instead.
    expected_headers = {
        "Accept": "text/event-stream",
        "Authorization": "Bearer access-token",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-language": "en-US",
        "User-Agent": CLOAKBROWSER_USER_AGENT,
        "oai-device-id": "device-id",
        "openai-sentinel-chat-requirements-token": "requirements",
        "openai-sentinel-proof-token": "proof",
        "openai-sentinel-turnstile-token": "turnstile",
    }
    for name, value in expected_headers.items():
        assert headers[name] == value, f"legacy header {name} drifted"
    assert set(headers) == set(expected_headers) | {"Cookie"}
    assert "cf_clearance=clearance" in headers["Cookie"]
    assert "originator" not in headers
    assert "OpenAI-Beta" not in headers
    body = kwargs["json"]
    assert body["action"] == "next"
    assert body["messages"][0]["author"]["role"] == "user"
    assert body["thinking_effort"] == "high"
    assert "store" not in body and "stream" not in body


@pytest.mark.anyio
async def test_codex_rejection_invalidates_credentials_and_raises_auth(monkeypatch):
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")

    class RejectedResponse:
        status_code = 403

        async def aclose(self):
            return None

    session = FakeSession(RejectedResponse())
    manager = FakeTokenManager()
    transport = CurlCffiTransport(manager, session=session)

    with pytest.raises(AuthRequired, match="rejected"):
        await transport.send(SendRequest(text=SYSTEM_USER_PROMPT))

    # Review round 10: the codex envelope carries no sentinel, so a 401/403
    # must invalidate the access token / cookie jar it actually used and must
    # NOT touch the sentinel cache.
    assert manager.access_invalidations == 1
    assert manager.invalidated is False


@pytest.mark.anyio
async def test_codex_challenge_remints_and_retries_with_fresh_headers(monkeypatch):
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")

    class ChallengedResponse(FakeResponse):
        status_code = 403

        async def aiter_bytes(self):
            yield b"<html>Just a moment...</html>"

        async def aclose(self):
            return None

    ok_response = FakeResponse(_sse_bytes(*CODEX_EVENTS))
    responses = [ChallengedResponse(), ok_response]
    header_attempts: list[dict] = []

    class RetrySession:
        async def post(self, *args, **kwargs):
            header_attempts.append(kwargs["headers"])
            return responses.pop(0)

    manager = FakeTokenManager()
    transport = CurlCffiTransport(manager, session=RetrySession())

    result = await transport.send(SendRequest(text=SYSTEM_USER_PROMPT))

    # Exactly one browser re-mint happened between the two POSTs...
    assert manager.extract_calls == 1
    assert len(header_attempts) == 2
    first, second = header_attempts
    # ...and the retry carries the FRESH envelope, not the challenged one.
    assert first["Authorization"] == "Bearer access-token"
    assert first["Cookie"].count("cf_clearance=clearance") == 1
    assert second["Authorization"] == "Bearer access-token-v3"
    assert "cf_clearance=reminted-clearance" in second["Cookie"]
    assert "cf_clearance=clearance" not in second["Cookie"]
    assert result.status == "completed"
    assert result.text == "Xin chào!"


@pytest.mark.anyio
async def test_codex_delta_before_created_buffers_until_real_turn_id(monkeypatch):
    # Review round 10 POSSIBLE: deltas preceding response.created used to be
    # emitted under the random placeholder turn_id, which can never be
    # corrected once the real id arrives.  They must stay buffered instead.
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    payload = _sse_bytes(
        '{"type":"response.output_text.delta","delta":"early "}',
        '{"type":"response.created","response":{"id":"resp_late"}}',
        '{"type":"response.output_text.delta","delta":"late"}',
    )
    session = FakeSession(FakeResponse(payload))
    transport = CurlCffiTransport(FakeTokenManager(), session=session)
    deltas: list[tuple[str, str]] = []

    async def on_delta(text: str, turn_id: str) -> None:
        deltas.append((text, turn_id))

    result = await transport.send(
        SendRequest(text=SYSTEM_USER_PROMPT), on_delta=on_delta
    )

    # No callback ever sees the placeholder id; buffered text flushes in
    # order once the authoritative id lands.
    assert all(turn == "resp_late" for _, turn in deltas)
    assert deltas == [("early ", "resp_late"), ("late", "resp_late")]
    assert "".join(text for text, _ in deltas) == result.text == "early late"
    assert result.turn_id == "resp_late"


def test_codex_flag_defaults_off():
    assert CurlCffiTransport._codex_sse_enabled() is False


# --- CLI-shape parity guards (next-horizon research 2026-08-25, F1.3 /
# David-Factor patch rules): OpenAI's sub2api flag (2026-08-21) makes payload
# divergence from codex_cli_rs the clearest detection surface, so the codex
# branch must match the official envelope before shape freeze. ---

import re  # noqa: E402
import uuid  # noqa: E402

from gpt.transport.curl_transport import _CODEX_VERSION  # noqa: E402


def test_codex_payload_has_no_token_caps_or_reasoning_items():
    transport = CurlCffiTransport(FakeTokenManager(), session=FakeSession())

    payload = transport._build_codex_payload(SendRequest(text=SYSTEM_USER_PROMPT))

    # official CLI never sends token caps
    assert "max_output_tokens" not in payload
    assert "max_completion_tokens" not in payload
    types = [item.get("type") for item in payload["input"]]
    assert "reasoning" not in types  # store:false ⇒ replayed reasoning stripped


def test_strip_reasoning_items_keeps_everything_else():
    poisoned = [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "hm"}]},
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "keep me"}],
        },
        {"type": "reasoning"},
    ]

    kept = CurlCffiTransport._strip_reasoning_items(poisoned)

    assert len(kept) == 1
    assert kept[0]["content"][0]["text"] == "keep me"


@pytest.mark.anyio
async def test_codex_headers_carry_stable_session_id_and_version(monkeypatch):
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    session = FakeSession()
    transport = CurlCffiTransport(FakeTokenManager(), session=session)

    await transport.send(SendRequest(text=SYSTEM_USER_PROMPT))
    headers = session.calls[0][1]["headers"]

    parsed = uuid.UUID(headers["session_id"])  # must be a valid uuid
    assert parsed.version == 4
    assert headers["version"] == _CODEX_VERSION
    assert re.fullmatch(r"\d+\.\d+\.\d+", headers["version"])

    await transport.send(SendRequest(text=SYSTEM_USER_PROMPT))
    assert session.calls[1][1]["headers"]["session_id"] == headers["session_id"]

    other_session = FakeSession()
    other = CurlCffiTransport(FakeTokenManager(), session=other_session)
    await other.send(SendRequest(text=SYSTEM_USER_PROMPT))
    assert other_session.calls[0][1]["headers"]["session_id"] != headers["session_id"]


@pytest.mark.anyio
async def test_codex_instructions_stay_short_and_prose_moves_to_input():
    transport = CurlCffiTransport(FakeTokenManager(), session=FakeSession())
    contract = "<WEBGPT_PROTOCOL> " + ("x" * 4000) + " </WEBGPT_PROTOCOL>"
    prompt = (
        f"{contract}\n\n"
        '<WEBGPT_MESSAGE role="system">\n{"content":"You are WebGPT."}\n</WEBGPT_MESSAGE>\n\n'
        '<WEBGPT_MESSAGE role="user">\n{"content":"hello"}\n</WEBGPT_MESSAGE>'
    )

    payload = transport._build_codex_payload(SendRequest(text=prompt))

    assert payload["instructions"] == "You are WebGPT."
    assert len(payload["instructions"]) < 200  # CLI-like: short system block only
    assert "WEBGPT_PROTOCOL" not in payload["instructions"]
    joined = "\n".join(
        item["content"][0]["text"]
        for item in payload["input"]
        if item.get("type") == "message"
    )
    assert contract in joined  # controller prose preserved as input, not dropped


# --- CODEX-IMG-INPUT (2026-08-26): /v1/responses input_image → codex
# input_image data URLs.  gpt.api.server encodes inline images as
# <WEBGPT_IMAGE_DATA> markers when WEBGPT_CODEX_SSE is on; the codex payload
# builder expands them back into Responses-API image parts. ---

import copy  # noqa: E402
import logging  # noqa: E402

from gpt.api import server as api_server  # noqa: E402
from gpt.api.protocol_adapters import parse_responses_request  # noqa: E402
from gpt.transport import curl_transport as ct_mod  # noqa: E402

PNG_BASE64 = "iVBORw0KGgo="  # PNG magic bytes, real base64 alphabet


def _marker(mime: str = "image/png", data: str = PNG_BASE64) -> str:
    return f'<WEBGPT_IMAGE_DATA mime="{mime}">{data}</WEBGPT_IMAGE_DATA>'


def _user_prompt_with_content(content: str) -> str:
    """Render one user message exactly like the controller render layer does
    (json.dumps + '<' escaped to \\u003c inside the block body)."""
    encoded = json.dumps({"content": content}, ensure_ascii=False).replace("<", "\\u003c")
    return f'<WEBGPT_MESSAGE role="user">\n{encoded}\n</WEBGPT_MESSAGE>'


def _user_parts(payload: dict) -> list[dict]:
    items = [
        item
        for item in payload["input"]
        if item.get("type") == "message" and item.get("role") == "user"
    ]
    parts: list[dict] = []
    for item in items:
        parts.extend(item["content"])
    return parts


class monkeypatch_context:
    """Tiny context manager so sync tests can patch class/module attrs."""

    def __init__(self, target, name, value):
        self.target = target
        self.name = name
        self.value = value
        self.old = None

    def __enter__(self):
        self.old = getattr(self.target, self.name)
        setattr(self.target, self.name, self.value)
        return self

    def __exit__(self, *exc_info):
        setattr(self.target, self.name, self.old)


@pytest.mark.anyio
async def test_codex_payload_expands_ingress_marker_to_input_image():
    transport = CurlCffiTransport(FakeTokenManager(), session=FakeSession())
    prompt = _user_prompt_with_content(f"screenshot:\n{_marker()}")

    payload = transport._build_codex_payload(SendRequest(text=prompt))

    user_parts = _user_parts(payload)
    images = [part for part in user_parts if part["type"] == "input_image"]
    texts = [part for part in user_parts if part["type"] == "input_text"]
    assert images == [
        {"type": "input_image", "image_url": f"data:image/png;base64,{PNG_BASE64}"}
    ]
    assert len(texts) == 1
    assert texts[0]["text"].startswith("screenshot:")
    # No raw marker survives anywhere in the outgoing envelope.
    dumped = json.dumps(payload)
    assert "WEBGPT_IMAGE_DATA" not in dumped
    assert PNG_BASE64 in images[0]["image_url"]


@pytest.mark.anyio
async def test_codex_ingress_to_payload_full_roundtrip(monkeypatch):
    """End-to-end through the real ingress mutation + request parser: a
    /v1/responses body carrying input_image lands as a correctly shaped
    codex input_image with the original mime and payload."""
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    body = {
        "model": "gpt-5.2",
        "stream": False,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "what is this?"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{PNG_BASE64}",
                    },
                    {"type": "input_text", "text": "be precise"},
                ],
            }
        ],
    }
    api_server._inject_codex_image_markers(body)

    types = [block["type"] for block in body["input"][0]["content"]]
    assert types == ["input_text", "input_text", "input_text"]  # image → marker text
    adapted = parse_responses_request(body)
    user_message = adapted.request.messages[-1]["content"]
    prompt = _user_prompt_with_content(user_message)

    transport = CurlCffiTransport(FakeTokenManager(), session=FakeSession())
    payload = transport._build_codex_payload(SendRequest(text=prompt))

    user_parts = _user_parts(payload)
    images = [part for part in user_parts if part["type"] == "input_image"]
    assert images == [
        {"type": "input_image", "image_url": f"data:image/jpeg;base64,{PNG_BASE64}"}
    ]
    texts = [part["text"] for part in user_parts if part["type"] == "input_text"]
    assert any(text.startswith("what is this?") for text in texts)
    assert any("be precise" in text for text in texts)


def test_codex_payload_text_only_is_untouched():
    transport = CurlCffiTransport(FakeTokenManager(), session=FakeSession())

    _, items = CurlCffiTransport._split_prompt_for_responses(SYSTEM_USER_PROMPT)
    expanded = CurlCffiTransport._expand_image_markers(items)
    assert expanded is items  # identical objects ⇒ byte-identical serialization

    payload = transport._build_codex_payload(SendRequest(text=SYSTEM_USER_PROMPT))
    assert "input_image" not in json.dumps(payload)
    assert payload["instructions"] == "You are WebGPT."
    assert payload["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }
    ]


def test_codex_truncated_marker_stays_inert_text():
    transport = CurlCffiTransport(FakeTokenManager(), session=FakeSession())
    truncated = (
        '<WEBGPT_IMAGE_DATA mime="image/png">' + PNG_BASE64
    )  # no closing tag (e.g. budget-trimmed)
    prompt = _user_prompt_with_content(truncated)

    payload = transport._build_codex_payload(SendRequest(text=prompt))

    user_parts = _user_parts(payload)
    assert all(part["type"] == "input_text" for part in user_parts)
    assert any(PNG_BASE64 in part.get("text", "") for part in user_parts)


@pytest.mark.anyio
async def test_codex_disabled_legacy_path_degrades_markers_to_notes(monkeypatch):
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)

    class LegacyResponse(FakeResponse):
        async def aiter_bytes(self):
            yield b'data: {"conversation_id":"conversation-1","message":{"id":"turn-1","content":{"parts":["Hi"]},"status":"finished_successfully"}}\n\n'

    session = FakeSession(LegacyResponse())
    transport = CurlCffiTransport(FakeTokenManager(), session=session)
    request = SendRequest(
        text=f"look\n{_marker()}\nthanks",
        conversation_id="conversation-1",
    )

    await transport.send(request)

    body = session.calls[0][1]["json"]
    part = body["messages"][0]["content"]["parts"][0]
    assert "WEBGPT_IMAGE_DATA" not in part
    assert PNG_BASE64 not in part
    assert "[image omitted: image/png]" in part


def test_codex_transport_side_oversize_guard_emits_note(caplog):
    transport = CurlCffiTransport(FakeTokenManager(), session=FakeSession())
    prompt = _user_prompt_with_content(_marker(data="QUJDREVGRw"))  # 10 chars

    with (
        caplog.at_level(logging.WARNING, logger="gpt.transport.curl"),
        monkeypatch_context(ct_mod, "_CODEX_IMAGE_MAX_B64_CHARS", 8),
    ):
        payload = transport._build_codex_payload(SendRequest(text=prompt))

    user_parts = _user_parts(payload)
    assert not any(part["type"] == "input_image" for part in user_parts)
    assert any(
        part.get("text", "").startswith("[image omitted: image/png ~0KB exceeds")
        for part in user_parts
    )
    assert any("upload cap" in record.message for record in caplog.records)


# --- Ingress-side tests (gpt.api.server CODEX-IMG-INPUT helpers) ---


def test_responses_ingress_noop_when_codex_flag_off(monkeypatch):
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)
    body = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "hi"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{PNG_BASE64}",
                    },
                ],
            }
        ]
    }
    snapshot = copy.deepcopy(body)

    api_server._inject_codex_image_markers(body)

    assert body == snapshot  # non-codex paths keep byte-identical behavior


def test_responses_ingress_oversize_image_skipped_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    big_data = PNG_BASE64 * 4  # 48 chars
    body = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{big_data}",
                    }
                ],
            }
        ]
    }

    with (
        caplog.at_level(logging.WARNING, logger="gpt.webchat.api"),
        monkeypatch_context(api_server, "_CODEX_IMAGE_MAX_B64_CHARS", 16),
    ):
        api_server._inject_codex_image_markers(body)

    block = body["input"][0]["content"][0]
    assert block["type"] == "input_text"
    assert "WEBGPT_IMAGE_DATA" not in block["text"]
    assert block["text"] == "[image omitted: image/png ~0KB exceeds upload cap]"
    assert any("exceeds" in record.message for record in caplog.records)


def test_responses_ingress_unsupported_and_malformed_sources_become_notes(monkeypatch):
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    body = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": "https://example.com/x.png"},
                    {"type": "input_image", "image_url": "data:image/png;base64,"},
                    {"type": "input_image", "image_url": "data:image/png;base64,not/b64!"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/webp",
                            "data": PNG_BASE64,
                        },
                    },
                ],
            }
        ]
    }

    api_server._inject_codex_image_markers(body)

    texts = [block["text"] for block in body["input"][0]["content"]]
    assert texts[0] == "[image omitted: unsupported image source]"
    assert texts[1] == "[image omitted: malformed image payload]"
    assert texts[2] == "[image omitted: malformed image payload]"
    # Anthropic-style base64 sources are accepted like data URLs.
    assert texts[3] == _marker(mime="image/webp")


def test_responses_ingress_ignores_non_user_roles(monkeypatch):
    monkeypatch.setenv("WEBGPT_CODEX_SSE", "1")
    body = {
        "input": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{PNG_BASE64}",
                    }
                ],
            }
        ]
    }
    snapshot = copy.deepcopy(body)

    api_server._inject_codex_image_markers(body)

    assert body == snapshot
