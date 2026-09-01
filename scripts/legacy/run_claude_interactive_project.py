"""Autonomous Multi-Turn Project Driver for Claude Code CLI."""

import httpx


def run_claude_autonomous_project():
    print("=" * 70)
    print("🚀 LAUNCHING CLAUDE CODE CLI AUTONOMOUS PROJECT PIPELINE")
    print("   Project: 'threat_hunter' Threat Detection & Anomaly Engine")
    print("=" * 70)

    client = httpx.Client(base_url="http://127.0.0.1:18000", timeout=60.0)

    # Verify Gateway health
    health = client.get("/health").json()
    assert health["ok"] is True
    print(f"✅ Gateway online: {health}")

    session_messages = []
    tools_declaration = [
        {
            "name": "Write",
            "description": "Write a new file to the workspace",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
        {
            "name": "Read",
            "description": "Read a file from the workspace",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "Bash",
            "description": "Run a shell command",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
        {
            "name": "Agent",
            "description": "Spawn a subagent in parallel",
            "input_schema": {
                "type": "object",
                "properties": {"prompt": {"type": "string"}, "run_in_background": {"type": "boolean"}},
                "required": ["prompt"],
            },
        },
    ]

    def send_turn(content, expected_stop_reason=None):
        print(f"\n▶ SENDING TURN: {str(content)[:100]}...")
        if isinstance(content, str):
            session_messages.append({"role": "user", "content": content})
        else:
            session_messages.append({"role": "user", "content": content})

        payload = {
            "model": "claude-3-5-sonnet",
            "max_tokens": 4096,
            "messages": session_messages,
            "tools": tools_declaration,
        }
        resp = client.post("/v1/messages", json=payload)
        assert resp.status_code == 200, f"Turn failed ({resp.status_code}): {resp.text}"
        data = resp.json()
        stop_reason = data.get("stop_reason")
        print(f"  Status: 200 OK | Stop Reason: {stop_reason}")
        print(f"  Content: {str(data.get('content'))[:120]}...")
        if expected_stop_reason:
            assert stop_reason == expected_stop_reason, f"Expected {expected_stop_reason}, got {stop_reason}"

        # Append assistant turn
        session_messages.append({"role": "assistant", "content": data.get("content")})
        return data

    # Step 1: Initial user task instruction
    send_turn(
        "Build the complete threat_hunter security package. First, analyze requirements and outline the architecture.",
        expected_stop_reason="end_turn",
    )

    # Step 2: Request tool action for file creation
    res2 = send_turn(
        "Use the tool Write to create 'threat_hunter/types.py' with data models.",
        expected_stop_reason="tool_use",
    )
    call2 = res2["content"][0]

    # Step 3: Return tool result and request subagent fan-out
    send_turn(
        [
            {
                "type": "tool_result",
                "tool_use_id": call2["id"],
                "content": "File threat_hunter/types.py successfully written.",
            }
        ],
        expected_stop_reason="end_turn",
    )

    # Step 4: Request subagent fan-out
    res4 = send_turn(
        "Use the tool Agent to spawn 2 subagents: Subagent A for anomaly_detector.py and Subagent B for sigma_parser.py.",
        expected_stop_reason="tool_use",
    )
    call4 = res4["content"][0]

    # Step 5: Return subagent results
    send_turn(
        [
            {
                "type": "tool_result",
                "tool_use_id": call4["id"],
                "content": "Subagents completed: anomaly_detector.py and sigma_parser.py ready.",
            }
        ],
        expected_stop_reason="end_turn",
    )

    # Step 6: Request test execution via Bash tool
    res6 = send_turn(
        "Use the tool Bash to run pytest tests/test_pcap_analysis_pipeline.py and verify quality gates.",
        expected_stop_reason="tool_use",
    )
    call6 = res6["content"][0]

    # Step 7: Return bash test output and finish
    send_turn(
        [
            {
                "type": "tool_result",
                "tool_use_id": call6["id"],
                "content": "============================= 323 passed in 6.45s ==============================",
            }
        ],
        expected_stop_reason="end_turn",
    )

    print("\n" + "=" * 70)
    print("🎉 CLAUDE CODE CLI AUTONOMOUS PROJECT PIPELINE COMPLETED SUCCESSFULLY!")
    print("   All 7 Multi-Turn Autonomous Tool & Subagent Steps PASSED 100%!")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    run_claude_autonomous_project()
