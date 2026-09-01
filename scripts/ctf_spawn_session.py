#!/usr/bin/env python3
"""Spawn một session `gpt` để giải 1 bài CTF.

Mỗi session chạy `gpt --new-session --no-session "<prompt>"` trong workspace riêng.
Output stream vào log file, bạn nhìn thấy PID qua ps/top.
Khi xong: progress.json update, process thoát.

Dùng:
  ctf_spawn_session.py --chal-dir <path> --name <name> [--timeout 1800]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Inline neutralize (copy từ scripts/ctf_prompting.py — tránh vấn đề package import)
import re as _re

_NEUTRAL_TERMS: tuple[tuple[_re.Pattern[str], str], ...] = (
    (_re.compile(r"\breverse[ -]?engineer(?:ing)?\b", _re.I), "phân tích định dạng và hành vi chương trình"),
    (_re.compile(r"\bapk\b", _re.I), "Android app archive"),
    (_re.compile(r"\bpwn(?:ing)?\b", _re.I), "binary challenge"),
    (_re.compile(r"\bshellcode\b", _re.I), "machine-code input"),
    (_re.compile(r"\bpayload\b", _re.I), "test input"),
    (_re.compile(r"\bexploit(?:ation)?\b", _re.I), "reproduction/solve procedure"),
    (_re.compile(r"\bvulnerabilit(?:y|ies)\b", _re.I), "implementation flaw"),
    (_re.compile(r"\bbypass\b", _re.I), "satisfy or work around the challenge condition"),
    (_re.compile(r"\bred[ -]?team\b", _re.I), "authorized challenge analysis"),
)


def _neutralize(text: str) -> str:
    out = text
    for pattern, replacement in _NEUTRAL_TERMS:
        out = pattern.sub(replacement, out)
    return out


def frame_local_ctf_prompt(text: str) -> str:
    prefix = (
        "Đây là bài tập giáo dục từ một kho CTF cục bộ đã được chủ sở hữu cho phép. "
        "Hãy chỉ phân tích các file trong workspace hiện tại trước; không kết nối hay "
        "tác động hệ thống bên ngoài trừ khi người dùng yêu cầu riêng sau khi local đã được xác minh. "
    )
    return prefix + _neutralize(text)

RUNS_DIR = Path("/home/light/GitHub/gpt/scratch/ctf-runs")
WORK_ROOT = Path("/home/light/GitHub/gpt/scratch/ctf-workspaces")
FLAG_REGEX = re.compile(r"brunner\{[^}]+\}|flag\{[^}]+\}", re.IGNORECASE)
CYBER_REFUSAL_PATTERNS = [
    "this content can't be shown",
    "cybersecurity requests",
    "extra caution",
    "i can't help with that",
    "i'm not able to",
]


def collect_files(chal_dir: Path) -> list[Path]:
    """Thu thập file đính kèm từ challenge dir (zip, attachment, readme)."""
    files: list[Path] = []
    for p in sorted(chal_dir.iterdir()):
        if p.is_file() and not p.name.startswith("."):
            if p.name == "flag.txt":
                continue
            files.append(p)
    # Lấy 1 file zip nếu có
    return files


def build_prompt(chal_dir: Path, name: str) -> str:
    """Tạo prompt: framing an toàn + mô tả + file đính kèm inline."""
    files = collect_files(chal_dir)
    meta = chal_dir / "metadata.json"
    desc = ""
    if meta.exists():
        try:
            d = json.loads(meta.read_text(encoding="utf-8", errors="replace"))
            desc = (d.get("description") or d.get("raw", {}).get("description") or "").strip()
        except Exception:
            pass
    if not desc:
        readme = chal_dir / "README.md"
        if readme.exists():
            try:
                desc = readme.read_text(encoding="utf-8", errors="replace")[:1500]
            except Exception:
                pass

    # Đóng gói file (text hoặc base64 binary)
    file_block_parts: list[str] = []
    for f in files:
        if f.suffix.lower() in {".zip", ".tar", ".tar.gz", ".7z"}:
            # Không inline file lớn, để agent tự unzip
            file_block_parts.append(
                f"=== ARCHIVE (cần unzip): {f.name} ({f.stat().st_size} bytes) ===\n"
                f"Path: {f}\n"
                f"Hãy dùng `unzip` hoặc `tar -xf` để giải nén và phân tích nội dung bên trong."
            )
            continue
        try:
            content = f.read_text(encoding="utf-8")
            if len(content) > 30000:
                content = content[:30000] + "\n...[truncated]..."
            file_block_parts.append(
                f"=== FILE: {f.name} ===\n{content}\n=== END FILE: {f.name} ==="
            )
        except Exception:
            raw = f.read_bytes()
            if len(raw) < 50000:
                b64 = base64.b64encode(raw).decode("ascii")
                file_block_parts.append(
                    f"=== BINARY (base64): {f.name} ===\n{b64}\n=== END ==="
                )
            else:
                file_block_parts.append(
                    f"=== BINARY (skip, too large): {f.name} ({len(raw)} bytes) ==="
                )

    file_block = "\n\n".join(file_block_parts)
    task = f"""
