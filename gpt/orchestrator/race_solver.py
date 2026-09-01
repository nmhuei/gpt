from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gpt.orchestrator.session_runner import ClaudeCodeSessionRunner
from gpt.orchestrator.types import (
    ChallengeStatus,
    ChallengeTask,
    InstanceNotLiveError,
)


async def _relay_stop(src: asyncio.Event, dst: asyncio.Event) -> None:
    """Chờ global stop_event rồi relay sang per-worker stop_event."""
    await src.wait()
    dst.set()


async def _call_supporting_stop(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Gọi ``func(*args, stop_event=..., ...)`` nếu signature chấp nhận,
    ngược lại gỡ ``stop_event`` và gọi bình thường để tương thích ngược với runner/fake cũ."""
    if kwargs.get("stop_event") is not None:
        try:
            inspect.signature(func).bind(*args, **kwargs)
            return await func(*args, **kwargs)
        except (TypeError, ValueError):
            kwargs = {k: v for k, v in kwargs.items() if k != "stop_event"}
    return await func(*args, **kwargs)


def _env_float(name: str, default: str) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


RACE_STRATEGIES = [
    (
        "Grandmaster-1: Statistical Timing Oracle (Z-Score & IQR Filter)",
        "Tập trung xây dựng bộ đo Timing Side-Channel nâng cao: sử dụng thống kê Median, lọc nhiễu Outlier bằng IQR (Interquartile Range) hoặc Z-score để loại bỏ triệt để network jitter và trích xuất chính xác từng ký tự token."
    ),
    (
        "Grandmaster-2: Asynchronous High-Throughput Pipeline (100 req/s)",
        "Tập trung viết mã Python bất đồng bộ hoàn toàn (asyncio + httpx.AsyncClient / aiohttp) với Connection Pooling để đo đạc và fuzzing hàng trăm request song song với tốc độ tối đa."
    ),
    (
        "Grandmaster-3: Differential Status & Error Oracle Fuzzer",
        "Tập trung phân tích sự sai khác tinh vi giữa các phản hồi (Content-Length, Response Body, Headers, Cookies, HTTP 200/401/403/422/500) để phát hiện lỗ hổng rò rỉ thông tin."
    ),
    (
        "Grandmaster-4: Auth Bypass & Header Injection Specialist",
        "Tập trung kiểm tra các kỹ thuật vượt qua xác thực: Header injection (X-Forwarded-For, X-Real-IP, X-Original-URL), Bearer token formats, Parameter pollution, hoặc Type Juggling."
    ),
    (
        "Grandmaster-5: Heuristic Character Permutation & Binary Search",
        "Tập trung vào tối ưu hóa thuật toán tìm kiếm: sắp xếp tập ký tự theo tần suất xuất hiện (a-z0-9A-Z), thử nghiệm Prefix Tree hoặc Binary Search nếu độ trễ tỷ lệ thuận với độ dài khớp."
    ),
    (
        "Grandmaster-6: Cryptanalysis & PRNG Seed Cracker",
        "Tập trung phân tích cấu trúc mật mã học: phân tích entropy của token, PRNG seeds, chuỗi giả ngẫu nhiên, XOR keystreams hoặc padding oracles."
    ),
    (
        "Grandmaster-7: AST, Bytecode & Reverse Engineering Specialist",
        "Tập trung đọc sâu mã nguồn gốc, dịch ngược bytecode/AST, tìm kiếm các hàm hash không an toàn, hardcoded secrets hoặc logic flaw trong cơ chế so sánh chuỗi."
    ),
    (
        "Grandmaster-8: Full-Spectrum Exploit Synthesizer",
        "Tập trung tổng hợp toàn diện: kết hợp probe endpoint nhanh, trích xuất cấu trúc API và tự động hóa toàn bộ luồng gửi token để lấy Flag ngay lập tức."
    ),
]


class SwarmRaceSolver:
    """Competitive Multi-Agent Swarm Solver:

    Runs up to 8 Grandmaster worker agents with diverse specialized attack angles on
    the SAME challenge. The first worker to capture and verify the flag wins,
    instantly halting the rest.
    """

    def __init__(
        self,
        challenge_dir: Path,
        target_url: str | None = None,
        num_workers: int = 8,
        max_attempts_per_worker: int = 3,
        logger: Callable[[str], None] | None = None,
    ):
        self.challenge_dir = challenge_dir.resolve()
        self.target_url = target_url
        self.num_workers = min(num_workers, len(RACE_STRATEGIES))
        self.max_attempts = max_attempts_per_worker
        self.logger = logger or print
        self.winner: ChallengeTask | None = None
        self.worker_errors: dict[str, BaseException] = {}
        self.stop_event = asyncio.Event()
        self.worker_deadline = _env_float("WEBGPT_WORKER_DEADLINE", "3600")
        try:
            slots = int(os.environ.get("WEBGPT_SOLVE_CONCURRENCY", "4"))
        except ValueError:
            slots = 4
        self._solve_slots = asyncio.Semaphore(max(1, slots))

    async def _worker_runner(self, worker_idx: int, worker_name: str, worker_angle: str) -> ChallengeTask | None:
        scratch_dir = self.challenge_dir / ".swarm_scratch" / f"worker_{worker_idx}"
        shutil.rmtree(scratch_dir, ignore_errors=True)
        scratch_dir.mkdir(parents=True, exist_ok=True)

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

        self.logger(f"🏁 [{worker_name}] Bắt đầu xuất phát! Chiến thuật: {worker_angle}")

        # B3-full: per-worker stop relayed từ global stop_event để hủy được
        # cả subprocess đang chạy (cooperative cancel), không chỉ ở ranh giới attempt.
        worker_stop = asyncio.Event()
        stop_watcher = asyncio.create_task(_relay_stop(self.stop_event, worker_stop))

        worker_started = time.monotonic()
        try:
            for attempt in range(1, self.max_attempts + 1):
                if self.stop_event.is_set():
                    self.logger(f"⏹️ [{worker_name}] Dừng cuộc đua do đã có worker khác bắt được Flag.")
                    return None

                elapsed = time.monotonic() - worker_started
                if self.worker_deadline > 0 and elapsed >= self.worker_deadline:
                    task.status = ChallengeStatus.ESCALATED
                    self.logger(
                        f"⏰ [{worker_name}] Hết deadline tổng của worker ({self.worker_deadline:g}s, "
                        f"đã chạy {elapsed:.1f}s) — dừng trước vòng {attempt}/{self.max_attempts}."
                    )
                    break

                task.attempt = attempt
                prompt_angle = (
                    f"Bạn là '{worker_name}' - Đại kiện tướng an toàn thông tin đang thi đấu giải CTF này.\n"
                    f"ĐẶC BIỆT CHÚ Ý: Chiến thuật cốt lõi chuyên sâu của bạn là: {worker_angle}\n\n"
                )

                files_context = runner._collect_files()
                prompt = (
                    f"{prompt_angle}"
                    f"Thư mục bài thi hiện tại:\n{files_context}\n\n"
                    f"Target URL: {self.target_url or 'Offline / Local'}\n\n"
                    f"Nhiệm vụ: Viết code Python hoàn chỉnh vào `{scratch_dir}/solve.py` để giải quyết dứt điểm và lấy Flag.\n"
                    f"Code phải xử lý kết nối, bypass SSL (verify=False), timeout, in Flag rõ ràng định dạng flag{{...}} ra stdout.\n"
                    f"Hãy cung cấp code Python hoàn chỉnh trong khối ```python ... ```."
                )

                if attempt > 1:
                    prompt += f"\n\nLưu ý lần thử trước chưa thành công. Hãy nâng cấp thuật toán theo hướng: {worker_angle}."

                _code, ai_out = await _call_supporting_stop(
                    runner.run_claude_turn, prompt, stop_event=worker_stop
                )

                if self.stop_event.is_set():
                    # Attempt bị abort giữa chừng: KHÔNG ghi vào error_history.
                    self.logger(f"⏹️ [{worker_name}] bị hủy do race đã kết thúc")
                    return None

                import re
                py_blocks = re.findall(r"```python(.*?)```", ai_out, re.DOTALL)
                if py_blocks:
                    solve_code = py_blocks[-1].strip()
                    (scratch_dir / "solve.py").write_text(solve_code, encoding="utf-8")
                    (scratch_dir / "solve.py").chmod(0o755)

                # Wait for instance to be live before executing
                try:
                    await _call_supporting_stop(runner.ensure_instance_live, stop_event=worker_stop)
                except InstanceNotLiveError as exc:
                    if self.stop_event.is_set():
                        # Bị đánh thức bởi cooperative cancel, không phải instance chết thật.
                        self.logger(f"⏹️ [{worker_name}] bị hủy do race đã kết thúc")
                        return None
                    task.status = ChallengeStatus.ESCALATED
                    (scratch_dir / "NEEDS_HUMAN_REVIEW.md").write_text(
                        f"# NEEDS HUMAN REVIEW\n\n"
                        f"- **Worker:** {worker_name}\n"
                        f"- **Target URL:** {self.target_url or 'N/A'}\n"
                        f"- **Lý do:** instance không sống sau khi chờ — {exc}\n",
                        encoding="utf-8",
                    )
                    self.logger(f"🚨 [{worker_name}] {exc} — chuyển sang NEEDS_HUMAN_REVIEW.")
                    break

                async with self._solve_slots:
                    _retcode, _stdout, _stderr, flag = await _call_supporting_stop(
                        runner.execute_solve_script, timeout=45, stop_event=worker_stop
                    )

                if not flag and self.stop_event.is_set():
                    # Attempt bị abort giữa chừng: KHÔNG ghi vào error_history.
                    self.logger(f"⏹️ [{worker_name}] bị hủy do race đã kết thúc")
                    return None

                if flag:
                    task.flag = flag
                    task.status = ChallengeStatus.SOLVED
                    self.winner = task
                    self.stop_event.set()
                    self.logger(f"\n🏆🏆🏆 [{worker_name}] ĐÃ BẮT ĐƯỢC FLAG ĐẦU TIÊN VÀ CHIẾN THẮNG CUỘC ĐUA: {flag} 🏆🏆🏆\n")

                    shutil.copy2(scratch_dir / "solve.py", self.challenge_dir / "solve.py")
                    (self.challenge_dir / "flag.txt").write_text(flag, encoding="utf-8")
                    return task

                self.logger(f"⚠️ [{worker_name}] Vòng {attempt}/{self.max_attempts} chưa có flag. Đang nâng cấp payload...")
        finally:
            stop_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_watcher

        return task

    async def run_race(self) -> ChallengeTask | None:
        start_time = time.monotonic()
        self.logger("=" * 80)
        self.logger("🏎️ BẮT ĐẦU CUỘC ĐUA ĐỈNH CAO: 8 GRANDMASTER SWARM RACE")
        self.logger(f"🎯 Bài thi   : {self.challenge_dir.name}")
        self.logger(f"⚡ Số Worker : {self.num_workers} Grandmaster Workers cùng xuất phát")
        self.logger("=" * 80)

        tasks = []
        for i in range(self.num_workers):
            name, angle = RACE_STRATEGIES[i]
            t = asyncio.create_task(self._worker_runner(i + 1, name, angle))
            tasks.append(t)

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for idx, res in enumerate(results):
            if isinstance(res, BaseException):
                worker_name = RACE_STRATEGIES[idx][0]
                self.logger(f"[worker {idx + 1}] FAILED: {res!r}")
                self.worker_errors[worker_name] = res
        duration = time.monotonic() - start_time

        shutil.rmtree(self.challenge_dir / ".swarm_scratch", ignore_errors=True)

        solved_by = self.winner.name if (self.winner and self.winner.flag) else "Không ai"
        clean_losses = max(
            0,
            self.num_workers - len(self.worker_errors) - (1 if solved_by != "Không ai" else 0),
        )

        # Bảng tổng hợp kết quả đua: ai giải được, ai crash, ai thua sạch.
        self.logger("\n📊 TỔNG KẾT SWARM RACE:")
        self.logger(f"   👑 Solved by      : {solved_by}")
        self.logger(f"   ❌ Failed workers : {len(self.worker_errors)}")
        for wname, exc in self.worker_errors.items():
            self.logger(f"      - [{wname}]: {exc!r}")
        self.logger(f"   🧹 Clean losses   : {clean_losses}")

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
