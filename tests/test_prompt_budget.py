from unittest.mock import AsyncMock, MagicMock

import pytest

from gpt.api.server import create_api_app
from gpt.promptcompat import compact_messages
from gpt.requests import parse_chat_completion_request
from gpt.types import TurnResult


def test_compaction_retains_objective_latest_turn_and_tool_pair():
    messages = [
        {"role": "system", "content": "system contract"},
        {"role": "user", "content": "original objective"},
        {"role": "assistant", "content": "old " * 300},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_keep",
                    "type": "function",
                    "function": {"name": "Read", "arguments": '{"file_path":"SPEC.md"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_keep", "content": "authoritative result"},
        {"role": "assistant", "content": "noise " * 300},
        {"role": "user", "content": "current objective"},
    ]

    compacted = compact_messages(messages, max_content_chars=800)

    assert messages[0] in compacted
    assert messages[1] in compacted
    assert messages[3] in compacted
    assert messages[4] in compacted
    assert messages[-1] in compacted
    assert messages[2] not in compacted


@pytest.mark.anyio
async def test_runtime_compacts_before_browser_send(monkeypatch):
    monkeypatch.setenv("WEBGPT_MAX_PROMPT_CHARS", "4000")
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
            turn_id="turn_budget",
            conversation_id="conv_budget",
            text="budget ok",
        )
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=session))

    messages = [{"role": "user", "content": "ORIGINAL_OBJECTIVE"}]
    for index in range(10):
        messages.append({"role": "assistant", "content": f"OLD_{index}_" + ("x" * 650)})
    messages.append({"role": "user", "content": "CURRENT_OBJECTIVE"})
    request = parse_chat_completion_request(
        {"model": "chatgpt-web", "messages": messages}
    )

    response, _record = await server.complete_normalized(request)

    assert response["choices"][0]["message"]["content"] == "budget ok"
    sent_prompt = session.send.await_args.args[0]
    assert len(sent_prompt) <= 4000
    assert "ORIGINAL_OBJECTIVE" in sent_prompt
    assert "CURRENT_OBJECTIVE" in sent_prompt
    assert any(
        event.component == "promptcompat" and event.kind == "prompt_compacted"
        for event in server.trace.snapshot()
    )
