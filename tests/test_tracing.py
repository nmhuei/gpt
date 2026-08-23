import json
import stat
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from gpt.api.server import create_api_app
from gpt.requests import parse_chat_completion_request
from gpt.tracing import RuntimeTraceBus
from gpt.types import StateChanged, TurnResult


def test_runtime_trace_file_is_private_and_structural(tmp_path):
    target = tmp_path / "trace" / "runtime.jsonl"
    bus = RuntimeTraceBus(output_path=target)
    bus.emit(
        "completionruntime",
        "submit_start",
        session_id="wgs_1",
        metadata={"prompt_chars": 123},
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    raw = target.read_text(encoding="utf-8")
    assert "submit_start" in raw
    assert "prompt_chars" in raw


@pytest.mark.anyio
async def test_completion_runtime_emits_ordered_boundary_trace(monkeypatch):
    app = create_api_app()
    server = app.state.server
    session = AsyncMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="trace_turn",
            conversation_id="trace_conv",
            text="trace answer",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))
    request = parse_chat_completion_request(
        {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": "TRACE_SECRET_MARKER"}],
        }
    )

    response, _record = await server.complete_normalized(request)
    assert response["choices"][0]["message"]["content"] == "trace answer"
    events = server.trace.snapshot()
    kinds = [(event.component, event.kind) for event in events]
    assert kinds == [
        ("completionruntime", "lease_acquired"),
        ("promptcompat", "prompt_built"),
        ("conversation_store", "pending_marked"),
        ("webchat", "position_start"),
        ("webchat", "position_done"),
        ("completionruntime", "submit_start"),
        ("completionruntime", "submit_completed"),
        ("assistantturn", "parsed"),
    ]
    assert all("TRACE_SECRET_MARKER" not in repr(event.metadata) for event in events)


def test_http_request_trace_has_master_observability_envelope(monkeypatch):
    app = create_api_app()
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
            turn_id="trace_http_turn",
            conversation_id="trace_http_conv",
            text="HTTP_TRACE_OK",
            duration_ms=17,
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"user-agent": "openai-python/1.0"},
            json={
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": "TRACE_HTTP_SECRET"}],
            },
        )

    assert response.status_code == 200
    completed = [
        event
        for event in server.trace.snapshot()
        if event.component == "api" and event.kind == "request_completed"
    ]
    assert len(completed) == 1
    event = completed[0]
    metadata = event.metadata
    assert metadata["request_id"].startswith("req_")
    assert metadata["client"] == "openai-client"
    assert metadata["protocol"] == "openai_chat"
    assert metadata["gateway_session_id"] == response.headers["x-webgpt-session-id"]
    assert metadata["browser_conversation_id"] == "trace_http_conv"
    assert metadata["turn_id"] == "trace_http_turn"
    assert metadata["duration_ms"] >= 0
    assert metadata["queue_ms"] >= 0
    assert metadata["browser_ms"] >= 0
    assert metadata["parse_ms"] >= 0
    assert metadata["tool_count"] == 0
    assert metadata["correction_count"] == 0
    assert metadata["status"] == "ok"
    assert metadata["http_status"] == 200
    assert metadata["error"] is None
    assert "TRACE_HTTP_SECRET" not in repr(metadata)


def test_runtime_trace_parent_chmod_permission_error_does_not_break_file_write(tmp_path, monkeypatch):
    target = tmp_path / "foreign-parent" / "runtime.jsonl"
    real_chmod = __import__("os").chmod

    def fake_chmod(path, mode):
        if str(path).endswith("foreign-parent"):
            raise PermissionError("parent owned by test fixture")
        return real_chmod(path, mode)

    monkeypatch.setattr("gpt.tracing.os.chmod", fake_chmod)
    bus = RuntimeTraceBus(output_path=target)
    bus.emit("component", "kind")

    assert target.read_text(encoding="utf-8").strip()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.anyio
async def test_prompt_debug_dir_writes_redacted_pre_gpt_prompt(monkeypatch, tmp_path):
    prompt_dir = tmp_path / "prompt-debug"
    app = create_api_app(prompt_debug_dir=str(prompt_dir))
    server = app.state.server
    session = AsyncMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="debug_turn",
            conversation_id="debug_conv",
            text="debug answer",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))
    request = parse_chat_completion_request(
        {
            "model": "chatgpt-web",
            "messages": [
                {"role": "user", "content": "Bearer sk-testsecret12345678901234567890 visible-task"}
            ],
        },
        protocol="anthropic_messages",
        client="claude-code",
    )

    response, _record = await server.complete_normalized(request)

    assert response["choices"][0]["message"]["content"] == "debug answer"
    dumps = list(prompt_dir.glob("*.txt"))
    metadata_files = list(prompt_dir.glob("*.json"))
    assert len(dumps) == 1
    assert len(metadata_files) == 1
    metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    assert metadata["protocol"] == "anthropic_messages"
    assert metadata["client"] == "claude-code"
    assert metadata["correction"] is False
    assert metadata["prompt_sha256"]
    raw = dumps[0].read_text(encoding="utf-8")
    assert "WEBGPT PRE-GPT PROMPT DEBUG" in raw
    assert "REDACTED_PROMPT_SENT_TO_CHATGPT_WEB" in raw
    assert "visible-task" in raw
    assert "sk-testsecret" not in raw
    assert "<REDACTED>" in raw
    assert stat.S_IMODE(dumps[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(prompt_dir.stat().st_mode) == 0o700
    kinds = [(event.component, event.kind) for event in server.trace.snapshot()]
    assert ("promptcompat", "prompt_debug_written") in kinds


@pytest.mark.anyio
async def test_completion_runtime_traces_state_transition_evidence(monkeypatch):
    app = create_api_app()
    server = app.state.server
    session = MagicMock()
    session.conversation_id = None
    session.new_conversation = AsyncMock()
    session.open = AsyncMock()
    session.select_model = AsyncMock()
    session.select_reasoning_effort = AsyncMock()
    session.drain_events = MagicMock(
        side_effect=[
            [
                StateChanged(
                    old_state="booting",
                    new_state="ready",
                    reason="composer_ready",
                    duration_ms=12.5,
                )
            ],
            [
                StateChanged(
                    old_state="generating",
                    new_state="ready",
                    reason="response_stable",
                    duration_ms=417.0,
                )
            ],
        ]
    )
    session.send = AsyncMock(
        return_value=TurnResult(
            turn_id="transition_turn",
            conversation_id="transition_conv",
            text="transition answer",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))
    request = parse_chat_completion_request(
        {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": "trace transitions"}],
        }
    )

    await server.complete_normalized(request)

    transitions = [
        event
        for event in server.trace.snapshot()
        if event.component == "session" and event.kind == "state_transition"
    ]
    assert [event.metadata["from"] for event in transitions] == ["booting", "generating"]
    assert [event.metadata["to"] for event in transitions] == ["ready", "ready"]
    assert transitions[0].metadata["evidence"] == "composer_ready"
    assert transitions[1].metadata["duration_ms"] == 417.0
