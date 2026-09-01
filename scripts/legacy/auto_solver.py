#!/usr/bin/env python3
"""
Autonomous CTF Solver & Self-Healing Feedback Loop (AutoSolver)

Mechanics:
1. Iterative Execution Loop: Generates/updates solve.py.
2. Automated Verification: Runs `python3 solve.py <target_url>` after each turn.
3. Regex Flag Detection: Scans stdout/stderr for standard CTF flag patterns.
4. Autonomous Reflection / Self-Correction: If execution fails or no flag is found,
   captures stdout, stderr, and exit codes, feeding them back to the LLM in the next turn.
5. Auto-Writeup: Once the flag is verified, automatically generates writeup/WRITEUP.md.
"""

import argparse
import base64
import os
import re
import subprocess
import sys
from pathlib import Path

FLAG_REGEX = re.compile(r"(?:flag|ctf|danangctf|dragons|vuln)\{[^}]+\}", re.IGNORECASE)
CLAUDE_BIN = "/home/light/.local/bin/claude"

def collect_directory_context(target_dir: Path) -> str:
    """Collects text and binary files from challenge directory into a structured prompt."""
    files_data = []
    for file_path in target_dir.iterdir():
        if file_path.is_file() and not file_path.name.startswith("."):
            if file_path.name in {"flag.txt"}:
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                # Truncate very large raw html if inside json to save tokens
                if len(content) > 30000:
                    content = content[:30000] + "\n...[truncated]..."
                files_data.append(f"=== FILE: {file_path.name} ===\n{content}\n=== END FILE ===")
            except Exception:
                raw = file_path.read_bytes()
                if len(raw) < 50000:
                    b64 = base64.b64encode(raw).decode("ascii")
                    files_data.append(f"=== BINARY FILE (BASE64): {file_path.name} ===\n{b64}\n=== END FILE ===")
    return "\n\n".join(files_data)


def run_solve_script(target_dir: Path, target_url: str | None, timeout: int = 30) -> tuple[int, str, str, str | None]:
    """Executes solve.py and checks for flag."""
    solve_path = target_dir / "solve.py"
    if not solve_path.exists():
        return 1, "", "File solve.py does not exist yet.", None

    cmd = [sys.executable, str(solve_path)]
    if target_url:
        cmd.append(target_url)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=timeout
        )
        combined = f"{proc.stdout}\n{proc.stderr}"
        match = FLAG_REGEX.search(combined)
        flag = match.group(0) if match else None
        return proc.returncode, proc.stdout, proc.stderr, flag
    except subprocess.TimeoutExpired:
        return 124, "", f"Execution timed out after {timeout} seconds.", None
    except Exception as e:
        return 1, "", str(e), None


