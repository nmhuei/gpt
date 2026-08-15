from unittest.mock import AsyncMock

from openai import OpenAI
from starlette.testclient import TestClient

from gpt.api.server import create_api_app
from gpt.types import TurnResult

STEP_TOOL = {
    "type": "function",
    "function": {
        "name": "next_step",
        "description": "Return the result for one deterministic step",
        "parameters": {
            "type": "object",
            "properties": {"step": {"type": "integer"}},
            "required": ["step"],
        },
    },
}


class FakeWebSession:
    """Deterministic browser substitute; gateway mapping remains real."""

    def __init__(self, steps: int = 10):
        self.steps = steps
        self.send_count = 0
        self.conversation_id = None
        self.new_conversation = AsyncMock(side_effect=self._new)
        self.open = AsyncMock(side_effect=self._open)
        self.select_model = AsyncMock()

    async def _new(self):
        self.conversation_id = None

    async def _open(self, conversation_id):
        self.conversation_id = conversation_id

    async def send(self, prompt):
        self.send_count += 1
        self.conversation_id = "web-conversation-1"
        if self.send_count <= self.steps:
            text = (
                "<WEBGPT_TOOL_CALL>\n"
                f'{{"name":"next_step","arguments":{{"step":{self.send_count}}}}}\n'
                "</WEBGPT_TOOL_CALL>"
            )
        else:
            text = f"Completed {self.steps} correlated tool steps."
        return TurnResult(
            turn_id=f"turn-{self.send_count}",
            conversation_id=self.conversation_id,
            text=text,
        )

    def drain_events(self):
        return []


def test_standard_openai_client_completes_ten_step_tool_loop(monkeypatch):
    app = create_api_app()
    server = app.state.server
    fake = FakeWebSession(steps=10)
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))
    messages = [{"role": "user", "content": "Complete ten steps."}]

    with TestClient(app) as http_client:
        client = OpenAI(
            base_url="http://testserver/v1",
            api_key="local",
            http_client=http_client,
        )
        first = client.chat.completions.create(
            model="chatgpt-web", messages=messages, tools=[STEP_TOOL]
        )
        assert first.choices[0].finish_reason == "tool_calls"
        response = first

        for expected_step in range(1, 11):
            call = response.choices[0].message.tool_calls[0]
            assert call.function.name == "next_step"
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [call.model_dump()],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": f"step {expected_step} verified",
                }
            )
            response = client.chat.completions.create(
                model="chatgpt-web", messages=messages, tools=[STEP_TOOL]
            )

        assert response.choices[0].finish_reason == "stop"
        assert response.choices[0].message.content == "Completed 10 correlated tool steps."
        assert fake.send_count == 11
        assert len(server.conversations) == 1


def test_wrong_tool_call_id_is_rejected(monkeypatch):
    app = create_api_app()
    server = app.state.server
    fake = FakeWebSession(steps=1)
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))
    payload = {
        "model": "chatgpt-web",
        "messages": [{"role": "user", "content": "use a tool"}],
        "tools": [STEP_TOOL],
    }
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", json=payload)
        assistant = first.json()["choices"][0]["message"]
        payload["messages"].extend(
            [
                assistant,
                {"role": "tool", "tool_call_id": "call_wrong", "content": "fake"},
            ]
        )
        response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request_error"
