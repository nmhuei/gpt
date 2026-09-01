from __future__ import annotations

import json

import httpx

from gpt.direct_agent import DirectAgent, DirectAgentConfig


def _response(payload: dict, *, session_id: str = "wgs_direct_test") -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        headers={
            "x-webgpt-session-id": session_id,
            "request-id": "req_direct_test",
        },
    )


def test_direct_agent_executes_tool_and_preserves_gateway_session(tmp_path):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        if len(calls) == 1:
            assert request.headers.get("x-webgpt-session-id") is None
            assert body["messages"] == [{"role": "user", "content": "do it"}]
            return _response(
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "Bash",
                            "input": {"command": "printf DIRECT_AGENT_OK"},
                        }
                    ],
                    "stop_reason": "tool_use",
                }
            )

        assert request.headers["x-webgpt-session-id"] == "wgs_direct_test"
        tool_result = body["messages"][-1]["content"][0]
        assert tool_result["type"] == "tool_result"
        assert tool_result["tool_use_id"] == "call_1"
        assert tool_result["is_error"] is False
        assert "DIRECT_AGENT_OK" in tool_result["content"]
        return _response(
            {
                "content": [{"type": "text", "text": "verified"}],
                "stop_reason": "end_turn",
            }
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = DirectAgentConfig(base_url="http://gateway.test", max_rounds=4)
    with DirectAgent(workspace=tmp_path, config=config, client=client) as agent:
        result = agent.run("do it")

    assert result.success
    assert result.text == "verified"
    assert result.tool_calls == 1
    assert result.rounds == 2
    assert result.session_id == "wgs_direct_test"
    assert len(calls) == 2


def test_direct_agent_reports_failed_bash_as_tool_error_but_continues(tmp_path):
    seen_error = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_error
        body = json.loads(request.content)
        if len(body["messages"]) == 1:
            return _response(
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_bad",
                            "name": "Bash",
                            "input": {"command": "exit 7"},
                        }
                    ],
                    "stop_reason": "tool_use",
                }
            )

        result = body["messages"][-1]["content"][0]
        seen_error = result["is_error"] is True and "exit_code=7" in result["content"]
        return _response(
            {
                "content": [{"type": "text", "text": "handled"}],
                "stop_reason": "end_turn",
            }
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with DirectAgent(
        workspace=tmp_path,
        config=DirectAgentConfig(base_url="http://gateway.test"),
        client=client,
    ) as agent:
        result = agent.run("test failure handling")

    assert result.success
    assert result.text == "handled"
    assert seen_error


def test_direct_agent_stops_at_round_cap(tmp_path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return _response(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_loop",
                        "name": "Bash",
                        "input": {"command": "true"},
                    }
                ],
                "stop_reason": "tool_use",
            }
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with DirectAgent(
        workspace=tmp_path,
        config=DirectAgentConfig(base_url="http://gateway.test", max_rounds=2),
        client=client,
    ) as agent:
        result = agent.run("loop")

    assert not result.success
    assert result.rounds == 2
    assert result.tool_calls == 2
    assert "Maximum tool rounds reached" in (result.error or "")
