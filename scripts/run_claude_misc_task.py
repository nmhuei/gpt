#!/usr/bin/env python3
import os
import sys
import subprocess
from pathlib import Path

TARGET_DIR = Path("/home/light/Workspace/CTF/CTF_Da_Nang_2026/Misc/Misc_Challenge_3")
CLAUDE_BIN = "/home/light/.local/bin/claude"

# Read challenge files
readme_content = (TARGET_DIR / "README.md").read_text(encoding="utf-8") if (TARGET_DIR / "README.md").exists() else ""

prompt = (
    f"Bạn đang ở trong thư mục bài thi CTF: {TARGET_DIR}.\n\n"
    f"Nội dung file README.md:\n```markdown\n{readme_content}\n```\n\n"
    f"Yêu cầu:\n"
    f"1. Phân tích bài toán 'Indirect Prompt Injection' (CourseBot, PDF upload, Admin Bot review uploads tự động).\n"
    f"2. Thiết kế kịch bản Indirect Prompt Injection: tạo file PDF chứa prompt injection payload để khi Admin Bot đọc tài liệu này thì bị thao túng và tiết lộ sensitive data / flag vào đoạn chat hoặc phản hồi qua API.\n"
    f"3. Viết script giải hoàn chỉnh bằng Python vào {TARGET_DIR}/solve.py (sử dụng fpdf2 hoặc reportlab để tạo file PDF chứa payload, upload qua POST /api/upload, sau đó kiểm tra /api/chat hoặc /api/query để lấy flag).\n"
    f"4. Cập nhật README.md với writeup chi tiết."
)

ENV = os.environ.copy()
ENV["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:18000"
ENV["ANTHROPIC_API_KEY"] = "sk-webgpt-local"
ENV["CLAUDE_DEFAULT_MODEL"] = "claude-3-5-sonnet"
ENV["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "200000"
ENV["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "8192"

print(f"[*] Khởi chạy Claude Code CLI trong: {TARGET_DIR}")
print(f"[*] Prompt gửi tới Claude Code:\n{prompt}\n")

cmd = [
    CLAUDE_BIN,
    "-p", prompt,
    "--dangerously-skip-permissions",
    "--print"
]

proc = subprocess.Popen(
    cmd,
    cwd=str(TARGET_DIR),
    env=ENV,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1
)

output_lines = []
for line in iter(proc.stdout.readline, ''):
    print(line, end='', flush=True)
    output_lines.append(line)

proc.stdout.close()
return_code = proc.wait()
print(f"\n[*] Claude Code kết thúc với exit code: {return_code}")

# Save full output to artifact/scratch
full_output = "".join(output_lines)
log_path = Path("/home/light/GitHub/gpt/scratch/misc_challenge_3_output.md")
log_path.parent.mkdir(parents=True, exist_ok=True)
log_path.write_text(full_output, encoding="utf-8")
print(f"[*] Báo cáo chi tiết đã lưu tại: {log_path}")

sys.exit(return_code)
