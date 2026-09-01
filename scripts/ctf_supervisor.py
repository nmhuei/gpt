#!/usr/bin/env python3
"""CTF Supervisor — gom incident từ 5 solver session, ghi vào incidents/.

Khi supervisor phát hiện bất thường, ghi 1 file incident-<ts>-<name>.json
vào scratch/ctf-incidents/ để coordinator spawn research/fix agent.

Các loại incident:
  - cyber-refusal: response chứa "cybersecurity requests"
  - session-stuck: PID sống nhưng log không tăng >15 phút
  - session-dead: PID không còn tồn tại nhưng status chưa solved
  - gateway-degraded: /health không OK
  - low-ram: RAM < 2GB available

Cron 20p sẽ gọi script này.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RUNS_DIR = Path("/home/light/GitHub/gpt/scratch/ctf-runs")
INCIDENTS_DIR = Path("/home/light/GitHub/gpt/scratch/ctf-incidents")
INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)

CYBER_REFUSAL_PATTERNS = [
    "this content can't be shown",
    "cybersecurity requests",
    "extra caution",
    "i can't help with that",
    "i'm not able to",
]

GATEWAY = "http://127.0.0.1:18000"
STUCK_THRESHOLD_S = 15 * 60   # log không tăng 15' = stuck
RAM_LOW_MB = 2048             # <2GB available = low


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


def read_tail(path: Path, n: int = 200) -> str:
    try:
        if not path.exists():
            return ""
        # Đọc n dòng cuối, an toàn với file lớn
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 50000))  # ~50KB cuối
            data = f.read().decode("utf-8", errors="replace")
        return data
    except Exception as e:
        return f"(read error: {e})"


def curl_health() -> dict:
    try:
        out = subprocess.run(
            ["curl", "-sf", "-m", "3", f"{GATEWAY}/health"],
            capture_output=True, text=True, timeout=4,
        )
        if out.returncode == 0:
            return {"ok": True, "body": json.loads(out.stdout)}
        return {"ok": False, "error": f"rc={out.returncode}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def ram_available_mb() -> int:
    try:
        out = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=3)
        for line in out.stdout.splitlines():
            if line.startswith("Mem:"):
                return int(line.split()[6])
    except Exception:
        pass
    return -1


def find_cyber_refusal(log_text: str) -> str | None:
    low = log_text.lower()
    for pat in CYBER_REFUSAL_PATTERNS:
        if pat in low:
            # Tìm dòng chứa pattern, lấy 200 chars xung quanh
            idx = low.find(pat)
            start = max(0, idx - 100)
            end = min(len(log_text), idx + 200)
            return log_text[start:end].strip()
    return None


def find_flag(log_text: str) -> str | None:
    m = re.search(r"brunner\{[^}]+\}|flag\{[^}]+\}", log_text, re.IGNORECASE)
    return m.group(0) if m else None


def write_incident(kind: str, name: str, details: dict) -> None:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = INCIDENTS_DIR / f"incident-{ts}-{kind}-{name}.json"
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "session": name,
        "details": details,
        "suggested_action": SUGGESTED_ACTIONS.get(kind, "investigate"),
    }
    fname.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[!] INCIDENT: {kind} for {name} -> {fname.name}")


SUGGESTED_ACTIONS = {
    "cyber-refusal": "spawn research-agent: classify bài theo docs/reports/classifier-pass-research-2026-08-26.md, đề xuất reframe hoặc bỏ",
    "session-stuck": "spawn research-agent: đọc log, tìm tool-call nào đang treo, đề xuất kill hoặc gửi nudge",
    "session-dead": "spawn research-agent: tại sao PID chết, log dừng ở đâu, cần restart hay bỏ",
    "gateway-degraded": "spawn research-agent: journalctl gateway, tìm root cause FailoverRetryRequired/auth-wall, đề xuất restart workflow",
    "low-ram": "kill 1-2 session, giảm concurrency, hoặc restart gateway",
    "flag-found": "không cần research, chỉ verify và ghi writeup",
    "session-completed": "không cần research, đánh dấu done",
}


def check_session(name: str) -> list[tuple[str, dict]]:
    """Trả về list các incident cho 1 session."""
    incidents: list[tuple[str, dict]] = []
    ws = RUNS_DIR / name
    prog = ws / "progress.json"
    log = ws / "session.log"
    pid_file = ws / "session.pid"

    if not prog.exists():
        return incidents

    try:
        prog_data = json.loads(prog.read_text())
    except Exception:
        return incidents

    status = prog_data.get("status", "unknown")

    # Đã solved/ended/blocked → không check nữa
    if status in {"solved", "blocked-cyber", "timeout", "ended-no-flag", "error-no-binary", "blocked-instance-expired"}:
        return incidents

    pid = None
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except Exception:
            pass
    elif "pid" in prog_data:
        try:
            pid = int(prog_data["pid"])
        except Exception:
            pass

    # Check 1: session-dead
    if pid and not is_alive(pid):
        # Trừ khi status đã update (solver tự update trước khi exit)
        if status not in {"solved", "blocked-cyber", "ended-no-flag"}:
            log_tail = read_tail(log, 200)
            incidents.append(("session-dead", {
                "pid": pid,
                "last_log_size": log.stat().st_size if log.exists() else 0,
                "log_tail": log_tail[-2000:],
            }))

    # Check 2: cyber-refusal
    if log.exists():
        log_text = read_tail(log, 500)
        refusal = find_cyber_refusal(log_text)
        if refusal:
            incidents.append(("cyber-refusal", {
                "transcript": refusal,
                "log_size": log.stat().st_size,
            }))

        # Check 3: flag-found (cập nhật progress, không phải incident thật)
        flag = find_flag(log_text)
        if flag and status not in {"solved"}:
            incidents.append(("flag-found", {
                "flag": flag,
                "log_size": log.stat().st_size,
            }))

    # Check 4: session-stuck
    if log.exists() and status == "running":
        age = file_age_s(log)
        if age > STUCK_THRESHOLD_S and pid and is_alive(pid):
            incidents.append(("session-stuck", {
                "pid": pid,
                "log_age_seconds": int(age),
                "log_size": log.stat().st_size,
            }))

    return incidents


def main() -> int:
    now = datetime.now().isoformat(timespec="seconds")
    summary = {"ts": now, "sessions": [], "incidents": 0}

    # Gateway health
    gw = curl_health()
    if not gw["ok"]:
        write_incident("gateway-degraded", "global", gw)
        summary["incidents"] += 1

    ram = ram_available_mb()
    if 0 < ram < RAM_LOW_MB:
        write_incident("low-ram", "global", {"ram_available_mb": ram})
        summary["incidents"] += 1

    # Scan tất cả session
    if not RUNS_DIR.is_dir():
        print(f"FATAL: runs dir not found: {RUNS_DIR}", file=sys.stderr)
        return 2

    for ws in sorted(RUNS_DIR.iterdir()):
        if not ws.is_dir():
            continue
        name = ws.name
        if name in {"war-game-coord", "optimizer"}:
            continue
        # Chỉ check session có session.pid hoặc progress.json
        if not (ws / "session.pid").exists() and not (ws / "progress.json").exists():
            continue

        incidents = check_session(name)
        s = {"name": name, "incidents": [k for k, _ in incidents]}
        summary["sessions"].append(s)
        for kind, details in incidents:
            write_incident(kind, name, details)
            summary["incidents"] += 1

    # Lưu summary
    summary_path = Path("/home/light/GitHub/gpt/scratch/ctf-monitor/supervisor-summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"[supervisor] sessions={len(summary['sessions'])} incidents={summary['incidents']}")
    return 0


if __name__ == "__main__":
    import os  # noqa: E402
    raise SystemExit(main())
