#!/usr/bin/env python3
import base64
import os
import subprocess
import sys
from pathlib import Path

TARGET_DIR = Path("/home/light/Workspace/CTF/CTF_Da_Nang_2026/Web/Web_Challenge_2")
CLAUDE_BIN = "/home/light/.local/bin/claude"

# 1. Read and pack all files in target directory (as text or base64)
files_data = {}
for file_path in TARGET_DIR.iterdir():
    if file_path.is_file() and not file_path.name.startswith("."):
        try:
            content = file_path.read_text(encoding="utf-8")
            files_data[file_path.name] = {
                "type": "text",
                "content": content,
                "b64": base64.b64encode(content.encode("utf-8")).decode("ascii")
            }
        except Exception:
            raw = file_path.read_bytes()
            files_data[file_path.name] = {
                "type": "binary",
                "b64": base64.b64encode(raw).decode("ascii")
            }

print(f"[*] Đã đóng gói {len(files_data)} file từ {TARGET_DIR}:")
for fname, fdata in files_data.items():
    print(f"    - {fname} ({fdata['type']}, size={len(fdata['b64'])} b64 chars)")

# 2. Build rich prompt with file contents embedded
file_attachments_block = "\n\n".join(
    f"=== FILE: {fname} ===\n{fdata.get('content', fdata['b64'])}\n=== END FILE: {fname} ==="
    for fname, fdata in files_data.items()
)

prompt = (
    f"Dưới đây là toàn bộ nội dung các file hiện có trong thư mục bài thi CTF:\n\n"
    f"{file_attachments_block}\n\n"
    f"Nhiệm vụ của bạn:\n"
    f"1. Phân tích chi tiết bài thi Web Challenge 2 từ metadata.json và README.md.\n"
    f"2. Phân tích tiêu đề 'You Trusted the Technology, but Did You Check What Happened Recently?' (gợi ý về CVE/lỗ hổng gần đây liên quan đến Docker, ctfd-whale, container escaping, hoặc technology trust).\n"
    f"3. Đưa ra phân tích kỹ thuật đầy đủ và viết lại file solve.py hoàn chỉnh để giải quyết challenge.\n"
    f"4. Cập nhật README.md với writeup phân tích chi tiết."
)

ENV = os.environ.copy()
ENV["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:18000"
ENV["ANTHROPIC_API_KEY"] = "sk-webgpt-local"
ENV["CLAUDE_DEFAULT_MODEL"] = "claude-3-5-sonnet"
ENV["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "200000"
ENV["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "8192"

print(f"\n[*] Gửi prompt kèm toàn bộ file đính kèm tới Claude Code CLI...")

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
print(f"\n[*] Claude Code kết thúc với mã: {return_code}")

full_output = "".join(output_lines)
# Save response to log
log_path = Path("/home/light/GitHub/gpt/scratch/ctf_solve_output.md")
log_path.parent.mkdir(parents=True, exist_ok=True)
log_path.write_text(full_output, encoding="utf-8")
print(f"[*] Đã lưu kết quả phân tích vào: {log_path}")

sys.exit(return_code)
