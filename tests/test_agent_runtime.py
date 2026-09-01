from __future__ import annotations

import json

import httpx

from gpt.agent.runner import AgentRunner, AgentRunnerConfig


def _response(payload: dict, session_id: str = "wgs_agent") -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        headers={"x-webgpt-session-id": session_id, "request-id": "req_agent"},
    )


def test_agent_runner_exposes_only_shell_and_patch(tmp_path):
    seen_tools = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_tools
        body = json.loads(request.content)
        seen_tools = [tool["name"] for tool in body["tools"]]
        return _response(
            {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with AgentRunner(
        workspace=tmp_path,
        config=AgentRunnerConfig(base_url="http://gateway.test", verify="off"),
        http_client=client,
    ) as agent:
        result = agent.run("hi")
    assert result.success
    assert seen_tools == ["Bash", "ApplyPatch"]


def test_verification_gate_forces_another_round_after_patch(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if calls == 1:
            return _response(
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "p1",
                            "name": "ApplyPatch",
                            "input": {
                                "patch": "--- a.txt\n+++ a.txt\n@@ -1 +1 @@\n-old\n+new\n"
                            },
                        }
                    ],
                    "stop_reason": "tool_use",
                }
            )
        if calls == 2:
            return _response(
                {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}
            )
        if calls == 3:
            assert "verification gate" in json.dumps(body["messages"][-1]).lower()
            return _response(
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "v1",
                            "name": "Bash",
                            "input": {"command": "cat a.txt"},
                        }
                    ],
                    "stop_reason": "tool_use",
                }
            )
        return _response(
            {"content": [{"type": "text", "text": "verified"}], "stop_reason": "end_turn"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with AgentRunner(
        workspace=tmp_path,
        config=AgentRunnerConfig(
            base_url="http://gateway.test", verify="auto", max_rounds=6
        ),
        http_client=client,
    ) as agent:
        result = agent.run("edit")
    assert result.success
    assert result.text == "verified"
    assert result.verification_gate_count == 1
    assert target.read_text() == "new\n"
