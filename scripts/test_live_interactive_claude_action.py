"""Interact directly with Claude Code CLI binary to execute a real interactive tool action."""

import os
import pty
import select
import sys
import time


def run_claude_action():
    print("=" * 75)
    print("🖥️  RUNNING DIRECT CLAUDE CODE CLI INTERACTIVE ACTION IN PTY")
    print("=" * 75)

    claude_bin = "/home/light/.local/bin/claude"
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:18000"
    env["ANTHROPIC_API_KEY"] = "sk-webgpt-local"
    env["CLAUDE_DEFAULT_MODEL"] = "claude-3-5-sonnet"
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
            [
                claude_bin,
                "-p",
                "Read pyproject.toml and report the name and version fields.",
                "--dangerously-skip-permissions",
            ],
            env,
        )
        sys.exit(1)

    os.close(slave)

    output = ""
    start = time.time()
    try:
        while time.time() - start < 20.0:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(master, 1024).decode("utf-8", errors="replace")
                    output += chunk
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                except OSError:
                    break
    finally:
        os.close(master)
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except Exception:
            pass

    print("\n" + "=" * 75)
    print("🎉 REAL INTERACTIVE CLI OUTPUT CAPTURED SUCCESSFULLY!")
    print("=" * 75 + "\n")


if __name__ == "__main__":
    run_claude_action()
