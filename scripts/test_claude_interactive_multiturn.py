"""Interactive Multi-turn Session Test for Claude Code CLI."""

import httpx


def test_interactive_session():
    print("=" * 70)
    print("🚀 TESTING CLAUDE CODE CLI IN FULL INTERACTIVE MULTI-TURN MODE")
    print("=" * 70)

    client = httpx.Client(base_url="http://127.0.0.1:18000", timeout=30.0)

    # Check health
    health = client.get("/health").json()
    assert health["ok"] is True
    print("✅ Gateway health confirmed:", health)

    # Turn 1: Conversational message (like 'toio laf ai')
    print("\n--- Turn 1: Sending conversational prompt 'toio laf ai' ---")
    messages = [{"role": "user", "content": "toio laf ai"}]
    payload = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "messages": messages,
        "tools": [
            {
                "name": "Read",
                "description": "Read a file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
    }
    r1 = client.post("/v1/messages", json=payload)
    print(f"Status: {r1.status_code}")
    assert r1.status_code == 200, f"Turn 1 failed: {r1.text}"
    b1 = r1.json()
    print(f"Response: {b1['content']}")
    assert b1["stop_reason"] == "end_turn"
    assistant_text = b1["content"][0]["text"]
    messages.append({"role": "assistant", "content": assistant_text})

    # Turn 2: Follow-up conversational message
    print("\n--- Turn 2: Sending follow-up 'ban co the giup gi cho toi' ---")
    messages.append({"role": "user", "content": "ban co the giup gi cho toi"})
    payload["messages"] = messages
    r2 = client.post("/v1/messages", json=payload)
    print(f"Status: {r2.status_code}")
    assert r2.status_code == 200, f"Turn 2 failed: {r2.text}"
    b2 = r2.json()
    print(f"Response: {b2['content']}")
    assert b2["stop_reason"] == "end_turn"
    messages.append({"role": "assistant", "content": b2["content"][0]["text"]})

    # Turn 3: Tool use prompt
    print("\n--- Turn 3: Sending tool-invoking prompt 'Read pyproject.toml' ---")
    messages.append({"role": "user", "content": "Read pyproject.toml"})
    payload["messages"] = messages
    r3 = client.post("/v1/messages", json=payload)
    print(f"Status: {r3.status_code}")
    assert r3.status_code == 200, f"Turn 3 failed: {r3.text}"
    b3 = r3.json()
    print(f"Response: {b3['content']}")
    assert b3["stop_reason"] == "tool_use"
    tool_call = b3["content"][0]
    tool_use_id = tool_call["id"]
    messages.append({"role": "assistant", "content": [tool_call]})

    # Turn 4: Tool result return
    print("\n--- Turn 4: Returning tool_result for call ---")
    messages.append({
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "[project]\nname = 'gpt'\nversion = '0.1.0'",
            }
        ],
    })
    payload["messages"] = messages
    r4 = client.post("/v1/messages", json=payload)
    print(f"Status: {r4.status_code}")
    assert r4.status_code == 200, f"Turn 4 failed: {r4.text}"
    b4 = r4.json()
    print(f"Response: {b4['content']}")
    assert b4["stop_reason"] == "end_turn"

    print("\n" + "=" * 70)
    print("🎉 ALL 4 MULTI-TURN INTERACTIVE STEPS PASSED WITH 100% SUCCESS!")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    test_interactive_session()
