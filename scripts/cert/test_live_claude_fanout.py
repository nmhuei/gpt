"""Live Multi-Agent Fan-out Verification with Real Claude Code CLI."""

import asyncio
import os
import time


async def run_single_claude_agent(agent_id: int, prompt: str, port: int = 18000) -> dict:
    start_time = time.monotonic()
    env = os.environ.copy()
    env.setdefault("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{port}")
    env.setdefault("ANTHROPIC_API_KEY", "sk-webgpt-local")
    env.setdefault("CLAUDE_DEFAULT_MODEL", "claude-3-5-sonnet")
    
    cmd = [
        "/home/light/.local/bin/claude",
        "-p",
        prompt,
        "--dangerously-skip-permissions",
        "--print",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await proc.communicate()
    duration = time.monotonic() - start_time

    return {
        "agent_id": agent_id,
        "returncode": proc.returncode,
        "duration_s": round(duration, 2),
        "stdout": stdout.decode(errors="replace").strip(),
        "stderr": stderr.decode(errors="replace").strip(),
    }


async def test_fanout_tier(num_agents: int, port: int = 18000):
    print("\n=======================================================")
    print(f"🚀 RUNNING LIVE FAN-OUT TEST: {num_agents} REAL CLAUDE CODE CLI AGENTS")
    print("=======================================================")
    
    prompts = [
        f"Agent-{i}: Calculate 1234 * {i+1} and return only the number."
        for i in range(num_agents)
    ]
    
    start_total = time.monotonic()
    tasks = [run_single_claude_agent(i + 1, prompts[i], port) for i in range(num_agents)]
    results = await asyncio.gather(*tasks)
    total_time = time.monotonic() - start_total

    print(f"\nResults for {num_agents} Parallel Claude Agents (Total time: {total_time:.2f}s):")
    all_success = True
    for r in results:
        status = "✅ PASS" if r["returncode"] == 0 else f"❌ FAIL ({r['returncode']})"
        if r["returncode"] != 0:
            all_success = False
        print(f"  - Agent #{r['agent_id']:02d}: {status} in {r['duration_s']}s | Output: {r['stdout'][:60]}")
        if r["stderr"] and r["returncode"] != 0:
            print(f"    Error: {r['stderr'][:100]}")

    print(f"Summary: {'ALL SUCCEEDED' if all_success else 'SOME FAILED'} | Avg Time/Agent: {total_time/num_agents:.2f}s\n")
    return all_success


if __name__ == "__main__":
    import sys
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 18000
    asyncio.run(test_fanout_tier(count, port))
