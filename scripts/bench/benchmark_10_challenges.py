#!/usr/bin/env python3
"""
Benchmark Test: Parallel Solving of 10 CTF Challenges via Master Agent Orchestrator
"""

import asyncio
import json
import time
from pathlib import Path

from gpt.orchestrator.master_agent import MasterAgentOrchestrator
from gpt.orchestrator.types import ChallengeTask

# List of 10 target challenges
TARGET_CHALLENGES = [
    "/home/light/Workspace/CTF/CTF_Da_Nang_2026/Crypto/Crypto_Challenge_1",
    "/home/light/Workspace/CTF/CTF_Da_Nang_2026/Crypto/Crypto_Challenge_2",
    "/home/light/Workspace/CTF/CTF_Da_Nang_2026/Crypto/Crypto_Challenge_3",
    "/home/light/Workspace/CTF/CTF_Da_Nang_2026/Misc/Misc_Challenge_2",
    "/home/light/Workspace/CTF/CTF_Da_Nang_2026/Misc/Misc_Challenge_3",
    "/home/light/Workspace/CTF/CTF_Da_Nang_2026/Misc/OSINT_Challenge_1",
    "/home/light/Workspace/CTF/CTF_Da_Nang_2026/Reverse/Reverse_Challenge_1",
    "/home/light/Workspace/CTF/CTF_Da_Nang_2026/Reverse/Reverse_Challenge_2",
    "/home/light/Workspace/CTF/CTF_Da_Nang_2026/Reverse/Reverse_Challenge_3",
    "/home/light/Workspace/CTF/CTF_Da_Nang_2026/Web/Web_Challenge_1",
]

def main():
    print("=" * 80)
    print("🚀 BẮT ĐẦU BENCHMARK: THỬ SỨC ĐIỀU PHỐI 10 BÀI THI CTF ĐỒNG THỜI")
    print("=" * 80)

    orchestrator = MasterAgentOrchestrator(
        concurrency=4,      # 4 concurrent workers
        max_attempts=2,     # 2 strategy turns per challenge for benchmark
    )

    tasks = []
    for cpath_str in TARGET_CHALLENGES:
        cdir = Path(cpath_str)
        if not cdir.exists():
            continue
        name = cdir.name
        category = cdir.parent.name
        points = 0
        mpath = cdir / "metadata.json"
        if mpath.exists():
            try:
                m = json.loads(mpath.read_text())
                name = m.get("name", name)
                category = m.get("category", category)
                points = m.get("points", points)
            except Exception:
                pass
        tasks.append(
            ChallengeTask(
                directory=cdir,
                name=name,
                category=category,
                points=points,
                max_attempts=2,
            )
        )

    orchestrator.tasks = tasks
    print(f"[*] Đã nạp thành công {len(tasks)} bài thi vào danh sách điều phối.")

    results = asyncio.run(orchestrator.run_all())

    # Save benchmark telemetry report
    report_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_challenges": len(results),
        "solved_count": len([t for t in results if t.status.value == "SOLVED"]),
        "escalated_count": len([t for t in results if t.status.value == "ESCALATED_NEEDS_HUMAN"]),
        "challenges": [
            {
                "name": t.name,
                "category": t.category,
                "status": t.status.value,
                "flag": t.flag,
                "attempts": t.attempt,
                "duration_seconds": round(t.end_time - t.start_time, 2) if t.end_time else 0,
            }
            for t in results
        ]
    }

    out_file = Path("/home/light/GitHub/gpt/scratch/benchmark_10_challenges_report.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report_data, indent=2), encoding="utf-8")
    print(f"\n💾 Đã lưu dữ liệu benchmark vào: {out_file}")

if __name__ == "__main__":
    main()
