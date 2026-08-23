#!/usr/bin/env python3
import asyncio
import os
import sys
import subprocess
import time

TARGET_DIR = "/home/light/Workspace/CTF/CTF_Da_Nang_2026/Web/Web_Challenge_2"
CLAUDE_BIN = "/home/light/.local/bin/claude"

ENV = os.environ.copy()
ENV["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:18000"
ENV["ANTHROPIC_API_KEY"] = "sk-webgpt-local"
ENV["CLAUDE_DEFAULT_MODEL"] = "claude-3-5-sonnet"
ENV["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "200000"
ENV["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "8192"

PROMPT = (
    "Bạn hãy khảo sát thư mục bài thi CTF này, đọc kĩ metadata.json và README.md. "
    "Phân tích tiêu đề 'You Trusted the Technology, but Did You Check What Happened Recently?' "
    "và các gợi ý liên quan đến công nghệ gần đây (CVE/vulnerabilities gần đây liên quan đến Trust/Technology/Docker/Whale). "
    "Sau đó cập nhật file solve.py và README.md với phân tích và mã khai thác phù hợp."
)

print(f"[*] Chạy Claude Code CLI trong thư mục: {TARGET_DIR}")
print(f"[*] Prompt: {PROMPT}\n")

cmd = [
    CLAUDE_BIN,
    "-p", PROMPT,
    "--dangerously-skip-permissions",
    "--print"
]

proc = subprocess.Popen(
    cmd,
    cwd=TARGET_DIR,
    env=ENV,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1
)

for line in iter(proc.stdout.readline, ''):
    print(line, end='', flush=True)

proc.stdout.close()
return_code = proc.wait()
print(f"\n[*] Claude Code kết thúc với mã thoát: {return_code}")
sys.exit(return_code)
