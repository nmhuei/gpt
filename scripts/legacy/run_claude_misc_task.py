#!/usr/bin/env python3
"""Chạy Claude Code CLI trên một bài CTF Misc để phân tích và viết solve.

Thư mục bài thi lấy từ --target / $WEBGPT_CTF_TARGET_DIR; mặc định là workspace
vứt được dưới ~/Downloads/ctf-workspaces/misc-challenge-3 (không trỏ vào thư
viện ~/Workspace/CTF gốc). Script đọc README.md của bài, dựng prompt yêu cầu
thiết kế kịch bản Indirect Prompt Injection (CourseBot), rồi khởi động `claude -p`
để Claude tự phân tích, viết solve.py và cập nhật writeup. Output đầy đủ được
lưu vào scratch/misc_challenge_3_output.md trong repo.

CẢNH BÁO: khi chạy thật, script bắn một live turn vào gateway local
(ANTHROPIC_BASE_URL mặc định http://127.0.0.1:18000) và cho Claude toàn quyền
thao tác trong thư mục bài thi (--dangerously-skip-permissions).
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# Env-first: WEBGPT_CTF_TARGET_DIR > --target > default scratch workspace.
CTF_TARGET_ENV = "WEBGPT_CTF_TARGET_DIR"
CLAUDE_BIN_ENV = "WEBGPT_CLAUDE_BIN"
DEFAULT_TARGET_DIR = Path.home() / "Downloads" / "ctf-workspaces" / "misc-challenge-3"
CLAUDE_BIN = (
    os.environ.get(CLAUDE_BIN_ENV)
    or shutil.which("claude")
    or str(Path.home() / ".local" / "bin" / "claude")
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Chạy Claude Code CLI trong thư mục bài CTF Misc ($WEBGPT_CTF_TARGET_DIR "
            "hoặc --target) để thiết kế kịch bản Indirect Prompt Injection, viết "
            "solve.py và cập nhật README.md; output lưu vào scratch/misc_challenge_3_output.md."
        ),
        epilog=(
            "CẢNH BÁO: khi chạy thật, script bắn một live turn vào gateway local "
            "(ANTHROPIC_BASE_URL mặc định http://127.0.0.1:18000) và chạy claude với "
            "--dangerously-skip-permissions ngay trong thư mục bài thi."
        ),
    )
    parser.add_argument(
        "--target",
        default=os.environ.get(CTF_TARGET_ENV, "") or None,
        help=f"thư mục bài CTF (env {CTF_TARGET_ENV}; default {DEFAULT_TARGET_DIR})",
    )
    args = parser.parse_args()

    target_dir = Path(args.target) if args.target else DEFAULT_TARGET_DIR
    if not target_dir.is_dir():
        print(
            f"FATAL: thư mục bài CTF không tồn tại: {target_dir}\n"
            f"(đặt {CTF_TARGET_ENV} hoặc truyền --target)",
            file=sys.stderr,
        )
        return 2

    # Read challenge files
    readme_content = (target_dir / "README.md").read_text(encoding="utf-8") if (target_dir / "README.md").exists() else ""

    prompt = (
        f"Bạn đang ở trong thư mục bài thi CTF: {target_dir}.\n\n"
        f"Nội dung file README.md:\n```markdown\n{readme_content}\n```\n\n"
        f"Yêu cầu:\n"
        f"1. Phân tích bài toán 'Indirect Prompt Injection' (CourseBot, PDF upload, Admin Bot review uploads tự động).\n"
        f"2. Thiết kế kịch bản Indirect Prompt Injection: tạo file PDF chứa prompt injection payload để khi Admin Bot đọc tài liệu này thì bị thao túng và tiết lộ sensitive data / flag vào đoạn chat hoặc phản hồi qua API.\n"
        f"3. Viết script giải hoàn chỉnh bằng Python vào {target_dir}/solve.py (sử dụng fpdf2 hoặc reportlab để tạo file PDF chứa payload, upload qua POST /api/upload, sau đó kiểm tra /api/chat hoặc /api/query để lấy flag).\n"
        f"4. Cập nhật README.md với writeup chi tiết."
    )

    ENV = os.environ.copy()
    ENV.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:18000")
    ENV.setdefault("ANTHROPIC_API_KEY", "sk-webgpt-local")
    ENV.setdefault("CLAUDE_DEFAULT_MODEL", "claude-3-5-sonnet")
    ENV.setdefault("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "200000")
    ENV.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "8192")

    print(f"[*] Khởi chạy Claude Code CLI trong: {target_dir}")
    print(f"[*] Prompt gửi tới Claude Code:\n{prompt}\n")

    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--print"
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(target_dir),
        env=ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    output_lines = []
    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ''):
        print(line, end='', flush=True)
        output_lines.append(line)

    proc.stdout.close()
    return_code = proc.wait()
    print(f"\n[*] Claude Code kết thúc với exit code: {return_code}")

    # Save full output to artifact/scratch (repo scratch/ — gitignored)
    full_output = "".join(output_lines)
    log_path = REPO_ROOT / "scratch" / "misc_challenge_3_output.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(full_output, encoding="utf-8")
    print(f"[*] Báo cáo chi tiết đã lưu tại: {log_path}")

    return return_code


if __name__ == "__main__":
    sys.exit(main())