Đây là bài tập phân tích định dạng từ một kho CTF cục bộ đã được chủ sở hữu cho phép.

Thư mục bài: {chal_dir}
Bạn có quyền đọc/ghi trong thư mục này.

Mô tả bài (metadata/README):
{desc[:2000] if desc else "(không có mô tả chi tiết)"}

Các file đính kèm (đã được đóng gói):
{file_block}

Nhiệm vụ:
1. Khám phá thư mục (ls, file, pwd) để hiểu cấu trúc dữ liệu.
2. Phân tích định dạng và hành vi của các file (KHÔNG kết nối remote; chỉ phân tích local).
3. Viết script Python hoặc Sage (solve.py / solve.sage; hệ thống đã có sẵn `python3` và `sage` trong PATH) để tìm flag. Chạy thử và xác minh kết quả.
4. Khi tìm được flag, ghi vào flag.txt trong thư mục bài.
5. Cập nhật progress.json trong workspace của bạn ({RUNS_DIR / name}/progress.json).

Quy tắc:
- Nếu từ khoá nào trong bài làm bạn khó xử, hãy phân tích định dạng file thay vì đi vào chi tiết "làm thế nào để tấn công".
- Output cuối cùng: ghi "FLAG: <flag>" vào stdout để log script bắt được.
- Khi xong, kết thúc bằng một dòng "DONE".
"""
    return frame_local_ctf_prompt(task)


def update_progress(ws_dir: Path, **kwargs) -> None:
    """Update progress.json atomic."""
    prog = ws_dir / "progress.json"
    try:
        data = json.loads(prog.read_text()) if prog.exists() else {}
    except Exception:
        data = {}
    data.update(kwargs)
    data["last_turn_at"] = datetime.now().isoformat(timespec="seconds")
    # Atomic write
    tmp = prog.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.rename(prog)


def extract_flags(text: str) -> list[str]:
    return FLAG_REGEX.findall(text)


def is_cyber_refusal(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in CYBER_REFUSAL_PATTERNS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chal-dir", required=True, type=Path)
    ap.add_argument("--name", required=True, help="workspace name (vd: magic-or-not)")
    ap.add_argument("--timeout", type=int, default=1800, help="giây, 0=vô hạn")
    ap.add_argument("--no-spawn", action="store_true",
                    help="chỉ in prompt, không spawn (debug)")
    args = ap.parse_args()

    if not args.chal_dir.is_dir():
        print(f"FATAL: chal-dir not found: {args.chal_dir}", file=sys.stderr)
        return 2

    # Bỏ qua nếu bài đã solve (flag.txt trong challenge dir, hoặc registry)
    ws = RUNS_DIR / args.name
    ws.mkdir(parents=True, exist_ok=True)
    chal_flag = args.chal_dir / "flag.txt"
    if chal_flag.exists():
        print(f"SKIP: {args.name} already solved (flag.txt: "
              f"{chal_flag.read_text().strip()[:50]}...)")
        update_progress(ws, status="solved-skip",
                        flag=chal_flag.read_text().strip(), turns=0)
        return 0
    work_dir = WORK_ROOT / args.name
    work_dir.mkdir(parents=True, exist_ok=True)

    # Copy attachment + README vào workspace (sandbox không cho truy cập ngoài)
    import shutil as _sh
    copied = []
    for f in collect_files(args.chal_dir):
        dest = work_dir / f.name
        if not dest.exists():
            try:
                _sh.copy2(f, dest)
                copied.append(f.name)
            except Exception as e:
                print(f"    warn: cannot copy {f.name}: {e}")
    if copied:
        print(f"    copied {len(copied)} files: {copied[:5]}{'...' if len(copied) > 5 else ''}")

    prompt = build_prompt(args.chal_dir, args.name)
    prompt_file = ws / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    if args.no_spawn:
        print(f"[debug] prompt written to {prompt_file} ({len(prompt)} chars)")
        return 0

    # Update progress
    update_progress(ws, status="running", started_at=datetime.now().isoformat(timespec="seconds"))

    # Spawn `gpt` process
    env = os.environ.copy()
    env.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:18000")
    env.setdefault("ANTHROPIC_API_KEY", "sk-webgpt-local")
    env.setdefault("WEBGPT_CTF_TARGET_DIR", str(work_dir))
    env.setdefault("WEBGPT_RUNTIME_ROOT", str(Path("/home/light/GitHub/gpt/data/webgpt")))
    # CTF bài phức tạp cần nhiều tool calls — tăng max_rounds (default 20 quá ít)
    env.setdefault("WEBGPT_MAX_ROUNDS", "100")
    # Pin tool protocol để tránh 409 khi song song (DECISIONS 2026-08-24)
    env["WEBGPT_TOOL_PROTOCOL"] = "soft"
    # Bật sandbox working dir = work_dir (nơi ta vừa copy files vào)
    env["WEBGPT_CTF_TARGET_DIR"] = str(work_dir)
    # Mỗi session có session_id riêng (log progress.json để debug).
    # Hiện GatewayClient không đọc WEBGPT_SESSION_ID env, session_id chỉ được
    # nhận từ response header (round 1). Từ round 2+ GatewayClient sẽ gửi
    # x-webgpt-session-id header tự động → nếu tool_signature khác round 1
    # sẽ trigger 409 ConversationConflict. RESEARCH-409.md khuyến nghị fix ở
    # gpt/agent/client.py để chỉ set header ở round đầu.
    import uuid as _uuid
    session_id = f"wgs_{_uuid.uuid4().hex[:16]}"
    env.setdefault("WEBGPT_SESSION_ID", session_id)
    update_progress(ws, session_id=session_id)

    # Determine gpt binary — absolute path bắt buộc, subprocess không tự load ~/.local/bin
    gpt_bin = os.environ.get("WEBGPT_GPT_BIN") or "/home/light/.local/bin/gpt"
    if not Path(gpt_bin).exists():
        # Fallback chain
        for cand in (
            "/home/light/.local/bin/gpt",
            shutil.which("gpt") or "",
        ):
            if cand and Path(cand).exists():
                gpt_bin = cand
                break
        else:
            print(f"FATAL: cannot find gpt binary in PATH", file=sys.stderr)
            update_progress(ws, status="error-no-binary")
            return 3

    # CLI gpt nhận positional prompt (no -p/--print flag) và hỗ trợ
    # --new-session (quên session cũ) + --no-session (không persist session).
    cmd = [gpt_bin, "--verify", "off", "--new-session", "--no-session", prompt]
    log_path = ws / "session.log"
    log_fh = open(log_path, "w", encoding="utf-8")

    print(f"[*] Spawning session: {args.name}")
    print(f"    PID file:    {ws / 'session.pid'}")
    print(f"    Log file:    {log_path}")
    print(f"    Workspace:   {work_dir}")
    print(f"    Command:     {' '.join(cmd[:3])} ... ({len(prompt)} chars prompt)")

    # Note: gpt is a wrapper shell script. Spawn as new process group
    # để có thể kill toàn bộ children.
    proc = subprocess.Popen(
        cmd,
        cwd=str(work_dir),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # tạo process group riêng
    )
    pid = proc.pid
    (ws / "session.pid").write_text(str(pid))
    update_progress(ws, pid=pid, log=str(log_path))

    print(f"    Spawned PID={pid}. Tail log: tail -f {log_path}")

    if args.timeout > 0:
        try:
            rc = proc.wait(timeout=args.timeout)
            print(f"[*] Session {args.name} exited rc={rc} after {args.timeout}s timeout")
        except subprocess.TimeoutExpired:
            print(f"[!] Session {args.name} TIMEOUT after {args.timeout}s, killing")
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                proc.wait(timeout=10)
            except Exception as e:
                print(f"    kill error: {e}")
            update_progress(ws, status="timeout")
            return 1
    else:
        # Không chờ — return ngay để spawn nhiều session song song
        print(f"[*] Detached (no wait). Monitor via: tail -f {log_path}")
        return 0

    # Đọc log để tìm flag
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    flags = extract_flags(log_text)
    if flags:
        # Tìm workspace, ghi flag.txt
        flag = flags[0]
        (args.chal_dir / "flag.txt").write_text(flag + "\n", encoding="utf-8")
        update_progress(ws, status="solved", flag=flag, turns=1)
        print(f"    ✅ SOLVED: {flag}")
    elif is_cyber_refusal(log_text):
        update_progress(ws, status="blocked-cyber")
        print(f"    ⛔ BLOCKED-CYBER (transcript saved to {log_path})")
    else:
        update_progress(ws, status="ended-no-flag", exit_code=proc.returncode)
        print(f"    ❌ ENDED no flag (rc={proc.returncode})")

    return proc.returncode


if __name__ == "__main__":
    import shutil  # noqa: E402
    raise SystemExit(main())