def send_to_claude(prompt: str, target_dir: Path) -> tuple[int, str]:
    """Sends prompt to Claude Code CLI and captures stream output."""
    env = os.environ.copy()
    env.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:18000")
    env.setdefault("ANTHROPIC_API_KEY", "sk-webgpt-local")
    env.setdefault("CLAUDE_DEFAULT_MODEL", "claude-3-5-sonnet")
    env.setdefault("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "200000")
    env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "8192")

    cmd = [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions", "--print"]
    proc = subprocess.Popen(
        cmd,
        cwd=str(target_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    output_lines = []
    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ""):
        print(line, end="", flush=True)
        output_lines.append(line)
    proc.stdout.close()
    return proc.wait(), "".join(output_lines)


def auto_solve_challenge(target_dir: Path, target_url: str | None = None, max_iterations: int = 5):
    print("=" * 70)
    print("🤖 BẮT ĐẦU VÒNG LẶP TỰ ĐỘNG HÓA CTF (AUTONOMOUS SOLVER LOOP)")
    print(f"📁 Thư mục bài thi : {target_dir}")
    print(f"🔗 Target URL       : {target_url or 'N/A (Offline/Local)'}")
    print(f"🔄 Số lượt thử tối đa : {max_iterations}")
    print("=" * 70)

    # Pre-check: approve workspace trust in ~/.claude.json
    try:
        import json
        cpath = Path("/home/light/.claude.json")
        if cpath.exists():
            cdata = json.loads(cpath.read_text())
            cdata.setdefault("projects", {})[str(target_dir)] = {
                "hasTrustDialogAccepted": True,
                "allowedTools": ["Read", "Write", "Edit", "Bash", "Agent", "Glob", "Grep", "LS"]
            }
            cpath.write_text(json.dumps(cdata, indent=2))
    except Exception as e:
        print(f"[!] Warning updating trust: {e}")

    # Gather initial directory files
    dir_context = collect_directory_context(target_dir)

    initial_prompt = (
        f"Bạn là chuyên gia CTF tự động. Dưới đây là toàn bộ thông tin và file trong thư mục bài thi:\n\n"
        f"{dir_context}\n\n"
        f"Target URL: {target_url or 'Chạy local'}\n\n"
        f"Nhiệm vụ:\n"
        f"1. Phân tích chi tiết đề bài và các file đính kèm.\n"
        f"2. Xác định chính xác lỗ hổng bảo mật và bề mặt tấn công.\n"
        f"3. Viết mã khai thác Python hoàn chỉnh vào file `{target_dir}/solve.py` "
        f"(sử dụng requests với verify=False, timeout, xử lý response và in Flag rõ ràng định dạng flag{{...}}).\n"
        f"4. Hãy viết trực tiếp nội dung hoàn chỉnh của file solve.py."
    )

    current_prompt = initial_prompt
    flag_found = None

    for turn in range(1, max_iterations + 1):
        print("\n" + "-" * 70)
        print(f"▶️ [VÒNG {turn}/{max_iterations}] Đang gửi yêu cầu và phân tích với AI...")
        print("-" * 70)

        _code, ai_response = send_to_claude(current_prompt, target_dir)

        # Check if AI output already contains a flag (e.g. reverse / offline challenge)
        direct_match = FLAG_REGEX.search(ai_response)
        if direct_match and not target_url:
            flag_found = direct_match.group(0)
            print(f"\n🎉 [!] TÌM THẤY FLAG TRỰC TIẾP TRONG OUTPUT: {flag_found}")
            break

        # Check if solve.py was created/updated, or extract code block from ai_response if needed
        solve_file = target_dir / "solve.py"
        if not solve_file.exists() or turn > 1:
            # Extract python code block if present
            py_blocks = re.findall(r"```python(.*?)```", ai_response, re.DOTALL)
            if py_blocks:
                solve_code = py_blocks[-1].strip()
                solve_file.write_text(solve_code, encoding="utf-8")
                solve_file.chmod(0o755)
                print(f"[+] Đã tự động trích xuất và lưu mã vào `{solve_file}`")

        # Step 2: Run solve.py and verify execution
        print("\n[*] Đang chạy thực nghiệm `python3 solve.py` để kiểm tra kết quả...")
        retcode, stdout, stderr, flag = run_solve_script(target_dir, target_url, timeout=35)
        print(f"[*] Mã thoát: {retcode}")
        if stdout:
            print(f"[*] Stdout:\n{stdout[:500]}")
        if stderr:
            print(f"[!] Stderr:\n{stderr[:500]}")

        if flag:
            flag_found = flag
            print(f"\n🎉🎉🎉 [!] CHÚC MỪNG: ĐÃ BẮT ĐƯỢC FLAG: {flag_found}")
            break

        # Step 3: Self-healing feedback loop
        print(f"\n⚠️ Chưa thu được flag ở vòng {turn}. Tự động kích hoạt phản hồi tự sửa lỗi (Self-Healing Loop)...")
        current_prompt = (
            f"Ở lượt chạy vừa rồi, script `solve.py` đã được thực thi nhưng CHƯA LẤY ĐƯỢC FLAG.\n\n"
            f"Kết quả thực thi thực tế:\n"
            f"- Mã thoát (Return Code): {retcode}\n"
            f"- Output ghi nhận (Stdout):\n```\n{stdout[-1500:] if stdout else '[Trống]'}\n```\n"
            f"- Lỗi ghi nhận (Stderr):\n```\n{stderr[-1500:] if stderr else '[Trống]'}\n```\n\n"
            f"Hãy thực hiện tự chẩn đoán (Self-Diagnosis):\n"
            f"1. Phân tích nguyên nhân vì sao chưa lấy được flag hoặc vì sao xảy ra lỗi ở trên.\n"
            f"2. Đề xuất phương án sửa lỗi hoặc đổi hướng tiếp cận (payload khác, endpoint khác, phương thức giải mã khác).\n"
            f"3. Cung cấp lại mã nguồn hoàn chỉnh của `{target_dir}/solve.py` đã được sửa lỗi trong block ```python ... ```."
        )

    # Final Step: Save Flag & Generate Writeup
    if flag_found:
        (target_dir / "flag.txt").write_text(flag_found, encoding="utf-8")
        print(f"\n[+] Đã lưu cờ vào `{target_dir}/flag.txt`")

        print("\n[*] Đang tự động tạo Writeup tổng kết...")
        writeup_prompt = (
            f"Thử thách đã được giải thành công!\n"
            f"Flag thu được: {flag_found}\n\n"
            f"Hãy viết một file Writeup hoàn chỉnh theo cấu trúc chuẩn: "
            f"Tổng quan, Phân tích lỗ hổng, Các bước khai thác chi tiết, Script solve.py và Flag. "
            f"Lưu vào file `{target_dir}/writeup/WRITEUP.md`."
        )
        send_to_claude(writeup_prompt, target_dir)
        print("\n✅ QUY TRÌNH HOÀN TẤT THÀNH CÔNG!")
    else:
        print(f"\n❌ Đã thử {max_iterations} vòng nhưng chưa thu được flag. Hãy kiểm tra lại log chi tiết ở trên.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AutoSolver - Autonomous CTF Solver & Self-Correction Feedback Loop")
    parser.add_argument("target_dir", help="Đường dẫn tới thư mục bài thi CTF")
    parser.add_argument("target_url", nargs="?", default=None, help="URL instance container (nếu có)")
    parser.add_argument("--max-retries", "-m", type=int, default=5, help="Số lần tự sửa lỗi tối đa (mặc định: 5)")
    args = parser.parse_args()

    target_dir = Path(args.target_dir).resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        print(f"[!] Lỗi: Thư mục `{target_dir}` không tồn tại.")
        sys.exit(1)

    auto_solve_challenge(target_dir, args.target_url, max_iterations=args.max_retries)
