#!/usr/bin/env python3
"""Offline selfcheck for all Practical Bench V2 scenarios.

For every V2 task, the pristine fixture must FAIL grading and the canonical
solved tree must PASS. The TDD scenario reconstructs the required git history
in a temporary candidate so process grading is tested too. No gateway or
external network is used.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PRACTICAL = REPO / "benchmarks" / "practical"
GRADE = PRACTICAL / "grade.py"
TASKS = (
    "v2_bugfix_multi",
    "v2_tdd_tokenbucket",
    "v2_refactor_property",
    "v2_log_debug",
    "v2_api_integration",
)


def _run(task: str, ws: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(GRADE), "--task", task, "--ws", str(ws)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=240,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _git(cwd: Path, *args: str) -> None:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout).strip())


def _tdd_candidate(parent: Path) -> Path:
    fixture = PRACTICAL / "tasks" / "v2_tdd_tokenbucket" / "fixture"
    solved = PRACTICAL / "selfcheck" / "v2_tdd_tokenbucket-solved"
    ws = parent / "v2_tdd_tokenbucket"
    shutil.copytree(fixture, ws)
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "bench@example.invalid")
    _git(ws, "config", "user.name", "Bench Selfcheck")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "[bench] pristine fixture")
    shutil.copy2(solved / "tests" / "test_token_bucket.py", ws / "tests" / "test_token_bucket.py")
    _git(ws, "add", "tests/test_token_bucket.py")
    _git(ws, "commit", "-q", "-m", "[tdd] tests token bucket")
    shutil.copy2(solved / "ratelimit" / "bucket.py", ws / "ratelimit" / "bucket.py")
    _git(ws, "add", "ratelimit/bucket.py")
    _git(ws, "commit", "-q", "-m", "[tdd] impl token bucket")
    return ws


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=Path, default=PRACTICAL / "selfcheck" / "selfcheck-v2.log")
    args = ap.parse_args(argv)
    lines: list[str] = []
    overall = True
    with tempfile.TemporaryDirectory(prefix="webgpt-bench-v2-selfcheck-") as raw:
        tmp = Path(raw)
        for task in TASKS:
            pristine = PRACTICAL / "tasks" / task / "fixture"
            rc_pristine, _ = _run(task, pristine)
            if task == "v2_tdd_tokenbucket":
                candidate = _tdd_candidate(tmp)
            else:
                candidate = PRACTICAL / "selfcheck" / f"{task}-solved"
            rc_solved, solved_out = _run(task, candidate)
            ok = rc_pristine != 0 and rc_solved == 0
            overall &= ok
            line = f"{task}: pristine={'FAIL' if rc_pristine else 'UNEXPECTED_PASS'} solved={'PASS' if rc_solved == 0 else 'FAIL'}"
            lines.append(line)
            print(line)
            if rc_solved != 0:
                print(solved_out[-2000:])
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"RESULT: {'PASS' if overall else 'FAIL'} ({sum('solved=PASS' in x for x in lines)}/{len(lines)} solved)")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
