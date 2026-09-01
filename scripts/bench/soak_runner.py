#!/usr/bin/env python3
"""Soak / endurance harness for the webgpt gateway.

Runs parameterized endurance scenarios against a live gateway
(default http://127.0.0.1:18000) and produces a JSONL metric stream plus a
markdown verdict report under docs/reports/soak/.

Scenarios: stable | burst | recovery | leak  (see docs/guides/SOAK_TEST_PLAN.md)

Safety:
  --dry-run prints the plan and sends nothing.
  More than MAX_TURNS_WITHOUT_FLAG turns requires --i-know-this-is-long.

Stdlib only (urllib, subprocess, threading) so the harness itself stays light.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18000
PROMPT = "Reply with the single word: ok"
MODEL = "claude-3-5-sonnet"
MAX_TURNS_WITHOUT_FLAG = 500
CHROME_PROC_RE = re.compile(r"chrome|chromium|cloak", re.IGNORECASE)
SCENARIOS = ("stable", "burst", "recovery", "leak")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class TurnResult:
    index: int
    ok: bool
    status: int | None
    latency_s: float
    error: str = ""
    phase: str = "run"          # run | post-kill
    worker: int = 0

    def to_dict(self) -> dict:
        return {
            "kind": "turn",
            "index": self.index,
            "ok": self.ok,
            "status": self.status,
            "latency_s": round(self.latency_s, 3),
            "error": self.error,
            "phase": self.phase,
            "worker": self.worker,
        }


@dataclass
class Sample:
    ts: str
    gateway_rss_kb: int
    chrome_rss_kb: int

    @property
    def total_rss_kb(self) -> int:
        return self.gateway_rss_kb + self.chrome_rss_kb

    def to_dict(self) -> dict:
        return {
            "kind": "sample",
            "ts": self.ts,
            "gateway_rss_kb": self.gateway_rss_kb,
            "chrome_rss_kb": self.chrome_rss_kb,
            "total_rss_kb": self.total_rss_kb,
        }


@dataclass
class TraceSummary:
    request_count: int = 0
    corrected_requests: int = 0
    correction_count: int = 0
    max_corrections_per_request: int = 0
    malformed_lines: int = 0


@dataclass
class Thresholds:
    max_error_rate_pct: float = 2.0
    max_turn_latency_s: float = 120.0
    max_p95_latency_s: float = 90.0
    max_rss_growth_pct: float = 20.0
    require_recovered: bool = False


def trace_last_sequence(path: Path) -> int:
    """Return the highest sequence currently persisted in a trace JSONL file."""
    highest = 0
    if not path.is_file():
        return highest
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sequence = payload.get("sequence")
                if isinstance(sequence, int):
                    highest = max(highest, sequence)
    except OSError:
        return 0
    return highest


def summarize_trace(path: Path, *, after_sequence: int = 0) -> TraceSummary:
    """Summarize request-level correction telemetry emitted during this soak run."""
    summary = TraceSummary()
    if not path.is_file():
        return summary
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return summary
    with handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                summary.malformed_lines += 1
                continue
            sequence = payload.get("sequence")
            if not isinstance(sequence, int) or sequence <= after_sequence:
                continue
            if payload.get("kind") != "request_completed":
                continue
            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                continue
            corrections = metadata.get("correction_count", 0)
            if not isinstance(corrections, int) or isinstance(corrections, bool):
                corrections = 0
            corrections = max(0, corrections)
            summary.request_count += 1
            summary.correction_count += corrections
            summary.max_corrections_per_request = max(
                summary.max_corrections_per_request, corrections
            )
            if corrections:
                summary.corrected_requests += 1
    return summary


def thresholds_for(scenario: str) -> Thresholds:
    if scenario == "burst":
        return Thresholds(max_p95_latency_s=120.0, max_rss_growth_pct=25.0)
    if scenario == "recovery":
        return Thresholds(
            max_error_rate_pct=15.0,
            max_p95_latency_s=180.0,
            max_rss_growth_pct=50.0,
            require_recovered=True,
        )
    return Thresholds()


# --------------------------------------------------------------------------- #
# Pure parsing / stats helpers (unit-tested)
# --------------------------------------------------------------------------- #

def parse_pgrep_output(text: str) -> list[int]:
    """Parse `pgrep` stdout into a list of pids."""
    pids = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line.split()[0]))
        except ValueError:
            continue
    return pids


def parse_ss_pids(text: str) -> list[int]:
    """Extract process ids from `ss -lptn` output (pid=123 style)."""
    return [int(m) for m in re.findall(r"pid=(\d+)", text)]


def parse_ps_lines(text: str) -> list[tuple[int, str]]:
    """Parse `ps -o rss=,comm=` output into [(rss_kb, comm), ...]."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        try:
            rss = int(parts[0])
        except (IndexError, ValueError):
            continue
        comm = parts[1] if len(parts) == 2 else ""
        rows.append((rss, comm))
    return rows


