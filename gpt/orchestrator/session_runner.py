from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from gpt.orchestrator.types import ChallengeStatus, ChallengeTask, SolvingStrategy

FLAG_REGEX = re.compile(r"(?:flag|ctf|danangctf|dragons|brunner|vuln)\{[^}]+\}", re.IGNORECASE)
CLAUDE_BIN = "/home/light/.local/bin/claude"


class ClaudeCodeSessionRunner:
    """Manages an isolated Claude Code CLI session lifecycle for a specific challenge."""

    def __init__(self, task: ChallengeTask, logger: Callable[[str], None] | None = None):
        self.task = task
        self.logger = logger or (lambda msg: print(f"[{task.name}] {msg}"))

    def _log(self, message: str) -> None:
        self.task.log_messages.append(message)
        self.logger(message)

    def _ensure_trusted_workspace(self) -> None:
        try:
            cpath = Path("/home/light/.claude.json")
            if cpath.exists():
                cdata = json.loads(cpath.read_text())
                cdata.setdefault("projects", {})[str(self.task.directory)] = {
                    "hasTrustDialogAccepted": True,
                    "allowedTools": ["Read", "Write", "Edit", "Bash", "Agent", "Glob", "Grep", "LS"],
                }
                cpath.write_text(json.dumps(cdata, indent=2))
        except Exception as e:
            self._log(f"Warning updating workspace trust: {e}")

    def _collect_files(self) -> str:
        files_data = []
        
        def _process_file(file_path: Path):
            if not file_path.is_file() or file_path.name.startswith("."):
                return
            if file_path.name in {"flag.txt", "NEEDS_HUMAN_REVIEW.md"}:
                return
            if file_path.name == "metadata.json":
                try:
                    m = json.loads(file_path.read_text(encoding="utf-8"))
                    clean_meta = {
                        "name": m.get("name"),
                        "category": m.get("category"),
                        "points": m.get("value") or m.get("points"),
                        "description": m.get("description"),
                        "connection_info": m.get("connection_info") or m.get("instance_url"),
                        "hints": m.get("hints", []),
                    }
                    if not self.task.target_url and clean_meta["connection_info"]:
                        self.task.target_url = clean_meta["connection_info"]
                    files_data.append(f"=== METADATA (CLEAN) ===\n{json.dumps(clean_meta, indent=2, ensure_ascii=False)}\n=== END METADATA ===")
                    return
                except Exception:
                    pass
            if file_path.suffix.lower() in {".zip", ".gz", ".tar", ".bin", ".elf", ".exe", ".pcap", ".png", ".jpg", ".pyc"}:
                try:
                    raw = file_path.read_bytes()
                    if len(raw) < 50000:
                        b64 = base64.b64encode(raw).decode("ascii")
                        files_data.append(f"=== BINARY FILE (BASE64): {file_path.name} ===\n{b64}\n=== END FILE ===")
                except Exception:
                    pass
                return
            try:
                raw = file_path.read_bytes()
                if b"\x00" in raw:
                    if len(raw) < 50000:
                        b64 = base64.b64encode(raw).decode("ascii")
                        files_data.append(f"=== BINARY FILE (BASE64): {file_path.name} ===\n{b64}\n=== END FILE ===")
                else:
                    content = raw.decode("utf-8", errors="replace").replace("\x00", "")
                    if len(content) > 15000:
                        content = content[:15000] + "\n...[truncated]..."
                    rel_name = file_path.relative_to(self.task.directory)
                    files_data.append(f"=== FILE: {rel_name} ===\n{content}\n=== END FILE ===")
            except Exception:
                pass

        for p in sorted(self.task.directory.rglob("*")):
            if p.is_file():
                _process_file(p)

        return "\n\n".join(files_data).replace("\x00", "")

    def reboot_session(self) -> None:
        """Cleans up any cached Claude Code session files for this workspace to eliminate poisoned context."""
        self._log("🔄 Khởi động lại session sạch (Rebooting session context)...")
        try:
            claude_projects_dir = Path("/home/light/.claude/projects")
            if claude_projects_dir.exists():
                for pdir in claude_projects_dir.iterdir():
                    if str(self.task.directory).replace("/", "-") in pdir.name:
                        shutil.rmtree(pdir, ignore_errors=True)
        except Exception as e:
            self._log(f"Warning cleaning session cache: {e}")

    async def run_claude_turn(self, prompt: str) -> tuple[int, str]:
        env = os.environ.copy()
        env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:18000"
        env["ANTHROPIC_API_KEY"] = "sk-webgpt-local"
        env["CLAUDE_DEFAULT_MODEL"] = "claude-3-5-sonnet"
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "200000"
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "8192"

        safe_prompt = prompt.replace("\x00", "")
        cmd = [CLAUDE_BIN, "-p", safe_prompt, "--dangerously-skip-permissions", "--print"]

        def _exec():
            proc = subprocess.run(
                cmd,
                cwd=str(self.task.directory),
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return proc.returncode, proc.stdout + "\n" + proc.stderr

        return await asyncio.to_thread(_exec)

    async def execute_solve_script(self, timeout: int = 30) -> tuple[int, str, str, str | None]:
        solve_path = self.task.directory / "solve.py"
        if not solve_path.exists():
            return 1, "", "File solve.py chưa tồn tại.", None

        cmd = [sys.executable, str(solve_path)]
        if self.task.target_url:
            cmd.append(self.task.target_url)

        def _exec():
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(self.task.directory),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                combined = f"{proc.stdout}\n{proc.stderr}"
                match = FLAG_REGEX.search(combined)
                flag = match.group(0) if match else None
                return proc.returncode, proc.stdout, proc.stderr, flag
            except subprocess.TimeoutExpired:
                return 124, "", f"Hết thời gian thực thi ({timeout}s).", None
            except Exception as e:
                return 1, "", str(e), None

        return await asyncio.to_thread(_exec)

    async def ensure_instance_live(self) -> bool:
        """If target_url is provided, checks if it is responsive.
        If it returns 502/503 or connection errors, loops and waits gracefully for the user
        to renew/boot the container without exiting!
        """
        if not self.task.target_url or not self.task.target_url.startswith("http"):
            return True

        import ssl
        import urllib.error
        import urllib.request

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        waited = 0
        while True:
            try:
                req = urllib.request.Request(
                    self.task.target_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                with urllib.request.urlopen(req, context=ctx, timeout=4) as resp:
                    if resp.status < 500:
                        if waited > 0:
                            self._log(f"✅ [Instance Online] Container đã hoạt động trở lại (HTTP {resp.status})! Tiếp tục giải...")
                        return True
            except urllib.error.HTTPError as e:
                if e.code in (502, 503, 504):
                    if waited % 30 == 0:
                        self._log(f"⏳ [Instance Offline: HTTP {e.code}] Đang chờ bạn boot/renew container trên web CTFd... (Tự động thăm dò lại mỗi 5s, không exit)")
                else:
                    if waited > 0:
                        self._log(f"✅ [Instance Online] Container đã hoạt động trở lại (HTTP {e.code})! Tiếp tục giải...")
                    return True
            except Exception as e:
                if waited % 30 == 0:
                    self._log(f"⏳ [Instance Offline: {e}] Đang chờ bạn boot/renew container trên web CTFd... (Tự động thăm dò lại mỗi 5s, không exit)")

            await asyncio.sleep(5)
            waited += 5

    async def solve_challenge(self) -> ChallengeTask:
        self.task.start_time = time.monotonic()
        self.task.status = ChallengeStatus.DISPATCHED
        self._ensure_trusted_workspace()

        # Wait for instance to be live if URL is configured
        await self.ensure_instance_live()

        dir_context = self._collect_files()

        # Build initial prompt
        current_prompt = (
            f"Bạn là chuyên gia an toàn thông tin tự động giải quyết bài thi CTF `{self.task.name}` "
            f"(Category: {self.task.category}, Points: {self.task.points}).\n\n"
            f"Dưới đây là các file và dữ liệu hiện có trong thư mục bài thi:\n\n"
            f"{dir_context}\n\n"
            f"Target URL: {self.task.target_url or 'Offline / Local challenge'}\n\n"
            f"Nhiệm vụ:\n"
            f"1. Phân tích chi tiết bề mặt tấn công và cơ chế bài toán.\n"
            f"2. Viết mã khai thác Python hoàn chỉnh vào file `{self.task.directory}/solve.py` "
            f"(sử dụng requests với verify=False, timeout, xử lý response và in Flag rõ ràng định dạng flag{{...}}).\n"
            f"3. Cung cấp nội dung code hoàn chỉnh trong khối ```python ... ```."
        )

        for attempt in range(1, self.task.max_attempts + 1):
            self.task.attempt = attempt
            self.task.status = ChallengeStatus.ANALYZING

            # Rotate strategies
            if attempt == 1:
                self.task.current_strategy = SolvingStrategy.STANDARD_TRIAGE
            elif attempt == 2:
                self.task.current_strategy = SolvingStrategy.ERROR_FEEDBACK
            elif attempt == 3:
                self.task.current_strategy = SolvingStrategy.ALTERNATIVE_VECTOR
            elif attempt == 4:
                self.task.current_strategy = SolvingStrategy.CLEAN_REBOOT
                self.reboot_session()
            else:
                self.task.current_strategy = SolvingStrategy.EXTREME_RESONING

            self._log(f"▶️ [Vòng {attempt}/{self.task.max_attempts}] Chiến thuật: {self.task.current_strategy.value}")

            # Send prompt to Claude Code
            code, ai_output = await self.run_claude_turn(current_prompt)

            # Check if Flag is directly in AI output
            direct_flag = FLAG_REGEX.search(ai_output)
            if direct_flag and not self.task.target_url:
                self.task.flag = direct_flag.group(0)
                self.task.status = ChallengeStatus.SOLVED
                self._log(f"🎉 TÌM THẤY FLAG TRỰC TIẾP: {self.task.flag}")
                break

            # Extract python code block if present and write to solve.py
            solve_file = self.task.directory / "solve.py"
            py_blocks = re.findall(r"```python(.*?)```", ai_output, re.DOTALL)
            if py_blocks:
                solve_code = py_blocks[-1].strip()
                solve_file.write_text(solve_code, encoding="utf-8")
                solve_file.chmod(0o755)

            # Execute solve.py
            self.task.status = ChallengeStatus.TESTING
            retcode, stdout, stderr, flag = await self.execute_solve_script(timeout=35)

            if flag:
                self.task.flag = flag
                self.task.status = ChallengeStatus.SOLVED
                self._log(f"🎉🎉🎉 XÁC THỰC FLAG THÀNH CÔNG: {self.task.flag}")
                break

            # Record failure in error history
            err_summary = f"[Attempt {attempt}] RetCode: {retcode} | Stdout: {stdout[:200]} | Stderr: {stderr[:200]}"
            self.task.error_history.append(err_summary)
            self._log(f"⚠️ Chưa có Flag (Mã thoát: {retcode}). Chuẩn bị phản hồi lỗi...")

            # Prepare next turn prompt based on strategy
            if self.task.current_strategy == SolvingStrategy.ALTERNATIVE_VECTOR:
                hint = "Hãy đổi hoàn toàn hướng tiếp cận: thử payload khác, endpoint khác, phương pháp giải mã khác, hoặc kiểm tra boundary conditions."
            elif self.task.current_strategy == SolvingStrategy.CLEAN_REBOOT:
                hint = "Session vừa được reset sạch. Hãy tiếp cận lại bài toán từ đầu với góc nhìn mới và kiểm tra lại toàn bộ giả định ban đầu."
            else:
                hint = "Hãy phân tích log lỗi thực tế dưới đây, giải thích nguyên nhân và sửa lại logic trong solve.py."

            current_prompt = (
                f"Ở lượt vừa rồi, script `solve.py` đã chạy nhưng CHƯA BẮT ĐƯỢC FLAG.\n\n"
                f"Gợi ý chiến thuật: {hint}\n\n"
                f"Kết quả thực thi thực tế:\n"
                f"- Return Code: {retcode}\n"
                f"- Stdout:\n```\n{stdout[-1200:] if stdout else '[Trống]'}\n```\n"
                f"- Stderr:\n```\n{stderr[-1200:] if stderr else '[Trống]'}\n```\n\n"
                f"Hãy cung cấp lại toàn bộ nội dung file `{self.task.directory}/solve.py` đã được cải tiến trong ```python ... ```."
            )

        self.task.end_time = time.monotonic()

        # Post-Processing
        if self.task.status == ChallengeStatus.SOLVED and self.task.flag:
            (self.task.directory / "flag.txt").write_text(self.task.flag, encoding="utf-8")
            self._log("📝 Đang tự động sinh file writeup/WRITEUP.md...")
            writeup_prompt = (
                f"Thử thách đã giải thành công! Flag: {self.task.flag}\n"
                f"Hãy viết file writeup/WRITEUP.md chuyên nghiệp lưu vào `{self.task.directory}/writeup/WRITEUP.md`."
            )
            await self.run_claude_turn(writeup_prompt)
        else:
            # ESCALATION: Generate Diagnostic Report for User
            self.task.status = ChallengeStatus.ESCALATED
            self._log("🚨 ĐÃ ĐẠT GIỚI HẠN THỬ — Chuyển sang hàng đợi cảnh báo người dùng (Escalated to User)!")
            report = (
                f"# 🚨 BÁO CÁO CẢNH BÁO: CẦN NGƯỜI DÙNG HỖ TRỢ (HUMAN REVIEW NEEDED)\n\n"
                f"- **Bài thi:** `{self.task.name}` (Category: {self.task.category}, Points: {self.task.points})\n"
                f"- **Thư mục:** `{self.task.directory}`\n"
                f"- **Số lượt thử đã chạy:** {self.task.attempt}/{self.task.max_attempts}\n"
                f"- **Target URL:** {self.task.target_url or 'N/A'}\n\n"
                f"## 📋 Lịch Sử Lỗi Đã Gặp Qua Các Vòng:\n"
                + "\n".join(f"- {e}" for e in self.task.error_history)
                + f"\n\n## 💡 Đề Xuất Hướng Xử Lý Cho Kỹ Sư:\n"
                f"1. Kiểm tra xem instance container có đang hoạt động bình thường không.\n"
                f"2. Xem file `{self.task.directory}/solve.py` hiện tại để rà soát logic gửi payload.\n"
                f"3. Dùng lệnh `gpt` thủ công để tương tác chuyên sâu với Claude Code trong thư mục bài thi."
            )
            self.task.diagnostic_report = report
            (self.task.directory / "NEEDS_HUMAN_REVIEW.md").write_text(report, encoding="utf-8")

        return self.task
