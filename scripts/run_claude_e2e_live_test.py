"""Automated E2E Test Suite for Claude Code CLI against ChatGPT Web Gateway."""

import asyncio
import os
import sys
import time


class ClaudeE2ETestRunner:
    def __init__(self, port: int = 18000):
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.claude_bin = "/home/light/.local/bin/claude"
        self.env = os.environ.copy()
        self.env["ANTHROPIC_BASE_URL"] = self.base_url
        self.env["ANTHROPIC_API_KEY"] = "sk-webgpt-local"
        self.env["CLAUDE_DEFAULT_MODEL"] = "claude-3-5-sonnet"
        self.results = []

    async def run_claude_command(self, prompt: str, description: str, timeout_s: float = 120.0) -> dict:
        print("\n-------------------------------------------------------------")
        print(f"▶ EXECUTING: {description}")
        print(f"  Prompt: {prompt[:80]}...")
        start_time = time.monotonic()

        cmd = [
            self.claude_bin,
            "-p",
            prompt,
            "--dangerously-skip-permissions",
            "--print",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=self.env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            duration = time.monotonic() - start_time
            out_str = stdout.decode(errors="replace").strip()
            err_str = stderr.decode(errors="replace").strip()

            success = proc.returncode == 0
            res = {
                "phase": description,
                "success": success,
                "returncode": proc.returncode,
                "duration_s": round(duration, 2),
                "stdout": out_str,
                "stderr": err_str,
            }
            status = "✅ PASS" if success else f"❌ FAIL (exit {proc.returncode})"
            print(f"  Result: {status} in {duration:.2f}s")
            if out_str:
                print(f"  Output: {out_str[:120]}...")
            if err_str and not success:
                print(f"  Error: {err_str[:120]}...")
            self.results.append(res)
            return res
        except TimeoutError:
            duration = time.monotonic() - start_time
            print(f"  Result: ❌ TIMEOUT after {timeout_s}s")
            res = {
                "phase": description,
                "success": False,
                "returncode": -1,
                "duration_s": round(duration, 2),
                "stdout": "",
                "stderr": f"Execution timed out after {timeout_s}s",
            }
            self.results.append(res)
            return res

    async def run_all_phases(self):
        print("\n" + "=" * 70)
        print("🚀 CLAUDE CODE CLI FULL-LIFECYCLE LIVE TEST SUITE")
        print(f"Target Gateway: {self.base_url}")
        print("=" * 70)

        # Phase 1: Basic Handshake
        await self.run_claude_command(
            prompt="Reply with exactly: CLAUDE_CODE_GATEWAY_ONLINE",
            description="Phase 1: Basic Handshake & Connectivity",
        )

        # Phase 2: Single Tool Call (Read)
        await self.run_claude_command(
            prompt="Use the Read tool to read pyproject.toml and report the name and version fields.",
            description="Phase 2: Single Tool Invocations (Read pyproject.toml)",
        )

        # Phase 3: Shell Execution (Bash)
        await self.run_claude_command(
            prompt="Use the Bash tool to run 'git status --short' and report modified files.",
            description="Phase 3: Command Execution (Bash git status)",
        )

        # Phase 4: Subagent Fan-Out Spawning (Agent Tool)
        await self.run_claude_command(
            prompt=(
                "Spawn 2 subagents in parallel using the Agent tool:\n"
                "1. Subagent A: Run sha256sum on README.md\n"
                "2. Subagent B: Run wc -l on README.md\n"
                "Gather both results and summarize them."
            ),
            description="Phase 4: Multi-Subagent Fan-Out & Parallel Execution",
        )

        # Phase 5: Implementation & Testing (Pcap Analyzer)
        await self.run_claude_command(
            prompt=(
                "Inspect the pcap_analyzer package using Read or Bash, "
                "then run 'pytest tests/test_pcap_analysis_pipeline.py' via Bash and report test results."
            ),
            description="Phase 5: Code Inspection & Test Execution via Tools",
        )

        # Summary
        print("\n" + "=" * 70)
        print("📊 FINAL ACCEPTANCE TEST SUMMARY")
        print("=" * 70)
        total = len(self.results)
        passed = sum(1 for r in self.results if r["success"])
        for r in self.results:
            icon = "✅" if r["success"] else "❌"
            print(f"  {icon} {r['phase']:<50} | {r['duration_s']:>6.2f}s | exit {r['returncode']}")
        print("-" * 70)
        print(f"Overall Result: {passed}/{total} Phases Passed ({passed/total*100:.1f}%)")
        print("=" * 70 + "\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18000
    runner = ClaudeE2ETestRunner(port=port)
    asyncio.run(runner.run_all_phases())
