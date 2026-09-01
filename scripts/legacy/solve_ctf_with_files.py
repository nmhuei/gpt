#!/usr/bin/env python3
"""Đóng gói toàn bộ file trong thư mục bài CTF rồi gửi kèm prompt cho Claude Code CLI.

Thư mục bài thi lấy từ --target / $WEBGPT_CTF_TARGET_DIR; mặc định là workspace
vứt được dưới ~/Downloads/ctf-workspaces/web-challenge-2 (dữ liệu lớn, xoá được
bất cứ lúc nào — không bao giờ trỏ vào thư viện ~/Workspace/CTF gốc). Script đọc
mọi file thường trong thư mục (text hoặc base64), nhúng toàn bộ nội dung vào
prompt yêu cầu phân tích challenge và viết lại solve.py, sau đó khởi động
`claude -p` với prompt đó. Kết quả lưu vào scratch/ctf_solve_output.md trong repo.

CẢNH BÁO: khi chạy thật, script bắn một live turn (kèm toàn bộ file đính kèm) vào
gateway local (ANTHROPIC_BASE_URL mặc định http://127.0.0.1:18000) và cho Claude
toàn quyền thao tác trong thư mục bài thi (--dangerously-skip-permissions).
"""
import argparse
import base64
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
DEFAULT_TARGET_DIR = Path.home() / "Downloads" / "ctf-workspaces" / "web-challenge-2"
CLAUDE_BIN = (
    os.environ.get(CLAUDE_BIN_ENV)
    or shutil.which("claude")
    or str(Path.home() / ".local" / "bin" / "claude")
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Đóng gói mọi file trong thư mục bài CTF ($WEBGPT_CTF_TARGET_DIR hoặc "
            "--target), nhúng vào prompt và gửi cho Claude Code CLI để phân tích "
            "challenge, viết solve.py và cập nhật README.md; kết quả lưu vào "
            "scratch/ctf_solve_output.md."
        ),
        epilog=(
            "CẢNH BÁO: khi chạy thật, script bắn một live turn (kèm toàn bộ file đính kèm) "
            "vào gateway local (ANTHROPIC_BASE_URL mặc định http://127.0.0.1:18000) và chạy "
            "claude với --dangerously-skip-permissions ngay trong thư mục bài thi."
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

    # 1. Read and pack all files in target directory (as text or base64)
    files_data = {}
    for file_path in target_dir.iterdir():
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

    print(f"[*] Đã đóng gói {len(files_data)} file từ {target_dir}:")
    for fname, fdata in files_data.items():
        print(f"    - {fname} ({fdata['type']}, size={len(fdata['b64'])} b64 chars)")

    # 2. Build rich prompt with file contents embedded
    file_attachments_block = "\n\n".join(
        f"=== FILE: {fname} ===\n{fdata.get('content', fdata['b64'])}\n=== END FILE: {fname} ==="
        for fname, fdata in files_data.items()
    )

    prompt = frame_local_ctf_prompt(
        f"Dưới đây là nội dung các file hiện có trong workspace bài tập local:\n\n"
        f"{file_attachments_block}\n\n"
        "Nhiệm vụ:\n"
        "1. Phân tích metadata.json và README.md để xác định định dạng/hành vi cần hiểu.\n"
        "2. Đối chiếu các gợi ý về thay đổi công nghệ gần đây và implementation flaws liên quan nếu chúng thực sự xuất hiện trong tài liệu local.\n"
        "3. Viết hoặc cập nhật solve.py thành một reproduction/solve procedure hoàn chỉnh, chạy và xác minh local trước.\n"
        "4. Cập nhật README.md với writeup, bằng chứng kiểm thử và giới hạn còn lại. "
        "Không dùng remote để thay cho việc chứng minh local."
    )

    ENV = os.environ.copy()
    ENV.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:18000")
    ENV.setdefault("ANTHROPIC_API_KEY", "sk-webgpt-local")
    ENV.setdefault("CLAUDE_DEFAULT_MODEL", "claude-3-5-sonnet")
    ENV.setdefault("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "200000")
    ENV.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "8192")

    print("\n[*] Gửi prompt kèm toàn bộ file đính kèm tới Claude Code CLI...")

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
    print(f"\n[*] Claude Code kết thúc với mã: {return_code}")

    full_output = "".join(output_lines)
    # Save response to log (repo scratch/ — gitignored)
    log_path = REPO_ROOT / "scratch" / "ctf_solve_output.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(full_output, encoding="utf-8")
    print(f"[*] Đã lưu kết quả phân tích vào: {log_path}")

    return return_code


if __name__ == "__main__":
    sys.exit(main())
