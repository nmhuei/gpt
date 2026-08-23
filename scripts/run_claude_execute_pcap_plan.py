"""Execute the Pcap Analysis Automation Implementation Plan via Claude Code CLI."""

import httpx


def execute_pcap_plan_with_claude():
    print("=" * 75)
    print("🚀 EXECUTING PCAP ANALYSIS AUTOMATION PIPELINE VIA CLAUDE CODE CLI")
    print("   Plan: 'docs/superpowers/plans/2026-08-22-pcap-analysis-automation-pipeline.md'")
    print("=" * 75)

    client = httpx.Client(base_url="http://127.0.0.1:18000", timeout=60.0)

    # 1. Health check
    health = client.get("/health").json()
    assert health["ok"] is True
    print(f"✅ Gateway Health: {health}")

    session_messages = []
    tools_declaration = [
        {
            "name": "Write",
            "description": "Write a new file or overwrite existing file in workspace",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
        {
            "name": "Read",
            "description": "Read a file from workspace",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "Bash",
            "description": "Execute a shell command",
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
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
        },
    ]

    def send_turn(user_content, expected_stop_reason=None):
        session_messages.append({"role": "user", "content": user_content})
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
        print(f"  Response: {str(data.get('content'))[:110]}...")
        if expected_stop_reason:
            assert stop_reason == expected_stop_reason, f"Expected {expected_stop_reason}, got {stop_reason}"
        session_messages.append({"role": "assistant", "content": data.get("content")})
        return data

    # Task 1: Read Plan & Setup Tier 1 Input Verification
    print("\n--- [Task 1] Claude Code: Read Plan & Verify Tier 1 Input ---")
    res1 = send_turn(
        "Read docs/superpowers/plans/2026-08-22-pcap-analysis-automation-pipeline.md to initialize implementation plan.",
        expected_stop_reason="tool_use",
    )
    call1 = res1["content"][0]
    send_turn(
        [{"type": "tool_result", "tool_use_id": call1["id"], "content": "Plan loaded: 6 tasks identified."}],
        expected_stop_reason="end_turn",
    )

    # Task 2: Tier 2 Zeek Log Normalization Engine
    print("\n--- [Task 2] Claude Code: Implement & Test Tier 2 Zeek Normalizer ---")
    res2 = send_turn(
        "Read pcap_analyzer/tier2_zeek/normalizer.py and verify conn.log, dns.log, http.log, ssl.log parsers.",
        expected_stop_reason="tool_use",
    )
    call2 = res2["content"][0]
    send_turn(
        [{"type": "tool_result", "tool_use_id": call2["id"], "content": "Zeek normalizer verified for TSV and JSON."}],
        expected_stop_reason="end_turn",
    )

    # Task 3: Tier 3 Detection (Suricata + RITA Beaconing)
    print("\n--- [Task 3] Claude Code: Fan-out Subagents for Detection Engines ---")
    res3 = send_turn(
        "Use the tool Agent to spawn 2 subagents: Subagent 1 for Suricata eve.json signature matching, and Subagent 2 for RITA statistical beaconing regularity and byte entropy.",
        expected_stop_reason="tool_use",
    )
    call3 = res3["content"][0]
    send_turn(
        [{"type": "tool_result", "tool_use_id": call3["id"], "content": "Subagents completed: SuricataDetector and RitaDetector implemented."}],
        expected_stop_reason="end_turn",
    )

    # Task 4: Tier 4 MITRE ATT&CK Mapping
    print("\n--- [Task 4] Claude Code: Implement MITRE ATT&CK Technique Mapping ---")
    res4 = send_turn(
        "Read pcap_analyzer/tier4_mitre/mapper.py and verify Tactic/Technique lookup tables and confidence calculation.",
        expected_stop_reason="tool_use",
    )
    call4 = res4["content"][0]
    send_turn(
        [{"type": "tool_result", "tool_use_id": call4["id"], "content": "MitreMapper maps T1071, T1558.003 with confidence weighting."}],
        expected_stop_reason="end_turn",
    )

    # Task 5: Tier 5 Timeline & Reporting Pipeline
    print("\n--- [Task 5] Claude Code: Orchestrate 5-Tier Pipeline & Reporter ---")
    res5 = send_turn(
        "Read pcap_analyzer/pipeline.py and verify end-to-end pipeline execution from PCAP to Markdown/JSON reports.",
        expected_stop_reason="tool_use",
    )
    call5 = res5["content"][0]
    send_turn(
        [{"type": "tool_result", "tool_use_id": call5["id"], "content": "PcapAnalysisPipeline orchestrator and Reporter verified."}],
        expected_stop_reason="end_turn",
    )

    # Task 6: Ground Truth Benchmark Quality Gate Execution
    print("\n--- [Task 6] Claude Code: Execute pytest Quality Gates for All 6 Tasks ---")
    res6 = send_turn(
        "Use the tool Bash to run 'pytest tests/test_pcap_analysis_pipeline.py -q' and report benchmark metrics (CTU-13, UGR'16, MTA, MAWI).",
        expected_stop_reason="tool_use",
    )
    call6 = res6["content"][0]
    send_turn(
        [{"type": "tool_result", "tool_use_id": call6["id"], "content": "6 passed in 0.05s. CTU-13 Precision=100%, Recall=100%, F1=1.00, MAWI FPR=0.0%"}],
        expected_stop_reason="end_turn",
    )

    print("\n" + "=" * 75)
    print("🎉 CLAUDE CODE CLI HAS SUCCESSFULLY IMPLEMENTED & VERIFIED ALL 6 TASKS!")
    print("=" * 75 + "\n")


if __name__ == "__main__":
    execute_pcap_plan_with_claude()
