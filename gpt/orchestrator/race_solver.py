from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

from gpt.orchestrator.session_runner import ClaudeCodeSessionRunner
from gpt.orchestrator.types import ChallengeStatus, ChallengeTask, SolvingStrategy


RACE_STRATEGIES = [
    (
        "Worker-1: Deep Logic & Side-Channel Specialist",
        "Tập trung sâu vào phân tích logic xử lý, side-channel (timing attack, error difference), toán học hoặc thuật toán mã hoá."
    ),
    (
        "Worker-2: Fuzzing & Parameter Probing Specialist",
        "Tập trung vào fuzzing tham số, thử các payload đặc biệt, boundary testing, format string hoặc ký tự biên."
    ),
    (
        "Worker-3: Protocol & Auth Bypass Specialist",
        "Tập trung vào kiểm tra header, cookie, JWT/token structure, lỗ hổng bypass xác thực hoặc injection trong trường xác thực."
    ),
    (
        "Worker-4: Rapid Fast-Path Prototyper",
        "Tập trung viết PoC thực thi nhanh, dò tìm trực tiếp các endpoint ẩn, debug/admin APIs hoặc error stack trace."
    ),
]


class SwarmRaceSolver:
    """Competitive Multi-Agent Swarm Solver:

    Runs N concurrent worker agents with diverse specialized attack angles on
    the SAME challenge. The first worker to capture and verify the flag wins,
    instantly halting the rest.
    """

    def __init__(
        self,
        challenge_dir: Path,
        target_url: str | None = None,
        num_workers: int = 4,
        max_attempts_per_worker: int = 3,
        logger: Callable[[str], None] | None = None,
    ):
        self.challenge_dir = challenge_dir.resolve()
        self.target_url = target_url
        self.num_workers = min(num_workers, len(RACE_STRATEGIES))
        self.max_attempts = max_attempts_per_worker
        self.logger = logger or print
        self.winner: ChallengeTask | None = None
        self.stop_event = asyncio.Event()

    async def _worker_runner(self, worker_idx: int, worker_name: str, worker_angle: str) -> ChallengeTask | None:
        # Create isolated workspace directory for this worker
        scratch_dir = self.challenge_dir / ".swarm_scratch" / f"worker_{worker_idx}"
        shutil.rmtree(scratch_dir, ignore_errors=True)
        scratch_dir.mkdir(parents=True, exist_ok=True)

        # Copy challenge files to worker workspace
        for p in self.challenge_dir.iterdir():
            if p.name.startswith(".") or p.name in {"flag.txt", "NEEDS_HUMAN_REVIEW.md"}:
                continue
            if p.is_file():
                shutil.copy2(p, scratch_dir / p.name)
            elif p.is_dir():
                shutil.copytree(p, scratch_dir / p.name, dirs_exist_ok=True)

        task = ChallengeTask(
            directory=scratch_dir,
            name=f"{self.challenge_dir.name} [{worker_name}]",
            target_url=self.target_url,
            max_attempts=self.max_attempts,
        )

        runner = ClaudeCodeSessionRunner(
            task,
            logger=lambda msg: self.logger(f"[{worker_name}] {msg}"),
        )

        self.logger(f"🏁 [{worker_name}] Bắt đầu xuất phát! Góc tiếp cận: {worker_angle}")

        # Execute solve loops until flag found or cancelled
        for attempt in range(1, self.max_attempts + 1):
            if self.stop_event.is_set():
                self.logger(f"⏹️ [{worker_name}] Dừng cuộc đua do đã có worker khác bắt được Flag.")
                return None

            task.attempt = attempt
            prompt_angle = (
                f"Bạn là '{worker_name}' đang thi đấu giải tốc độ bài CTF này.\n"
                f"ĐẶC BIỆT CHÚ Ý: Hướng tiếp cận chuyên biệt của bạn là: {worker_angle}\n\n"
            )

            # Build prompt with angle
            files_context = runner._collect_files()
            prompt = (
                f"{prompt_angle}"
                f"Thư mục bài thi hiện tại:\n{files_context}\n\n"
                f"Target URL: {self.target_url or 'Offline / Local'}\n\n"
                f"Nhiệm vụ: Viết code Python hoàn chỉnh vào `{scratch_dir}/solve.py` để lấy Flag nhanh nhất. "
                f"Hãy in Flag ra stdout định dạng flag{{...}}."
            )

            if attempt > 1:
                prompt += f"\n\nLưu ý lần thử trước chưa thành công. Hãy tối ưu payload theo hướng: {worker_angle}."

            code, ai_out = await runner.run_claude_turn(prompt)

            if self.stop_event.is_set():
                return None

            # Extract python code
            import re
            py_blocks = re.findall(r"```python(.*?)```", ai_out, re.DOTALL)
            if py_blocks:
                solve_code = py_blocks[-1].strip()
                (scratch_dir / "solve.py").write_text(solve_code, encoding="utf-8")
                (scratch_dir / "solve.py").chmod(0o755)

            # Execute and check (Wait for instance if offline)
            await runner.ensure_instance_live()
            retcode, stdout, stderr, flag = await runner.execute_solve_script(timeout=40)

            if flag:
                task.flag = flag
                task.status = ChallengeStatus.SOLVED
                self.winner = task
                self.stop_event.set()
                self.logger(f"\n🏆🏆🏆 [{worker_name}] ĐÃ BẮT ĐƯỢC FLAG ĐẦU TIÊN VÀ CHIẾN THẮNG CUỘC ĐUA: {flag} 🏆🏆🏆\n")
                
                # Copy winning script and save flag to main directory
                shutil.copy2(scratch_dir / "solve.py", self.challenge_dir / "solve.py")
                (self.challenge_dir / "flag.txt").write_text(flag, encoding="utf-8")
                return task

            self.logger(f"⚠️ [{worker_name}] Vòng {attempt}/{self.max_attempts} chưa có flag. Đang thử tiếp...")

        return task

    async def run_race(self) -> ChallengeTask | None:
        start_time = time.monotonic()
        self.logger("=" * 80)
        self.logger(f"🏎️ BẮT ĐẦU CUỘC ĐUA SWARM RACE: {self.challenge_dir.name}")
        self.logger(f"⚡ Số Worker tham gia thi đấu cùng lúc: {self.num_workers} workers")
        self.logger("=" * 80)

        tasks = []
        for i in range(self.num_workers):
            name, angle = RACE_STRATEGIES[i]
            t = asyncio.create_task(self._worker_runner(i + 1, name, angle))
            tasks.append(t)

        await asyncio.gather(*tasks, return_exceptions=True)
        duration = time.monotonic() - start_time

        # Cleanup scratch
        shutil.rmtree(self.challenge_dir / ".swarm_scratch", ignore_errors=True)

        if self.winner and self.winner.flag:
            self.logger("\n" + "=" * 80)
            self.logger(f"🎉 KẾT QUẢ CUỘC ĐUA SWARM ({duration:.2f}s):")
            self.logger(f"👑 Quán quân : {self.winner.name}")
            self.logger(f"🚩 FLAG     : {self.winner.flag}")
            self.logger(f"💾 File code : {self.challenge_dir}/solve.py")
            self.logger("=" * 80)
            return self.winner
        else:
            self.logger("\n" + "=" * 80)
            self.logger(f"🚨 TẤT CẢ {self.num_workers} WORKERS ĐÃ HOÀN TẤT NHƯNG CHƯA TÌM THẤY FLAG ({duration:.2f}s).")
            self.logger("=" * 80)
            return None
