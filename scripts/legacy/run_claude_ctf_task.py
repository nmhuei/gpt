#!/usr/bin/env python3
"""Chạy Claude Code CLI trên một bài CTF Web để phân tích challenge.

Thư mục bài thi lấy từ --target / $WEBGPT_CTF_TARGET_DIR; mặc định là workspace
vứt được dưới ~/Downloads/ctf-workspaces/web-challenge-2 (không trỏ vào thư viện
~/Workspace/CTF gốc). Script khởi động `claude -p` trong thư mục bài thi với
prompt phân tích metadata.json/README.md, sau đó yêu cầu Claude cập nhật solve.py
và README.md kèm mã khai thác.

CẢNH BÁO: khi chạy thật, script bắn một live turn vào gateway local
(ANTHROPIC_BASE_URL mặc định http://127.0.0.1:18000) và cho Claude toàn quyền
thao tác trong thư mục bài thi (--dangerously-skip-permissions).
"""
import argparse
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

frame_local_ctf_prompt = importlib.import_module("scripts.legacy.ctf_prompting").frame_local_ctf_prompt


# Env-first: WEBGPT_CTF_TARGET_DIR > --target > default scratch workspace.
CTF_TARGET_ENV = "WEBGPT_CTF_TARGET_DIR"
CLAUDE_BIN_ENV = "WEBGPT_CLAUDE_BIN"
DEFAULT_TARGET_DIR = str(Path.home() / "Downloads" / "ctf-workspaces" / "web-challenge-2")
CLAUDE_BIN = (
    os.environ.get(CLAUDE_BIN_ENV)
    or shutil.which("claude")
    or str(Path.home() / ".local" / "bin" / "claude")
)

PROMPT = frame_local_ctf_prompt(
    "Khảo sát workspace bài tập này, đọc kỹ metadata.json và README.md. "
    "Phân tích tiêu đề 'You Trusted the Technology, but Did You Check What Happened Recently?' "
    "và các gợi ý liên quan đến thay đổi công nghệ gần đây (CVE/implementation flaws liên quan đến Trust/Technology/Docker/Whale). "
    "Sau đó cập nhật solve.py và README.md với phân tích, reproduction/solve procedure và bằng chứng xác minh local. "
    "Dùng thuật ngữ kỹ thuật trung tính, mô tả đúng thao tác thực tế và ưu tiên local-first."
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Chạy Claude Code CLI trong thư mục bài CTF Web ($WEBGPT_CTF_TARGET_DIR "
            "hoặc --target) để phân tích challenge và cập nhật solve.py/README.md."
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

    target_dir = args.target or DEFAULT_TARGET_DIR
    if not Path(target_dir).is_dir():
        print(
            f"FATAL: thư mục bài CTF không tồn tại: {target_dir}\n"
            f"(đặt {CTF_TARGET_ENV} hoặc truyền --target)",
            file=sys.stderr,
        )
        return 2

    ENV = os.environ.copy()
    ENV.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:18000")
    ENV.setdefault("ANTHROPIC_API_KEY", "sk-webgpt-local")
    ENV.setdefault("CLAUDE_DEFAULT_MODEL", "claude-3-5-sonnet")
    ENV.setdefault("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "200000")
    ENV.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "8192")

    print(f"[*] Chạy Claude Code CLI trong thư mục: {target_dir}")
    print(f"[*] Prompt: {PROMPT}\n")

    cmd = [
        CLAUDE_BIN,
        "-p", PROMPT,
        "--dangerously-skip-permissions",
        "--print"
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=target_dir,
        env=ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ''):
        print(line, end='', flush=True)

    proc.stdout.close()
    return_code = proc.wait()
    print(f"\n[*] Claude Code kết thúc với mã thoát: {return_code}")
    return return_code


if __name__ == "__main__":
    sys.exit(main())
