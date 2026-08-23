#!/usr/bin/env python3
import argparse
import asyncio
import sys
from pathlib import Path

from gpt.orchestrator.master_agent import MasterAgentOrchestrator


def main():
    parser = argparse.ArgumentParser(
        description="Master Agent Orchestrator for Parallel Claude Code CTF Solving & Self-Healing"
    )
    parser.add_argument(
        "target",
        help="Đường dẫn tới thư mục bài thi đơn lẻ hoặc thư mục gốc chứa nhiều bài thi CTF",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=4,
        help="Số luồng giải song song (mặc định: 4 workers)",
    )
    parser.add_argument(
        "--max-retries", "-m",
        type=int,
        default=5,
        help="Số lần tự thử lại / đổi chiến thuật tối đa cho mỗi bài (mặc định: 5)",
    )
    parser.add_argument(
        "--url", "-u",
        default=None,
        help="Target URL (áp dụng khi giải 1 bài đơn lẻ có container instance)",
    )

    args = parser.parse_args()
    target_path = Path(args.target).resolve()

    if not target_path.exists():
        print(f"[!] Lỗi: Đường dẫn `{target_path}` không tồn tại.")
        sys.exit(1)

    orchestrator = MasterAgentOrchestrator(
        concurrency=args.workers,
        max_attempts=args.max_retries,
    )

    tasks = orchestrator.discover_challenges(target_path)
    if not tasks:
        print(f"[!] Không tìm thấy bài thi nào trong `{target_path}`.")
        sys.exit(1)

    if args.url and len(tasks) == 1:
        tasks[0].target_url = args.url

    asyncio.run(orchestrator.run_all())


if __name__ == "__main__":
    main()
