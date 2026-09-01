#!/usr/bin/env python3
"""CTF mode monitor — chạy mỗi 20p, log health+solver+POST count vào log file.

Không restart gì — chỉ quan sát, để coordinator quyết định.
Exit 0 luôn để cron không báo failed.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("/home/light/GitHub/gpt/scratch/ctf-monitor")
LOG_FILE = LOG_DIR / "monitor.log"
STATE_FILE = LOG_DIR / "last-state.json"
GATEWAY = os.environ.get("WEBGPT_GATEWAY_URL", "http://127.0.0.1:18000")


def curl_health() -> dict:
    try:
        out = subprocess.run(
            ["curl", "-sf", "-m", "3", f"{GATEWAY}/health"],
            capture_output=True, text=True, timeout=4,
        )
        if out.returncode == 0:
            return {"ok": True, "body": json.loads(out.stdout)}
        return {"ok": False, "error": out.stderr.strip() or f"rc={out.returncode}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_solvers() -> list[dict]:
    """Tìm mọi process solve_ctf/auto_solver đang chạy."""
    out = subprocess.run(
        ["pgrep", "-af", "solve_ctf|auto_solver|ctf_solver"],
        capture_output=True, text=True,
    )
    solvers = []
    for line in out.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid, cmd = parts
        solvers.append({"pid": int(pid), "cmd": cmd[:200]})
    return solvers


def count_recent_posts(seconds: int = 1500) -> int:
    """Đếm số POST tới gateway trong N giây gần nhất qua journal."""
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", "webgpt-gateway.service",
             f"--since=-{seconds}s", "--no-pager", "-q"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return -1
        return sum(1 for ln in out.stdout.splitlines() if "POST" in ln)
    except Exception:
        return -2


def ram_available_mb() -> int:
    try:
        out = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=3)
        for line in out.stdout.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                return int(parts[6])  # available column
    except Exception:
        pass
    return -1


def tail_log(path: Path, n: int = 3) -> list[str]:
    try:
        if not path.exists():
            return ["(no log file yet)"]
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:] if lines else ["(empty)"]
    except Exception as e:
        return [f"(read error: {e})"]


def per_challenge_status() -> list[dict]:
    """Đọc progress từ scratch/ctf-runs/<chal>/progress.json nếu có."""
    runs_dir = LOG_DIR.parent / "ctf-runs"
    if not runs_dir.is_dir():
        return []
    out = []
    for d in sorted(runs_dir.iterdir()):
        if not d.is_dir():
            continue
        prog = d / "progress.json"
        if prog.exists():
            try:
                data = json.loads(prog.read_text())
                data["name"] = d.name
                out.append(data)
            except Exception:
                out.append({"name": d.name, "error": "bad progress.json"})
    return out


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().isoformat(timespec="seconds")
    gateway = curl_health()
    solvers = list_solvers()
    recent_posts = count_recent_posts(1500)
    available_ram = ram_available_mb()
    challenges = per_challenge_status()
    state = {
        "ts": timestamp,
        "gateway": gateway,
        "solvers": solvers,
        "recent_posts_25min": recent_posts,
        "ram_available_mb": available_ram,
        "challenges": challenges,
    }

    # Ghi log (append, 1 dòng JSON mỗi tick)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(state, ensure_ascii=False) + "\n")

    # Lưu state mới nhất
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

    # In summary ra stdout (cron sẽ mail nếu output, nhưng exit 0 nên thường im)
    gw_str = "OK" if gateway.get("ok") else f"DOWN({gateway.get('error', '?')})"
    print(f"[{timestamp}] gw={gw_str} "
          f"solvers={len(solvers)} "
          f"posts25m={recent_posts} "
          f"ram={available_ram}MB "
          f"challenges={len(challenges)}")
    for c in challenges:
        print(f"  - {c.get('name', '?')}: status={c.get('status', '?')} "
              f"turns={c.get('turns', 0)} last={c.get('last_turn_at', '?')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
