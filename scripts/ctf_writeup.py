#!/usr/bin/env python3
"""CTF Writeup Writer — tự động sinh writeup từ registry + workspace data.

Mỗi bài solved trong docs/automation/solved-flags.json mà chưa có
scratch/ctf-runs/<name>/WRITEUP.md sẽ được sinh writeup từ:
  - progress.json (method, flag, notes)
  - REPORT.md nếu có
  - solve.py / workspace files (tóm tắt approach)

Dùng:
  ctf_writeup.py --all                 # viết writeup cho mọi bài solved chưa có
  ctf_writeup.py --name <workspace>    # viết cho 1 bài
  ctf_writeup.py --list                # liệt kê solved + writeup status

Writeup lưu vào: scratch/ctf-runs/<name>/WRITEUP.md (gitignored workspace)
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = REPO_ROOT / "scratch" / "ctf-runs"
REGISTRY = REPO_ROOT / "docs" / "automation" / "solved-flags.json"

# Map workspace name → challenge dir (từ wave2-challenges + wave1 đã biết)
CHAL_MAP = {
    "half-baked": "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Misc/Half_Baked",
    "rubiks-cube": "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Forensics/Rubik's_Cube",
    "pi-crypt-057": "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Crypto/π-crypt_0.57",
    "magic-or-not": "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Misc/Magic_or_not",
    "unknown-artist": "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/OSINT/Unknown_Artist",
    "can-you-read-this": "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Forensics/CAN_you_read_this",
    "secret-storage": "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Crypto/Secret_Storage",
    "cleandesk": "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Mobile/CleanDesk",
    "north-star": "/home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/OSINT/The_North_Star_Metric",
    "qr-ptit": "/home/light/Workspace/CTF/.migration_backup_20260830_080722/PTIT_CTF_2026/Forensics/QR",
    "son-tung": "/home/light/Workspace/CTF/.migration_backup_20260830_080722/PTIT_CTF_2026/Forensics/Sơn_Tùng_M-TP",
    "signaling": "/home/light/Workspace/CTF/.migration_backup_20260830_080722/PTIT_CTF_2026/Crypto/Signaling",
    "stranger-papers": "/home/light/Workspace/CTF/.migration_backup_20260830_080722/PTIT_CTF_2026/Crypto/Stranger_Papers",
    "siren": "/home/light/Workspace/CTF/Z0d1ak_CTF/crypto/siren",
    "not-seen-colors": "/home/light/Workspace/CTF/Z0d1ak_CTF/crypto/You_Have_Not_Seen_My_Colors",
    "uncharted-tides": "/home/light/Workspace/CTF/Z0d1ak_CTF/osint/Uncharted_Tides",
    "genie": "/home/light/Workspace/CTF/Z0d1ak_CTF/misc/genie",
}


def load_registry() -> dict:
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"solved": {}}


def load_progress(name: str) -> dict:
    prog = RUNS_DIR / name / "progress.json"
    if prog.exists():
        try:
            return json.loads(prog.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def load_report(name: str) -> str:
    for f in ("REPORT.md", "report.md"):
        p = RUNS_DIR / name / f
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
    return ""


def find_flag_in_workspace(name: str) -> str | None:
    ws_dir = REPO_ROOT / "scratch" / "ctf-workspaces" / name
    if ws_dir.is_dir():
        for f in ws_dir.rglob("*"):
            if f.is_file() and f.name == "flag.txt":
                try:
                    return f.read_text().strip()
                except Exception:
                    pass
    return None


def challenge_name(name: str) -> str:
    chal = CHAL_MAP.get(name)
    if chal:
        return Path(chal).name
    return name


def generate_writeup(name: str, prog: dict, report: str, flag: str | None) -> str:
    chal = CHAL_MAP.get(name, name)
    method = prog.get("method", "")
    notes = prog.get("notes", "")
    llm_turns = prog.get("llm_turns", prog.get("turns", "?"))
    solved_at = prog.get("solved_at", prog.get("finished_at", prog.get("updated_at", "?")))

    # Trích method từ REPORT nếu progress thiếu
    if not method and report:
        m = re.search(r"## Method\s*\n(.*?)(?=\n## |\Z)", report, re.DOTALL)
        if m:
            method = m.group(1).strip()
        else:
            m2 = re.search(r"\*\*Method\*\*:?\s*(.*)", report)
            if m2:
                method = m2.group(1).strip()

    lines = [
        f"# Writeup: {challenge_name(name)}",
        "",
        f"- **Challenge**: {challenge_name(name)}",
        f"- **Path**: `{chal}`",
        f"- **Status**: ✅ SOLVED",
        f"- **Solved at**: {solved_at}",
        f"- **Flag**: `{flag or '???'}`",
        f"- **LLM turns**: {llm_turns}",
        "",
    ]

    if method:
        lines += ["## Method", "", method, ""]
    else:
        lines += ["## Method", "", "(chưa có chi tiết — xem progress.json/REPORT.md)", ""]

    if notes:
        lines += ["## Notes", "", notes, ""]

    if report:
        # Thêm toàn bộ REPORT làm appendix (đã có method ở trên)
        lines += ["---", "", "## Appendix: Full report", "", report, ""]

    lines += ["", f"*Writeup tự động sinh bởi `scripts/ctf_writeup.py` lúc "
              f"{datetime.datetime.now().isoformat(timespec='seconds')}*"]
    return "\n".join(lines)


def write_for(name: str) -> bool:
    prog = load_progress(name)
    status = prog.get("status", "")
    if status != "solved":
        # Có thể flag trong workspace nhưng progress chưa update
        flag = find_flag_in_workspace(name)
        if not flag:
            return False

    flag = prog.get("flag") or find_flag_in_workspace(name) or ""
    report = load_report(name)
    writeup = generate_writeup(name, prog, report, flag)

    out = RUNS_DIR / name / "WRITEUP.md"
    out.write_text(writeup, encoding="utf-8")
    print(f"[+] wrote {out} ({len(writeup)} chars)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="viết cho mọi bài solved chưa có writeup")
    ap.add_argument("--name", metavar="WORKSPACE", help="viết cho 1 bài")
    ap.add_argument("--list", action="store_true", help="liệt kê solved + writeup status")
    args = ap.parse_args()

    reg = load_registry()

    if args.list or not (args.all or args.name):
        print(f"Total solved in registry: {len(reg.get('solved', {}))}\n")
        for name in sorted(RUNS_DIR.iterdir()):
            if not name.is_dir():
                continue
            n = name.name
            prog = load_progress(n)
            if prog.get("status") != "solved":
                continue
            wp = RUNS_DIR / n / "WRITEUP.md"
            status = "✅" if wp.exists() else "❌"
            print(f"  {status} {n:22s} {prog.get('flag', '?')[:50]}")
        return 0

    if args.name:
        ok = write_for(args.name)
        print(f"[{'OK' if ok else 'SKIP'}] {args.name} "
              f"({'wrote' if ok else 'chưa solved / không có data'})")
        return 0 if ok else 1

    # --all
    written = skipped = 0
    for name_dir in sorted(RUNS_DIR.iterdir()):
        if not name_dir.is_dir():
            continue
        n = name_dir.name
        prog = load_progress(n)
        flag = prog.get("flag") or find_flag_in_workspace(n)
        if not flag:
            continue
        wp = RUNS_DIR / n / "WRITEUP.md"
        if wp.exists():
            skipped += 1
            continue
        if write_for(n):
            written += 1
    print(f"[summary] wrote={written} skipped_existing={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
