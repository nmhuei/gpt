"""MODEL-ROUTING-PHASE1+2 (2026-08-26): opt-in WEBGPT_MODEL_ALIAS routing.

Covers research row S (docs/reports/model-routing-research-2026-08-26.md):
env alias-map application at payload-build time, EFFORT-FIRST thinking_effort
pinning, byte-identical passthrough for unmapped models, and the requested-
vs-served downgrade WARNING on the f/conversation SSE path.  No live calls:
every test rides the fake token manager / fake SSE session used by
tests/test_curl_transport.py.

PHASE2 additions (same file, docs/reports/model-routing-phase2-2026-08-26.md):
per-request downgrade telemetry fields on TurnResult (resolved_model /
model_downgraded / model_downgrade_count), the WEBGPT_MODEL_FALLBACK policy
parser, warn-path byte-identity (single upstream POST, no marker), and the
retry-once default-model fallback with its ``[webgpt:model-fallback …]``
stream marker.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from gpt.transport.curl_transport import (
    CurlCffiTransport,
    ModelRoute,
    parse_model_alias_env,
    parse_model_fallback_env,
)
from gpt.types import ModelInfo, SendRequest

_ALIAS_ENV = "WEBGPT_MODEL_ALIAS"
_FALLBACK_ENV = "WEBGPT_MODEL_FALLBACK"
_PAIR_MAP = "claude-sonnet-4-5=gpt-5-5-thinking:low,claude-haiku-4-5=gpt-5-5-mini"
_MARKER_PREFIX = "[webgpt:model-fallback gpt-5-5-thinking→gpt-5-3-mini]\n"


class FakeTokenManager:
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


def make_sse_session(records: list[bytes]):
    class FakeResponse:
        status_code = 200
        closed = False

        def __init__(self, chunks):
            self._chunks = chunks

        async def aiter_bytes(self):
            for chunk in self._chunks:
                yield chunk

        async def aclose(self):
            self.closed = True

    class FakeSession:
        def __init__(self):
            self.response = FakeResponse(records)
            self.calls = []

        async def post(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return self.response

    return FakeSession()


def make_multi_sse_session(responses: list[list[bytes]]):
    """FakeSession popping one SSE body per successive .post call.

    Used by the PHASE2 fallback tests to give the first attempt and the
    retry-once attempt different served slugs.
    """

    class FakeResponse:
        status_code = 200
        closed = False

        def __init__(self, chunks):
            self._chunks = chunks

        async def aiter_bytes(self):
            for chunk in self._chunks:
                yield chunk

        async def aclose(self):
            self.closed = True

    class FakeQueueSession:
        def __init__(self):
            self.calls = []
            self.bodies = [list(chunks) for chunks in responses]

        async def post(self, *args, **kwargs):
            index = min(len(self.calls), len(self.bodies) - 1)
            self.calls.append((args, kwargs))
            return FakeResponse(self.bodies[index])

    return FakeQueueSession()


# ---------------------------------------------------------------------------
# Env parsing


def test_parse_model_alias_env_accepts_pair_and_json_forms():
    pairs = parse_model_alias_env(_PAIR_MAP)
    assert pairs == {
        "claude-sonnet-4-5": ModelRoute(slug="gpt-5-5-thinking", effort="low"),
        "claude-haiku-4-5": ModelRoute(slug="gpt-5-5-mini", effort=None),
    }
    as_json = parse_model_alias_env(
        '{"Claude-Sonnet-4-5": "gpt-5-5-thinking", "x": "slug:instant"}'
    )
    # Keys are casefolded+stripped exactly like ModelRegistry lookups.
    assert as_json["claude-sonnet-4-5"].slug == "gpt-5-5-thinking"
    assert as_json["x"] == ModelRoute(slug="slug", effort="instant")


def test_parse_model_alias_env_empty_and_malformed():
    assert parse_model_alias_env(None) == {}
    assert parse_model_alias_env("") == {}
    assert parse_model_alias_env("   ") == {}
    for bad in (
        "{not json",
        '{"a": 1}',
        "missing-separator",
        "=gpt-5",
        "a=",
        "a=:low",
        "a=slug:",
        "a=b,c-without-value",
    ):
        with pytest.raises(ValueError, match="WEBGPT_MODEL_ALIAS"):
            parse_model_alias_env(bad)


# ---------------------------------------------------------------------------
# f/conversation payload builder


def _fconv_request(**overrides) -> SendRequest:
    defaults: dict[str, Any] = dict(
        text="Hello",
        conversation_id="conversation-1",
        model=ModelInfo(id="claude-sonnet-4-5", label="Claude Sonnet 4.5"),
    )
    defaults.update(overrides)
    return SendRequest(**defaults)


@pytest.mark.anyio
async def test_alias_maps_fconv_model_and_pins_effort(monkeypatch):
    monkeypatch.setenv(_ALIAS_ENV, _PAIR_MAP)
    transport = CurlCffiTransport(FakeTokenManager(), session=make_sse_session([]))
    payload = await transport._maybe_build_multimodal_payload(
        None, _fconv_request(), enabled=False
    )
    assert payload["model"] == "gpt-5-5-thinking"
    assert payload["thinking_effort"] == "low"


def _stable_json(payload: dict) -> str:
    """json with per-turn uuids pinned, so only real shape diffs survive."""
    clone = json.loads(json.dumps(payload))
    clone["messages"][0]["id"] = "<turn-uuid>"
    clone["parent_message_id"] = "<parent-uuid>"
    return json.dumps(clone, sort_keys=True)


@pytest.mark.anyio
async def test_unmapped_model_keeps_legacy_payload_byte_identical(monkeypatch):
    transport = CurlCffiTransport(FakeTokenManager(), session=make_sse_session([]))
    request = _fconv_request(model=ModelInfo(id="claude-opus-9-9", label="x"))
    monkeypatch.delenv(_ALIAS_ENV, raising=False)
    legacy = await transport._maybe_build_multimodal_payload(
        None, request, enabled=False
    )
    # Alias map present but this model has no key: every byte identical
    # (fresh message/parent uuids excluded from the comparison).
    monkeypatch.setenv(_ALIAS_ENV, _PAIR_MAP)
    unmapped = await transport._maybe_build_multimodal_payload(
        None, request, enabled=False
    )
    assert _stable_json(unmapped) == _stable_json(legacy)
    assert unmapped["model"] == "claude-opus-9-9"
    assert "thinking_effort" not in unmapped
    # And the canonical legacy shape is untouched overall.
    assert unmapped["action"] == "next"
    assert unmapped["conversation_mode"] == {"kind": "primary_assistant"}
    assert unmapped["messages"][0]["content"]["content_type"] == "text"


@pytest.mark.anyio
async def test_client_effort_overrides_alias_effort(monkeypatch):
    monkeypatch.setenv(_ALIAS_ENV, _PAIR_MAP)
    transport = CurlCffiTransport(FakeTokenManager(), session=make_sse_session([]))
    payload = await transport._maybe_build_multimodal_payload(
        None, _fconv_request(reasoning_effort="high"), enabled=False
    )
    assert payload["thinking_effort"] == "high"


@pytest.mark.anyio
async def test_no_effort_source_omits_thinking_effort(monkeypatch):
    monkeypatch.setenv(_ALIAS_ENV, "claude-sonnet-4-5=gpt-5-5-thinking")
    transport = CurlCffiTransport(FakeTokenManager(), session=make_sse_session([]))
    payload = await transport._maybe_build_multimodal_payload(
        None, _fconv_request(), enabled=False
    )
    assert payload["model"] == "gpt-5-5-thinking"
    assert "thinking_effort" not in payload


# ---------------------------------------------------------------------------
# codex/responses payload builder


@pytest.mark.anyio
async def test_alias_applies_to_codex_payload_with_dotted_slug(monkeypatch):
    transport = CurlCffiTransport(FakeTokenManager(), session=make_sse_session([]))
    request = _fconv_request(model=ModelInfo(id="claude-sonnet-4-5", label="x"))
    monkeypatch.delenv(_ALIAS_ENV, raising=False)
    # Legacy codex behavior passes the raw id through; _DEFAULT_CODEX_MODEL
    # only applies when the request carries no model id at all.
    legacy = transport._build_codex_payload(request)
    assert legacy["model"] == "claude-sonnet-4-5"
    bare = transport._build_codex_payload(SendRequest(text="Hello"))
    assert bare["model"] == "gpt-5"
    monkeypatch.setenv(_ALIAS_ENV, "claude-sonnet-4-5=gpt-5.2")
    routed = transport._build_codex_payload(request)
    assert routed["model"] == "gpt-5.2"
    assert "reasoning" not in routed and "thinking_effort" not in routed


# ---------------------------------------------------------------------------
# Requested-vs-served verification on the fconv SSE stream


def _sse_record(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def _sse_chunks(served_slug: str | None) -> list[bytes]:
    metadata = {"model_slug": served_slug} if served_slug else {}
    return [
        _sse_record(
            {
                "conversation_id": "conversation-1",
                "message": {
                    "id": "turn-1",
                    "content": {"parts": ["Hel"]},
                    "metadata": metadata,
                },
            }
        ),
        _sse_record(
            {
                "message": {
                    "content": {"parts": ["Hello"]},
                    "status": "finished_successfully",
                }
            }
        ),
    ]


@pytest.mark.anyio
async def test_served_slug_mismatch_warns_but_completes(monkeypatch, caplog):
    monkeypatch.delenv(_ALIAS_ENV, raising=False)
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)
    monkeypatch.delenv("WEBGPT_FCONV_PREPARE", raising=False)
    session = make_sse_session(_sse_chunks("gpt-5-3-mini"))
    transport = CurlCffiTransport(FakeTokenManager(), session=session)
    with caplog.at_level(logging.WARNING, logger="gpt.transport.curl"):
        result = await transport.send(
            _fconv_request(model=ModelInfo(id="gpt-5-5-thinking", label="x"))
        )
    assert result.text == "Hello"
    assert result.status == "completed"
    assert result.requested_model == "gpt-5-5-thinking"
    assert result.model == "gpt-5-3-mini"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "MODEL-ROUTING mismatch" in r.getMessage()
        and "gpt-5-5-thinking" in r.getMessage()
        and "gpt-5-3-mini" in r.getMessage()
        for r in warnings
    )


@pytest.mark.anyio
async def test_matching_or_absent_slug_never_warns(monkeypatch, caplog):
    monkeypatch.delenv(_ALIAS_ENV, raising=False)
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)
    monkeypatch.delenv("WEBGPT_FCONV_PREPARE", raising=False)
    transport = CurlCffiTransport(FakeTokenManager(), session=make_sse_session([]))

    for chunks in (_sse_chunks("gpt-5-5-thinking"), _sse_chunks(None)):
        transport._session = make_sse_session(chunks)
        with caplog.at_level(logging.WARNING, logger="gpt.transport.curl"):
            result = await transport.send(
                _fconv_request(model=ModelInfo(id="gpt-5-5-thinking", label="x"))
            )
        assert result.status == "completed"
        assert result.requested_model == "gpt-5-5-thinking"
        assert not [
            r for r in caplog.records if "MODEL-ROUTING mismatch" in r.getMessage()
        ], f"unexpected mismatch warning for served={result.model!r}"
        caplog.clear()


# ---------------------------------------------------------------------------
# MODEL-ROUTING-PHASE2: downgrade telemetry + WEBGPT_MODEL_FALLBACK policy


def test_parse_model_fallback_env_policies():
    assert parse_model_fallback_env(None) == "warn"
    assert parse_model_fallback_env("") == "warn"
    assert parse_model_fallback_env("   ") == "warn"
    assert parse_model_fallback_env("warn") == "warn"
    assert parse_model_fallback_env("WARN") == "warn"
    assert parse_model_fallback_env("retry-once") == "retry-once"
    assert parse_model_fallback_env("  Retry-Once ") == "retry-once"
    with pytest.raises(ValueError, match="WEBGPT_MODEL_FALLBACK"):
        parse_model_fallback_env("retry-twice")


@pytest.mark.anyio
async def test_downgrade_telemetry_fields_populated_on_mismatch(monkeypatch):
    monkeypatch.delenv(_ALIAS_ENV, raising=False)
    monkeypatch.delenv(_FALLBACK_ENV, raising=False)
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)
    monkeypatch.delenv("WEBGPT_FCONV_PREPARE", raising=False)
    transport = CurlCffiTransport(
        FakeTokenManager(), session=make_sse_session(_sse_chunks("gpt-5-3-mini"))
    )
    result = await transport.send(
        _fconv_request(model=ModelInfo(id="gpt-5-5-thinking", label="x"))
    )
    assert result.requested_model == "gpt-5-5-thinking"
    assert result.resolved_model == "gpt-5-3-mini"
    assert result.model_downgraded is True
    assert result.model_downgrade_count == 1


@pytest.mark.anyio
async def test_matching_or_absent_slug_zero_downgrade_telemetry(monkeypatch):
    monkeypatch.delenv(_ALIAS_ENV, raising=False)
    monkeypatch.delenv(_FALLBACK_ENV, raising=False)
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)
    monkeypatch.delenv("WEBGPT_FCONV_PREPARE", raising=False)
    transport = CurlCffiTransport(FakeTokenManager(), session=make_sse_session([]))

    transport._session = make_sse_session(_sse_chunks("gpt-5-5-thinking"))
    matched = await transport.send(
        _fconv_request(model=ModelInfo(id="gpt-5-5-thinking", label="x"))
    )
    assert matched.model_downgraded is False
    assert matched.model_downgrade_count == 0
    assert matched.resolved_model == "gpt-5-5-thinking"

    transport._session = make_sse_session(_sse_chunks(None))
    absent = await transport.send(
        _fconv_request(model=ModelInfo(id="gpt-5-5-thinking", label="x"))
    )
    assert absent.model_downgraded is False
    assert absent.model_downgrade_count == 0
    assert absent.resolved_model is None


@pytest.mark.anyio
async def test_warn_default_single_post_no_marker(monkeypatch, caplog):
    """Default (warn) policy: telemetry + WARNING only, zero extra POSTs."""
    monkeypatch.delenv(_ALIAS_ENV, raising=False)
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)
    monkeypatch.delenv("WEBGPT_FCONV_PREPARE", raising=False)
    for fallback in (None, "warn"):
        if fallback is None:
            monkeypatch.delenv(_FALLBACK_ENV, raising=False)
        else:
            monkeypatch.setenv(_FALLBACK_ENV, fallback)
        session = make_multi_sse_session([_sse_chunks("gpt-5-3-mini"), []])
        transport = CurlCffiTransport(FakeTokenManager(), session=session)
        with caplog.at_level(logging.WARNING, logger="gpt.transport.curl"):
            result = await transport.send(
                _fconv_request(model=ModelInfo(id="gpt-5-5-thinking", label="x"))
            )
        # warn path byte-identical to phase 1: one POST, no marker, full text.
        assert len(session.calls) == 1
        assert result.text == "Hello"
        assert not result.text.startswith("[webgpt:model-fallback")
        assert result.status == "completed"
        assert result.model_downgraded is True
        assert result.model_downgrade_count == 1
        assert any("MODEL-ROUTING mismatch" in r.getMessage() for r in caplog.records)
        caplog.clear()


@pytest.mark.anyio
async def test_retry_once_reposts_default_model_with_marker(monkeypatch, caplog):
    monkeypatch.setenv(_ALIAS_ENV, "claude-sonnet-4-5=gpt-5-5-thinking:low")
    monkeypatch.setenv(_FALLBACK_ENV, "retry-once")
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)
    monkeypatch.delenv("WEBGPT_FCONV_PREPARE", raising=False)
    deltas: list[str] = []

    async def collect(delta: str, turn_id: str) -> None:
        deltas.append(delta)

    session = make_multi_sse_session(
        [_sse_chunks("gpt-5-3-mini"), _sse_chunks(None)]
    )
    transport = CurlCffiTransport(FakeTokenManager(), session=session)
    with caplog.at_level(logging.WARNING, logger="gpt.transport.curl"):
        result = await transport.send(
            _fconv_request(model=ModelInfo(id="claude-sonnet-4-5", label="x")),
            on_delta=collect,
        )
    # Exactly ONE extra upstream call was made.
    assert len(session.calls) == 2
    first_payload = session.calls[0][1]["json"]
    retry_payload = session.calls[1][1]["json"]
    assert first_payload["model"] == "gpt-5-5-thinking"
    assert first_payload["thinking_effort"] == "low"  # alias pin from _PAIR-less map
    # Retry carries the default model and drops the alias-pinned effort.
    assert retry_payload["model"] == "auto"
    assert "thinking_effort" not in retry_payload
    # Fresh per-turn uuids on the retry envelope.
    assert (
        retry_payload["messages"][0]["id"] != first_payload["messages"][0]["id"]
    )
    assert retry_payload["parent_message_id"] != first_payload["parent_message_id"]
    # Marker heads the retry's own first streamed delta (attempt one streamed
    # its plain deltas first), and heads the final text.
    assert deltas[0] == "Hel"
    marker_positions = [
        i for i, delta in enumerate(deltas) if delta.startswith(_MARKER_PREFIX)
    ]
    assert len(marker_positions) == 1 and marker_positions[0] > 0
    assert result.text.startswith(_MARKER_PREFIX)
    assert result.text.endswith("Hello")
    assert result.status == "completed"
    # Telemetry still describes the attempt-one downgrade event.
    assert result.requested_model == "gpt-5-5-thinking"
    assert result.resolved_model == "gpt-5-3-mini"
    assert result.model_downgraded is True
    assert result.model_downgrade_count == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("MODEL-ROUTING fallback" in message for message in warnings)


@pytest.mark.anyio
async def test_retry_once_skips_when_served_matches(monkeypatch):
    monkeypatch.delenv(_ALIAS_ENV, raising=False)
    monkeypatch.setenv(_FALLBACK_ENV, "retry-once")
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)
    monkeypatch.delenv("WEBGPT_FCONV_PREPARE", raising=False)
    session = make_multi_sse_session([_sse_chunks("gpt-5-5-thinking")])
    transport = CurlCffiTransport(FakeTokenManager(), session=session)
    result = await transport.send(
        _fconv_request(model=ModelInfo(id="gpt-5-5-thinking", label="x"))
    )
    assert len(session.calls) == 1
    assert result.text == "Hello"
    assert result.model_downgraded is False
    assert result.model_downgrade_count == 0


@pytest.mark.anyio
async def test_retry_once_failure_keeps_original_result(monkeypatch, caplog):
    """KHÔNG fail-hard: an exploding retry never loses the original turn."""
    monkeypatch.setenv(_ALIAS_ENV, "claude-sonnet-4-5=gpt-5-5-thinking:low")
    monkeypatch.setenv(_FALLBACK_ENV, "retry-once")
    monkeypatch.delenv("WEBGPT_CODEX_SSE", raising=False)
    monkeypatch.delenv("WEBGPT_FCONV_PREPARE", raising=False)

    class ExplodingRetrySession:
        def __init__(self):
            self.calls = []
            self.first_body = make_sse_session(_sse_chunks("gpt-5-3-mini")).response

        async def post(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            if len(self.calls) > 1:
                raise RuntimeError("upstream exploded during the retry")
            return self.first_body

    session = ExplodingRetrySession()
    transport = CurlCffiTransport(FakeTokenManager(), session=session)
    with caplog.at_level(logging.WARNING, logger="gpt.transport.curl"):
        result = await transport.send(
            _fconv_request(model=ModelInfo(id="claude-sonnet-4-5", label="x"))
        )
    assert len(session.calls) == 2
    assert result.text == "Hello"
    assert not result.text.startswith("[webgpt:model-fallback")
    assert result.status == "completed"
    assert result.model_downgraded is True
    assert result.model_downgrade_count == 1
    assert any(
        "MODEL-ROUTING fallback retry failed" in r.getMessage() for r in caplog.records
    )
