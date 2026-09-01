#!/usr/bin/env python3
"""CTF flag registry — ghi nhận flag đã solve, tránh agent sau giải lại.

Flow chuẩn:
  1. Agent solve thành công → gọi `ctf_flag_registry.py --add <chal-dir> <flag>`
  2. Agent/challenge PICKER trước khi chọn bài → gọi `--check <chal-dir>`
     (hoặc chạy `--list` để xem tất cả)
  3. Supervisor/challenge giải mới → `--mark-flag <chal-dir> <flag>` (alias add)

Hành vi:
  - Ghi flag vào BOTH:
      a) `<chal-dir>/flag.txt` (picker tự reject bài có flag.txt)
      b) `docs/automation/solved-flags.json` (registry trung tâm, git-tracked)
  - Không overwrite nếu flag.txt đã tồn tại (trừ --force)
  - --check exit 0 = đã solve, 1 = chưa solve (dùng trong script để bỏ qua)

Usage:
  ctf_flag_registry.py --add <chal-dir> <flag> [--writeup <path>]
  ctf_flag_registry.py --check <chal-dir>
  ctf_flag_registry.py --list [--json]
  ctf_flag_registry.py --sync   # quét challenge dirs có flag.txt → registry
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "docs" / "automation" / "solved-flags.json"


def load_registry() -> dict:
    if REGISTRY.exists():
        try:
            data = json.loads(REGISTRY.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"solved": {}, "updated_at": None}


def save_registry(data: dict) -> None:
    data["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.rename(REGISTRY)


def normalize_chal_dir(chal_dir: str) -> str:
    return str(Path(chal_dir).expanduser().resolve())


def add_flag(chal_dir: str, flag: str, writeup: str | None = None, force: bool = False) -> int:
    path = normalize_chal_dir(chal_dir)
    chal = Path(path)
    if not chal.is_dir():
        print(f"[!] not a directory: {chal}", file=sys.stderr)
        return 2

    # 1. Ghi flag.txt vào challenge dir (picker tự reject bài này)
    flag_file = chal / "flag.txt"
    existing = flag_file.read_text().strip() if flag_file.exists() else ""
    if existing and existing != flag.strip() and not force:
        print(f"[!] flag.txt exists with DIFFERENT flag: {existing[:40]}...", file=sys.stderr)
        print(f"    Use --force to overwrite with: {flag[:40]}...", file=sys.stderr)
        return 3
    flag_file.write_text(flag.strip() + "\n", encoding="utf-8")
    print(f"[+] wrote {flag_file}")

    # 2. Cập nhật registry trung tâm
    reg = load_registry()
    entry = {
        "flag": flag.strip(),
        "solved_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    if writeup:
        entry["writeup"] = str(writeup)
    reg["solved"][path] = entry
    save_registry(reg)
    print(f"[+] registry: {REGISTRY} ({len(reg['solved'])} total solved)")
    return 0


def check_flag(chal_dir: str) -> int:
    """Exit 0 nếu đã solve (flag.txt tồn tại hoặc trong registry), else 1."""
    path = normalize_chal_dir(chal_dir)
    chal = Path(path)

    # 1. flag.txt trong challenge dir
    if (chal / "flag.txt").exists():
        print(f"[solved] {path} (flag.txt)")
        return 0

    # 2. Registry trung tâm
    reg = load_registry()
    if path in reg.get("solved", {}):
        print(f"[solved] {path} (registry: {reg['solved'][path]['flag'][:40]}...)")
        return 0

    print(f"[unsolved] {path}")
    return 1


def sync_from_disk() -> int:
    """Quét mọi challenge dir có flag.txt → registry."""
    reg = load_registry()
    added = 0
    ctf_root = Path.home() / "Workspace" / "CTF"
    if not ctf_root.is_dir():
        print(f"[!] no CTF root at {ctf_root}", file=sys.stderr)
        return 2
    # Duyệt sâu tối đa 5 cấp, tìm flag.txt
    for flag_file in ctf_root.rglob("flag.txt"):
        chal_dir = str(flag_file.parent.resolve())
        if chal_dir not in reg.get("solved", {}):
            flag = flag_file.read_text().strip()
            reg.setdefault("solved", {})[chal_dir] = {
                "flag": flag,
                "solved_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            added += 1
    if added:
        save_registry(reg)
    print(f"[sync] scanned {ctf_root}, added {added} new solved flags "
          f"(total {len(reg.get('solved', {}))})")
    return 0


def list_flags(json_out: bool) -> int:
    reg = load_registry()
    solved = reg.get("solved", {})
    if json_out:
        print(json.dumps(reg, indent=2, ensure_ascii=False))
        return 0
    print(f"Total solved: {len(solved)}\n")
    for path, entry in sorted(solved.items()):
        print(f"  {entry.get('solved_at', '?')}  {path}")
        print(f"      flag: {entry['flag']}")
        if entry.get("writeup"):
            print(f"      writeup: {entry['writeup']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--add", nargs=2, metavar=("CHAL_DIR", "FLAG"))
    ap.add_argument("--check", metavar="CHAL_DIR")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--writeup", metavar="PATH")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.add:
        return add_flag(args.add[0], args.add[1], args.writeup, args.force)
    if args.check:
        return check_flag(args.check)
    if args.list:
        return list_flags(json_out=False)
    if args.sync:
        return sync_from_disk()

    # --list mặc định nếu không có action (hợp lệ)
    if args.writeup or args.force:
        print("--writeup/--force chỉ dùng kèm --add", file=sys.stderr)
        return 2
    return list_flags(json_out=False)


if __name__ == "__main__":
    raise SystemExit(main())