def sum_chrome_rss(rows: list[tuple[int, str]]) -> int:
    """Sum RSS of rows whose comm matches chrome/chromium/cloak."""
    return sum(rss for rss, comm in rows if CHROME_PROC_RE.search(comm))


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank-ish interpolated percentile; 0.0 on empty input."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


def rss_growth_pct(samples: list[Sample]) -> tuple[float, float, float]:
    """Return (growth_pct, baseline_kb, final_kb).

    baseline = mean of the first ~10% samples, final = mean of last 3.
    Returns (0.0, 0.0, 0.0) when there are too few samples to judge.
    """
    totals = [s.total_rss_kb for s in samples]
    if len(totals) < 4 or min(totals) <= 0:
        return 0.0, 0.0, 0.0
    baseline_n = max(1, len(totals) // 10)
    baseline = statistics.fmean(totals[:baseline_n])
    final = statistics.fmean(totals[-3:])
    growth = (final - baseline) / baseline * 100.0
    return growth, baseline, final


def leak_tail_ratio(samples: list[Sample]) -> float:
    """mean(total RSS of last quarter) / baseline; 1.0 when unknown."""
    _growth, baseline, _ = rss_growth_pct(samples)
    totals = [s.total_rss_kb for s in samples]
    if baseline <= 0 or len(totals) < 8:
        return 1.0
    quarter_n = max(1, len(totals) // 4)
    tail = statistics.fmean(totals[-quarter_n:])
    return tail / baseline


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #

@dataclass
class Verdict:
    passed: bool
    checks: list[dict] = field(default_factory=list)

    def to_rows(self) -> list[tuple[str, str, str, str]]:
        return [
            (
                c["name"],
                f"{c['value']:.2f}" if isinstance(c["value"], float) else str(c["value"]),
                f"{c['threshold']:.2f}" if isinstance(c["threshold"], float) else str(c["threshold"]),
                "PASS" if c["ok"] else "FAIL",
            )
            for c in self.checks
        ]


def compute_verdict(
    scenario: str,
    turns: list[TurnResult],
    samples: list[Sample],
    recovered: bool | None = None,
    thresholds: Thresholds | None = None,
) -> Verdict:
    th = thresholds or thresholds_for(scenario)
    checks: list[dict] = []

    total = len(turns)
    errors = sum(1 for t in turns if not t.ok)
    error_rate = (errors / total * 100.0) if total else 0.0
    latencies = [t.latency_s for t in turns]
    p95 = percentile(latencies, 95)
    max_lat = max(latencies, default=0.0)
    growth, _, _ = rss_growth_pct(samples)

    checks.append({
        "name": "error_rate_pct", "value": round(error_rate, 2),
        "threshold": th.max_error_rate_pct, "ok": error_rate < th.max_error_rate_pct,
    })
    checks.append({
        "name": "max_turn_latency_s", "value": round(max_lat, 2),
        "threshold": th.max_turn_latency_s, "ok": bool(turns) and max_lat < th.max_turn_latency_s,
    })
    checks.append({
        "name": "p95_latency_s", "value": round(p95, 2),
        "threshold": th.max_p95_latency_s, "ok": bool(turns) and p95 <= th.max_p95_latency_s,
    })
    if len(samples) >= 4:
        checks.append({
            "name": "rss_growth_pct", "value": round(growth, 2),
            "threshold": th.max_rss_growth_pct, "ok": growth < th.max_rss_growth_pct,
        })

    if th.require_recovered:
        checks.append({
            "name": "recovered_after_kill", "value": bool(recovered),
            "threshold": True, "ok": bool(recovered),
        })

    return Verdict(passed=all(c["ok"] for c in checks), checks=checks)


# --------------------------------------------------------------------------- #
# Markdown report
# --------------------------------------------------------------------------- #

def render_markdown(
    *,
    scenario: str,
    target: str,
    turns: list[TurnResult],
    samples: list[Sample],
    verdict: Verdict,
    events: list[str],
    started_at: str,
    finished_at: str,
    interval_s: float,
    concurrency: int,
    killed_pid: int | None = None,
    trace_summary: TraceSummary | None = None,
) -> str:
    latencies = [t.latency_s for t in turns]
    ok_n = sum(1 for t in turns if t.ok)
    err_n = len(turns) - ok_n
    growth, baseline, final = rss_growth_pct(samples)
    peak = max((s.total_rss_kb for s in samples), default=0)
    lines: list[str] = []
    lines.append(f"# Soak Report: {scenario}")
    lines.append("")
    lines.append(f"- Generated: {finished_at}")
    lines.append(f"- Window: {started_at} -> {finished_at}")
    lines.append(f"- Target: `{target}`")
    lines.append(
        f"- Turns: {len(turns)} (ok {ok_n} / err {err_n}) · "
        f"concurrency {concurrency} · interval {interval_s:g}s · samples {len(samples)}"
    )
    lines.append("")
    lines.append("## Latency")
    lines.append("")
    lines.append("| metric | seconds |")
    lines.append("|---|---|")
    lines.append(f"| p50 | {percentile(latencies, 50):.2f} |")
    lines.append(f"| p95 | {percentile(latencies, 95):.2f} |")
    lines.append(f"| mean | {statistics.fmean(latencies):.2f} |" if latencies else "| mean | n/a |")
    lines.append(f"| max | {max(latencies, default=0.0):.2f} |")
    lines.append("")
    lines.append("## Memory (gateway + chrome children)")
    lines.append("")
    lines.append("| metric | KB |")
    lines.append("|---|---|")
    lines.append(f"| baseline | {baseline:.0f} |")
    lines.append(f"| final | {final:.0f} |")
    lines.append(f"| peak total | {peak} |")
    lines.append(f"| growth | {growth:.2f}% |")
    lines.append("")
    if scenario == "recovery":
        pre = [t for t in turns if t.phase == "run"]
        post = [t for t in turns if t.phase == "post-kill"]
        rec = any(t.ok for t in post[-max(1, len(post) // 4):]) if post else False
        lines.append("## Recovery")
        lines.append("")
        lines.append(f"- Killed chrome child pid: {killed_pid if killed_pid else 'n/a'}")
        lines.append(f"- Pre-kill turns/errors: {len(pre)}/{sum(1 for t in pre if not t.ok)}")
        lines.append(f"- Post-kill turns/errors: {len(post)}/{sum(1 for t in post if not t.ok)}")
        lines.append(f"- Recovered: {'yes' if rec else 'no'}")
        lines.append("")
    if trace_summary is not None:
        lines.append("## Gateway correction telemetry")
        lines.append("")
        lines.append(f"- Completed requests observed: {trace_summary.request_count}")
        lines.append(f"- Requests requiring correction: {trace_summary.corrected_requests}")
        lines.append(f"- Corrections sent: {trace_summary.correction_count}")
        lines.append(
            f"- Max corrections in one request: {trace_summary.max_corrections_per_request}"
        )
        if trace_summary.malformed_lines:
            lines.append(f"- Malformed trace lines skipped: {trace_summary.malformed_lines}")
        lines.append("")
    if events:
        lines.append("## Events")
        lines.append("")
        for ev in events:
            lines.append(f"- {ev}")
        lines.append("")
    lines.append(f"## Verdict: {'PASS' if verdict.passed else 'FAIL'}")
    lines.append("")
    lines.append("| check | value | threshold | result |")
    lines.append("|---|---|---|---|")
    for name, value, threshold, result in verdict.to_rows():
        lines.append(f"| {name} | {value} | {threshold} | {result} |")
    lines.append("")
    lines.append("## Per-turn detail")
    lines.append("")
    detail = turns if len(turns) <= 40 else turns[:20] + turns[-20:]
    if len(turns) > 40:
        lines.append(f"(showing first 20 and last 20 of {len(turns)} turns)")
        lines.append("")
    lines.append("| turn | phase | worker | status | latency_s | ok |")
    lines.append("|---|---|---|---|---|---|")
    for t in detail:
        lines.append(
            f"| {t.index} | {t.phase} | {t.worker} | "
            f"{t.status if t.status is not None else '-'} | {t.latency_s:.2f} | "
            f"{'yes' if t.ok else 'no'} |"
        )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Process discovery & RSS sampling
# --------------------------------------------------------------------------- #

def find_gateway_pid(port: int, run=subprocess.run) -> tuple[int | None, str]:
    """Locate the gateway pid listening on `port`.

    Order: ss -> lsof -> pgrep -f 'gpt.debug api-server'. Returns (pid|None, source).
    """
    try:
        proc = run(["ss", "-lptn", "sport", "=", f":{port}"],
                   capture_output=True, text=True, timeout=15)
        pids = parse_ss_pids(proc.stdout)
        if pids:
            return pids[0], "ss"
    except Exception:
        pass
    try:
        proc = run(["lsof", "-ti", f"tcp:{port}"],
                   capture_output=True, text=True, timeout=15)
        pids = parse_pgrep_output(proc.stdout)
        if pids:
            return pids[0], "lsof"
    except Exception:
        pass
    try:
        proc = run(["pgrep", "-f", "gpt.debug api-server"],
                   capture_output=True, text=True, timeout=15)
        pids = parse_pgrep_output(proc.stdout)
        if pids:
            return pids[0], "pgrep"
    except Exception:
        pass
    return None, "none"


def collect_tree_children(root_pid: int, run=subprocess.run, max_depth: int = 3) -> list[int]:
    """Collect descendant pids of root via repeated pgrep -P (breadth-first)."""
    found: list[int] = []
    frontier = [root_pid]
    for _ in range(max_depth):
        nxt: list[int] = []
        for pid in frontier:
            try:
                proc = run(["pgrep", "-P", str(pid)],
                           capture_output=True, text=True, timeout=15)
            except Exception:
                continue
            kids = parse_pgrep_output(proc.stdout)
            found.extend(kids)
            nxt.extend(kids)
        frontier = nxt
        if not frontier:
            break
    return found


def read_process_rss(pids: list[int], run=subprocess.run) -> list[tuple[int, str]]:
    if not pids:
        return []
    try:
        proc = run(["ps", "-o", "rss=,comm=", "-p", ",".join(str(p) for p in pids)],
                   capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    return parse_ps_lines(proc.stdout)


def take_sample(gateway_pid: int, run=subprocess.run) -> Sample:
    gw_rows = read_process_rss([gateway_pid], run)
    gw_rss = sum(rss for rss, _comm in gw_rows)
    children = collect_tree_children(gateway_pid, run)
    chrome_rss = sum_chrome_rss(read_process_rss(children, run))
    return Sample(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        gateway_rss_kb=gw_rss,
        chrome_rss_kb=chrome_rss,
    )


class MetricSampler(threading.Thread):
    """Light background thread sampling RSS every `interval_s` seconds."""

    def __init__(self, gateway_pid: int, interval_s: float, sink: list[Sample],
                 jsonl_path: Path | None, stop_event: threading.Event,
                 run=subprocess.run):
        super().__init__(daemon=True, name="rss-sampler")
        self.gateway_pid = gateway_pid
        self.interval_s = interval_s
        self.sink = sink
        self.jsonl_path = jsonl_path
        self.stop_event = stop_event
        self._run = run

    def run(self) -> None:
        while not self.stop_event.wait(self.interval_s):
            try:
                sample = take_sample(self.gateway_pid, self._run)
            except Exception as exc:  # never kill the soak over a sampler hiccup
                print(f"[sampler] warn: {exc}", flush=True)
                continue
            self.sink.append(sample)
            append_jsonl(self.jsonl_path, sample.to_dict())


# --------------------------------------------------------------------------- #
# HTTP client (injectable for tests)
# --------------------------------------------------------------------------- #

class HttpClient:
    """Minimal stdlib POST client."""

    def __init__(self, timeout_s: float = 150.0):
        self.timeout_s = timeout_s

    def post_json(self, url: str, payload: dict) -> tuple[int | None, str]:
        data = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlrequest.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            return exc.code, body
        except URLError as exc:
            return None, str(exc.reason)
        except TimeoutError:
            return None, "client timeout"


def chat_payload() -> dict:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 16,
        "stream": False,
    }


def run_turn(client: HttpClient, base_url: str, index: int,
             phase: str = "run", worker: int = 0) -> TurnResult:
    url = f"{base_url}/v1/chat/completions"
    start = time.monotonic()
    try:
        status, _body = client.post_json(url, chat_payload())
    except Exception as exc:  # defensive: any client bug counts as failed turn
        return TurnResult(index=index, ok=False, status=None,
                          latency_s=time.monotonic() - start,
                          error=f"{type(exc).__name__}: {exc}",
                          phase=phase, worker=worker)
    latency = time.monotonic() - start
    ok = status == 200
    return TurnResult(index=index, ok=ok, status=status, latency_s=latency,
                      error="" if ok else f"HTTP {status}", phase=phase, worker=worker)


def append_jsonl(path: Path | None, record: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Scenario drivers
# --------------------------------------------------------------------------- #

def run_sequential(client, base_url, total, interval_s, log, phase="run",
                   start_index=0, jsonl_path=None, kill_at_index=None,
                   gateway_pid=None):
    turns: list[TurnResult] = []
    events: list[str] = []
    killed_pid = None
    for i in range(start_index, start_index + total):
        turn = run_turn(client, base_url, index=i, phase=phase)
        turns.append(turn)
        append_jsonl(jsonl_path, turn.to_dict())
        log(f"[turn {i}] status={turn.status} latency={turn.latency_s:.2f}s ok={turn.ok}")
        if kill_at_index is not None and i == kill_at_index and gateway_pid:
            killed_pid = kill_one_chrome_child(gateway_pid)
            msg = (f"killed chrome child pid {killed_pid}" if killed_pid
                   else "no chrome child found to kill")
            events.append(msg)
            log(f"[event] {msg}")
        if i != start_index + total - 1:
            time.sleep(interval_s)
    return turns, events, killed_pid


def kill_one_chrome_child(gateway_pid: int, run=subprocess.run) -> int | None:
    """SIGKILL one chrome-descendant process of the gateway."""
    candidates = [
        pid for pid, comm in read_process_rss(collect_tree_children(gateway_pid, run), run)
        if CHROME_PROC_RE.search(comm)
    ]
    # fall back to direct descendants if names did not match
    if not candidates:
        candidates = collect_tree_children(gateway_pid, run)
    if not candidates:
        return None
    victim = candidates[-1]
    try:
        os.kill(victim, signal.SIGKILL)
        return victim
    except OSError as exc:
        print(f"[kill] warn: pid {victim}: {exc}", flush=True)
        return None


def run_burst(client, base_url, total, interval_s, concurrency, log, jsonl_path=None):
    turns: list[TurnResult] = []
    sent = 0
    wave = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        while sent < total:
            size = min(concurrency, total - sent)
            futures = [
                pool.submit(run_turn, client, base_url, sent + k, "run", k)
                for k in range(size)
            ]
            for fut in futures:
                turn = fut.result()
                turns.append(turn)
                append_jsonl(jsonl_path, turn.to_dict())
                log(f"[wave {wave} turn {turn.index}] status={turn.status} "
                    f"latency={turn.latency_s:.2f}s ok={turn.ok}")
            sent += size
            wave += 1
            if sent < total:
                time.sleep(interval_s)
    turns.sort(key=lambda t: t.index)
    return turns


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_plan(args: argparse.Namespace) -> dict:
    thresholds = vars(thresholds_for(args.scenario))
    thresholds.update(
        max_error_rate_pct=args.max_error_rate_pct,
        max_turn_latency_s=args.max_turn_latency_s,
        max_p95_latency_s=args.max_p95_latency_s,
        max_rss_growth_pct=args.max_rss_growth_pct,
    )
    return {
        "mode": "DRY-RUN (nothing will be sent)" if args.dry_run else "live",
        "scenario": args.scenario,
        "target": f"http://{args.host}:{args.port}",
        "turns": args.turns,
        "interval_s": args.interval,
        "concurrency": args.concurrency if args.scenario == "burst" else 1,
        "rss_interval_s": args.rss_interval,
        "recovery_kill_at_turn": (
            args.turns // 2 if args.scenario == "recovery" and args.turns >= 4 else None
        ),
        "report_dir": str(args.report_dir),
        "jsonl": str(args.jsonl) if args.jsonl else "(auto: alongside report)",
        "long_run_flag_used": args.i_know_this_is_long,
        "would_refuse_without_long_flag": args.turns > MAX_TURNS_WITHOUT_FLAG
        and not args.i_know_this_is_long,
        "thresholds": thresholds,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="webgpt gateway soak harness")
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    parser.add_argument("--turns", type=int, required=True)
    parser.add_argument("--interval", type=float, default=10.0,
                        help="sleep between turns/waves in seconds")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--concurrency", type=int, default=4,
                        help="parallel requests per wave (burst only)")
    parser.add_argument("--rss-interval", type=float, default=30.0,
                        help="RSS sampling period in seconds")
    parser.add_argument("--report-dir", type=Path,
                        default=REPO_ROOT / "docs" / "reports" / "soak")
    parser.add_argument("--jsonl", type=Path, default=None,
                        help="metric JSONL path (default: alongside the report)")
    parser.add_argument("--trace-file", type=Path, default=None,
                        help="gateway trace.jsonl; summarize new request correction telemetry")
    parser.add_argument("--timeout", type=float, default=150.0,
                        help="per-request timeout in seconds")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and send nothing")
    parser.add_argument("--i-know-this-is-long", action="store_true",
                        help=f"required for more than {MAX_TURNS_WITHOUT_FLAG} turns")
    parser.add_argument("--max-error-rate-pct", type=float, default=None)
    parser.add_argument("--max-turn-latency-s", type=float, default=None)
    parser.add_argument("--max-p95-latency-s", type=float, default=None)
    parser.add_argument("--max-rss-growth-pct", type=float, default=None)
    args = parser.parse_args(argv)
    defaults = thresholds_for(args.scenario)
    if args.max_error_rate_pct is None:
        args.max_error_rate_pct = defaults.max_error_rate_pct
    if args.max_turn_latency_s is None:
        args.max_turn_latency_s = defaults.max_turn_latency_s
    if args.max_p95_latency_s is None:
        args.max_p95_latency_s = defaults.max_p95_latency_s
    if args.max_rss_growth_pct is None:
        args.max_rss_growth_pct = defaults.max_rss_growth_pct
    return args


def main(argv: list[str] | None = None, client: HttpClient | None = None) -> int:
    args = parse_args(argv)

    if args.turns < 1:
        print("error: --turns must be >= 1", file=sys.stderr)
        return 2
    if args.turns > MAX_TURNS_WITHOUT_FLAG and not args.i_know_this_is_long:
        print(
            f"error: refusing to run {args.turns} turns without --i-know-this-is-long",
            file=sys.stderr,
        )
        return 2

    plan = build_plan(args)
    print(json.dumps(plan, indent=2, ensure_ascii=False))

    if args.dry_run:
        print("[dry-run] no request sent, no metric collected.")
        return 0

    base_url = f"http://{args.host}:{args.port}"
    client = client or HttpClient(timeout_s=args.timeout)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_dir = args.report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"soak-{args.scenario}-{timestamp}.md"
    jsonl_path = args.jsonl or report_dir / f"soak-{args.scenario}-{timestamp}.jsonl"

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    trace_start_sequence = trace_last_sequence(args.trace_file) if args.trace_file else 0
    events: list[str] = []
    killed_pid: int | None = None

    def log(msg: str) -> None:
        print(msg, flush=True)

    # locate gateway process for RSS sampling
    gateway_pid, source = find_gateway_pid(args.port)
    if gateway_pid is None:
        events.append(f"gateway pid not found on port {args.port}; RSS sampling disabled")
        log(f"[warn] {events[-1]}")
        sampler = None
        samples: list[Sample] = []
        stop_event = threading.Event()
    else:
        events.append(f"gateway pid {gateway_pid} found via {source}")
        log(f"[info] {events[-1]}")
        samples = []
        stop_event = threading.Event()
        sampler = MetricSampler(gateway_pid, args.rss_interval, samples,
                                jsonl_path, stop_event)
        sampler.start()

    try:
        if args.scenario == "burst":
            turns = run_burst(client, base_url, args.turns, args.interval,
                              args.concurrency, log, jsonl_path=jsonl_path)
        elif args.scenario == "recovery":
            if args.turns < 4:
                print("error: recovery needs --turns >= 4", file=sys.stderr)
                return 2
            half = args.turns // 2
            first, kill_events, killed_pid = run_sequential(
                client, base_url, half, args.interval, log,
                jsonl_path=jsonl_path, gateway_pid=gateway_pid,
                kill_at_index=half - 1,
            )
            events.extend(kill_events)
            second, _, _ = run_sequential(
                client, base_url, args.turns - half, args.interval, log,
                phase="post-kill", start_index=half, jsonl_path=jsonl_path,
            )
            turns = first + second
            post = [t for t in second]
            recovered_flag = any(t.ok for t in post[-max(1, len(post) // 4):])
        else:  # stable | leak
            turns, _, _ = run_sequential(client, base_url, args.turns,
                                         args.interval, log, jsonl_path=jsonl_path)
            recovered_flag = None
    finally:
        if sampler is not None:
            stop_event.set()
            sampler.join(timeout=args.rss_interval + 5.0)

    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # health probe after the run (extra signal, does not affect verdict)
    health_ok = None
    try:
        import urllib.request as _ur
        with _ur.urlopen(f"{base_url}/health", timeout=10) as resp:
            health_ok = resp.status == 200
    except Exception:
        health_ok = False
    events.append(f"post-run /health ok={health_ok}")

    verdict = compute_verdict(
        args.scenario, turns, samples, recovered=recovered_flag,
        thresholds=Thresholds(
            max_error_rate_pct=args.max_error_rate_pct,
            max_turn_latency_s=args.max_turn_latency_s,
            max_p95_latency_s=args.max_p95_latency_s,
            max_rss_growth_pct=args.max_rss_growth_pct,
            require_recovered=(args.scenario == "recovery"),
        ),
    )

    trace_summary = (
        summarize_trace(args.trace_file, after_sequence=trace_start_sequence)
        if args.trace_file
        else None
    )
    markdown = render_markdown(
        scenario=args.scenario, target=base_url, turns=turns, samples=samples,
        verdict=verdict, events=events, started_at=started_at,
        finished_at=finished_at, interval_s=args.interval,
        concurrency=args.concurrency if args.scenario == "burst" else 1,
        killed_pid=killed_pid, trace_summary=trace_summary,
    )
    report_path.write_text(markdown, encoding="utf-8")
    log(f"[done] report: {report_path}")
    log(f"[done] metrics: {jsonl_path}")
    log(f"[done] verdict: {'PASS' if verdict.passed else 'FAIL'}")
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
