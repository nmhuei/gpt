from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

from gpt.orchestrator.session_runner import ClaudeCodeSessionRunner
from gpt.orchestrator.types import ChallengeStatus, ChallengeTask


class MasterAgentOrchestrator:
    """Master Orchestrator Agent: manages concurrent challenge dispatching,

    worker session lifecycle, error escalation, and live telemetry.
    """

    def __init__(
        self,
        concurrency: int = 4,
        max_attempts: int = 5,
        logger: Callable[[str], None] | None = None,
    ):
        self.concurrency = concurrency
        self.max_attempts = max_attempts
        self.logger = logger or print
        self.tasks: list[ChallengeTask] = []
        self.semaphore = asyncio.Semaphore(concurrency)

    def discover_challenges(self, root_dir: Path) -> list[ChallengeTask]:
        """Auto-discovers CTF challenge directories containing metadata.json or README.md."""
        discovered: list[ChallengeTask] = []
        root_dir = root_dir.resolve()

        # If root_dir itself is a single challenge
        if (root_dir / "metadata.json").exists() or (root_dir / "README.md").exists():
            # Check if it has sub-challenges
            sub_metas = list(root_dir.glob("*/**/metadata.json"))
            if not sub_metas:
                name = root_dir.name
                category = root_dir.parent.name if root_dir.parent else "Misc"
                points = 0
                target_url = None
                if (root_dir / "metadata.json").exists():
                    try:
                        m = json.loads((root_dir / "metadata.json").read_text())
                        name = m.get("name", name)
                        category = m.get("category", category)
                        points = m.get("value") or m.get("points", points)
                        target_url = m.get("connection_info") or m.get("instance_url")
                    except Exception:
                        pass
                discovered.append(
                    ChallengeTask(
                        directory=root_dir,
                        name=name,
                        category=category,
                        points=points,
                        target_url=target_url,
                        max_attempts=self.max_attempts,
                    )
                )
                self.tasks = discovered
                return discovered

        # Scan recursively
        for mpath in sorted(root_dir.glob("**/metadata.json")):
            cdir = mpath.parent
            name = cdir.name
            category = cdir.parent.name
            points = 0
            target_url = None
            try:
                m = json.loads(mpath.read_text())
                name = m.get("name", name)
                category = m.get("category", category)
                points = m.get("points", points)
                # Check if already solved
                if m.get("solved_by_me"):
                    continue
            except Exception:
                pass

            discovered.append(
                ChallengeTask(
                    directory=cdir,
                    name=name,
                    category=category,
                    points=points,
                    target_url=target_url,
                    max_attempts=self.max_attempts,
                )
            )

        self.tasks = discovered
        return discovered

    async def _worker_task(self, task: ChallengeTask) -> ChallengeTask:
        async with self.semaphore:
            self.logger(f"🚀 [Master Agent] Cấp phát worker cho bài: `{task.name}` ({task.category})")
            runner = ClaudeCodeSessionRunner(task, logger=lambda m: self.logger(f"[{task.name}] {m}"))
            return await runner.solve_challenge()

    async def run_all(self) -> list[ChallengeTask]:
        start_time = time.monotonic()
        self.logger("=" * 80)
        self.logger("👑 KHỞI CHẠY MASTER AGENT ORCHESTRATOR")
        self.logger(f"📊 Tổng số bài thi phát hiện : {len(self.tasks)}")
        self.logger(f"⚡ Số luồng xử lý song song   : {self.concurrency} workers")
        self.logger(f"🔄 Số lượt tự sửa tối đa     : {self.max_attempts} attempts / bài")
        self.logger("=" * 80)

        # Launch all tasks bounded by semaphore
        jobs = [self._worker_task(task) for task in self.tasks]
        results = await asyncio.gather(*jobs)
        total_time = time.monotonic() - start_time

        # Print Summary Report
        solved = [t for t in results if t.status == ChallengeStatus.SOLVED]
        escalated = [t for t in results if t.status == ChallengeStatus.ESCALATED]

        self.logger("\n" + "=" * 80)
        self.logger(f"📊 BÁO CÁO TỔNG HỢP TIẾN ĐỘ MASTER AGENT ({total_time:.2f}s):")
        self.logger("=" * 80)
        pct = (len(solved) / len(results) * 100) if results else 0.0
        self.logger(f"✅ Đã giải thành công : {len(solved)}/{len(results)} bài ({pct:.1f}%)")
        self.logger(f"🚨 Cần người dùng hỗ trợ : {len(escalated)}/{len(results)} bài")

        if solved:
            self.logger("\n🎉 DANH SÁCH BÀI THI ĐÃ GIẢI XONG & CÓ FLAG:")
            for s in solved:
                self.logger(f"  • [{s.category}] {s.name}: {s.flag} ({s.directory})")

        if escalated:
            self.logger("\n🚨 DANH SÁCH BÀI THI CẦN BẠN HỖ TRỢ (ĐÃ TỰ ĐỘNG TẠO BÁO CÁO CHẨN ĐOÁN):")
            for e in escalated:
                self.logger(f"  • [{e.category}] {e.name}:")
                self.logger(f"    - Báo cáo lỗi chi tiết: {e.directory}/NEEDS_HUMAN_REVIEW.md")
                self.logger(f"    - Lỗi gần nhất: {e.error_history[-1] if e.error_history else 'Không có phản hồi'}")

        self.logger("=" * 80)
        return results
