#!/usr/bin/env python3
"""CTF Recruiter — tự động phát hiện sub-agent chết/treo và ghi lệnh recruit.

Chạy mỗi tick cron (20p). Khi phát hiện 1 session:
  - CHẾT (PID không còn, status chưa solved/blocked) → ghi incident recruit-needed
  - TREO (PID sống nhưng log không đổi >15') → ghi incident stuck
  - ĐÃ SOLVED → bỏ qua (flag.txt ghi rồi)

Khi coordinator (session chính) thấy incident recruit-needed → spawn sub-agent mới
cho bài đó (recruit thật). Script này CHỈ PHÁT HIỆN + GHI, không tự spawn.

Output: scratch/ctf-incidents/recruit-<ts>-<name>.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RUNS_DIR = Path("/home/light/GitHub/gpt/scratch/ctf-runs")
INCIDENTS_DIR = Path("/home/light/GitHub/gpt/scratch/ctf-incidents")
INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)

# Trạng thái terminal (không cần recruit)
TERMINAL = {"solved", "blocked-cyber", "timeout", "ended-no-flag",
            "error-no-binary", "solved-skip", "recruit-failed",
            "blocked-instance-expired"}
SKIP_NAMES = {"war-game-coord", "optimizer"}
STUCK_THRESHOLD_S = 15 * 60  # log không đổi 15' = treo
STUCK_NO_LOG_S = 10 * 60     # PID sống nhưng chưa có log 10' = nghi treo


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def file_age_s(path: Path) -> float:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return float("inf")


def read_progress(ws: Path) -> dict:
    prog = ws / "progress.json"
    if not prog.exists():
        return {}
    try:
        return json.loads(prog.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_incident(name: str, kind: str, details: dict) -> None:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = INCIDENTS_DIR / f"recruit-{ts}-{kind}-{name}.json"
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,          # "session-dead" | "session-stuck" | "flag-found" | "cyber-refusal"
        "session": name,
        "details": details,
        "action": "recruit-new-subagent",
    }
    fname.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[recruiter] {kind}: {name} -> {fname.name}")


def check_session(name: str) -> list[str]:
    """Trả về danh sách incident kinds cho 1 session."""
    if name in SKIP_NAMES:
        return []

    ws = RUNS_DIR / name
    prog_data = read_progress(ws)
    status = prog_data.get("status", "unknown")

    # Terminal → không recruit
    if status in TERMINAL:
        return []

    pid_file = ws / "session.pid"
    log_file = ws / "session.log"

    pid: int | None = None
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except Exception:
            pid = None
    elif "pid" in prog_data:
        try:
            pid = int(prog_data["pid"])
        except Exception:
            pid = None

    prog_age = file_age_s(ws / "progress.json")

    # Nếu có PID và process đã chết
    if pid is not None and not is_alive(pid):
        if status not in TERMINAL:
            if prog_age <= STUCK_THRESHOLD_S:
                return []
            write_incident(name, "session-dead", {
                "pid": pid,
                "status_at_death": status,
                "progress_age_seconds": int(prog_age),
                "log_tail": (log_file.read_text(errors="replace")[-1000:]
                             if log_file.exists() else ""),
            })
            return ["session-dead"]
        return []

    # Không có PID file / PID không xác định nhưng status đang chạy / dở dang
    if pid is None:
        if status not in TERMINAL and prog_age > STUCK_THRESHOLD_S:
            write_incident(name, "session-stuck", {
                "progress_age_seconds": int(prog_age),
                "status": status,
                "note": "sub-agent mode: progress.json không đổi quá lâu",
            })
            return ["session-stuck"]
        return []

    incidents: list[str] = []

    # 1. CHẾT
    if not is_alive(pid):
        if status not in TERMINAL:
            # Bỏ qua nếu progress.json còn MỚI (agent tự quản lý, spawn process mới
            # nhưng chưa update session.pid) — chỉ recruit khi thật sự đứng im.
            prog_age = file_age_s(ws / "progress.json")
            if prog_age <= STUCK_THRESHOLD_S:
                return []
            write_incident(name, "session-dead", {
                "pid": pid,
                "status_at_death": status,
                "progress_age_seconds": int(prog_age),
                "log_tail": (log_file.read_text(errors="replace")[-1000:]
                             if log_file.exists() else ""),
            })
            incidents.append("session-dead")
        return incidents

    # 2. TREO
    if log_file.exists():
        age = file_age_s(log_file)
        if age > STUCK_THRESHOLD_S:
            write_incident(name, "session-stuck", {
                "pid": pid,
                "log_age_seconds": int(age),
                "log_size": log_file.stat().st_size,
            })
            incidents.append("session-stuck")
    else:
        # PID sống nhưng chưa có log → nghi treo từ đầu
        age = file_age_s(ws / "progress.json")
        if age > STUCK_NO_LOG_S:
            write_incident(name, "session-stuck", {
                "pid": pid,
                "note": "no log file yet, progress.json cũ",
                "progress_age_seconds": int(age),
            })
            incidents.append("session-stuck")

    return incidents


def main() -> int:
    now = datetime.now().isoformat(timespec="seconds")
    summary = {"ts": now, "sessions_checked": 0, "recruit_needed": 0,
               "details": []}

    if not RUNS_DIR.is_dir():
        print(f"FATAL: runs dir not found: {RUNS_DIR}", file=sys.stderr)
        return 2

    for ws in sorted(RUNS_DIR.iterdir()):
        if not ws.is_dir():
            continue
        name = ws.name
        # Chỉ check session thuộc wave (có progress.json + status not pending quá lâu)
        prog = ws / "progress.json"
        if not prog.exists():
            continue

        # Bỏ qua pending quá trẻ (<5') — agent mới spawn chưa kịp update
        prog_age = file_age_s(prog)
        prog_data = read_progress(ws)
        status = prog_data.get("status", "unknown")
        if status == "pending" and prog_age < 300:
            continue

        summary["sessions_checked"] += 1
        incidents = check_session(name)
        if incidents:
            summary["recruit_needed"] += 1
            summary["details"].append({"session": name, "kinds": incidents})

    # Ghi summary
    summary_file = INCIDENTS_DIR / "recruiter-summary.json"
    summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"[recruiter] checked={summary['sessions_checked']} "
          f"recruit_needed={summary['recruit_needed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
