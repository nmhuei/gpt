"""Interact directly with the real Claude Code CLI binary in an interactive PTY session."""

import os
import pty
import select
import sys
import time


def run_interactive_claude_session():
    print("=" * 75)
    print("🖥️  SPAWNING REAL INTERACTIVE CLAUDE CODE CLI SESSION (PTY DRIVER)")
    print("=" * 75)

    claude_bin = "/home/light/.local/bin/claude"
    env = os.environ.copy()
    env.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:18000")
    env.setdefault("ANTHROPIC_API_KEY", "sk-webgpt-local")
    env.setdefault("CLAUDE_DEFAULT_MODEL", "claude-3-5-sonnet")
    env["TERM"] = "xterm-256color"
    env["COLUMNS"] = "120"
    env["LINES"] = "40"

    master, slave = pty.openpty()
    pid = os.fork()

    if pid == 0:
        os.close(master)
        os.setsid()
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        os.close(slave)
        os.execve(
            claude_bin,
            [claude_bin, "--dangerously-skip-permissions"],
            env,
        )
        sys.exit(1)

    os.close(slave)

    def read_until(expected_substrings, timeout=15.0):
        buffer = ""
        start = time.time()
        while time.time() - start < timeout:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(master, 1024).decode("utf-8", errors="replace")
                    buffer += chunk
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    for sub in expected_substrings:
                        if sub in buffer:
                            return buffer
                except OSError:
                    break
        return buffer

    def send_input(text):
        print(f"\n[USER TYPING]: {text}")
        os.write(master, (text + "\n").encode("utf-8"))

    try:
        print("[Waiting for Claude Code prompt...]")
        read_until([">", "Claude Code", "Welcome", "?"], timeout=10.0)

        # Turn 1: Conversational question
        send_input("toio laf ai")
        read_until([">", "completed", "error", "API Error"], timeout=10.0)

        # Turn 2: Follow-up question
        send_input("ban giup duoc gi cho toi")
        read_until([">", "completed", "error", "API Error"], timeout=10.0)

        # Turn 3: Ask to inspect project
        send_input("Read pyproject.toml and tell me the version")
        read_until([">", "completed", "version", "error"], timeout=15.0)

        # Turn 4: Exit session
        send_input("/exit")
        time.sleep(1)

        print("\n" + "=" * 75)
        print("🎉 INTERACTIVE CLAUDE CODE CLI SESSION TEST COMPLETE!")
        print("=" * 75 + "\n")

    finally:
        os.close(master)
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except Exception:
            pass


if __name__ == "__main__":
    run_interactive_claude_session()
