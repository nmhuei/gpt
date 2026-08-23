#!/usr/bin/env python3
import argparse
import asyncio
import sys
from pathlib import Path

from gpt.orchestrator.race_solver import SwarmRaceSolver


def main():
    parser = argparse.ArgumentParser(
        description="Competitive Multi-Agent Swarm Race Solver (All workers race on 1 challenge)"
    )
    parser.add_argument(
        "challenge_dir",
        help="Đường dẫn tới thư mục bài thi CTF",
    )
    parser.add_argument(
        "target_url",
        nargs="?",
        default=None,
        help="URL instance container (nếu có)",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=4,
        help="Số worker thi đấu cùng lúc (mặc định: 4 workers)",
    )
    parser.add_argument(
        "--max-attempts", "-m",
        type=int,
        default=3,
        help="Số lượt thử tối đa cho mỗi worker (mặc định: 3)",
    )

    args = parser.parse_args()
    cdir = Path(args.challenge_dir).resolve()

    if not cdir.exists():
        print(f"[!] Lỗi: Thư mục `{cdir}` không tồn tại.")
        sys.exit(1)

    solver = SwarmRaceSolver(
        challenge_dir=cdir,
        target_url=args.target_url,
        num_workers=args.workers,
        max_attempts_per_worker=args.max_attempts,
    )

    asyncio.run(solver.run_race())


if __name__ == "__main__":
    main()
