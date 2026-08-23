#!/usr/bin/env python3
"""
Benchmark Test: Parallel Solving of 10 CTF Challenges from BrunnerCTF Global
using Master Agent Orchestrator
"""

import asyncio
import json
import time
from pathlib import Path

from gpt.orchestrator.master_agent import MasterAgentOrchestrator
from gpt.orchestrator.types import ChallengeTask

TARGET_CHALLENGES = [
    "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Onboarding/Legacy_Cipher",
    "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Onboarding/Why_those_random_letters",
    "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Onboarding/Start_Here_Sanity_Check",
    "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Onboarding/Bears",
    "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Onboarding/Blackboard",
    "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Onboarding/Business_Trip",
    "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Onboarding/Going_Paperless",
    "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Onboarding/Invoice",
    "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Onboarding/Insanity_Check",
    "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Onboarding/Touch_Base",
]

def main():
    print("=" * 80)
    print("🚀 BẮT ĐẦU BENCHMARK 10 BÀI THI: BrunnerCTF 2026 Global")
    print("=" * 80)

    orchestrator = MasterAgentOrchestrator(
        concurrency=4,      # 4 concurrent worker sessions
        max_attempts=2,     # 2 strategy turns per challenge
    )

    tasks = []
    for cpath_str in TARGET_CHALLENGES:
        cdir = Path(cpath_str)
        if not cdir.exists():
            continue
        name = cdir.name
        category = "Onboarding"
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
    print(f"[*] Đã nạp thành công {len(tasks)} bài thi từ BrunnerCTF vào hàng đợi song song.")

    results = asyncio.run(orchestrator.run_all())

    # Save benchmark telemetry report
    report_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "competition": "BrunnerCTF 2026 Global",
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

    out_file = Path("/home/light/GitHub/gpt/scratch/benchmark_brunnerctf_report.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report_data, indent=2), encoding="utf-8")
    print(f"\n💾 Báo cáo benchmark chi tiết đã lưu tại: {out_file}")

if __name__ == "__main__":
    main()
