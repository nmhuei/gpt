#!/usr/bin/env python3
"""SOAK-LITE-MOCK: in-process mock soak proving today's reliability stack.

Runs the REAL reliability components -- ChatGPTWorkerFactory, the
ChatGPTWebSession state machine (worker-poison guard), the global rate-limit
circuit breaker, and the history-cache cap -- against fake UI drivers.
No browser, no network, no gateway on :18000 is touched.

Turn mix (wall-clock scheduled):
  - normal success turns at --rate rps;
  - RateLimited injections every --rl-spacing s -> breaker trip ->
    open -> half-open probe -> record_success closes (recovery cycle);
  - mid-send cancels every --cancel-spacing s -> BaseException poison-guard
    path must land FATAL_ERROR and the worker must never be repooled;
  - near-cap prompts (--big-chars) every --big-spacing s -> history-cache
    eviction keeps session memory bounded.

Measured: process RSS via `ps` (start/mid/end + periodic JSONL), breaker state
transitions (0.05 s poll), events dropped by _emit's 1000-event queue cap,
history evictions, factory stats (stuck-worker detection).

Verdict PASS iff:
  - |RSS growth| < 15% and max RSS excursion < 15% of baseline;
  - no stuck workers (no lease held > --stuck-lease-s; zero in-flight turns
    left after grace; final leased_workers == 0);
  - every breaker trip reached half-open AND fully recovered to closed;
  - zero unexpected turn errors.

Stdlib + repo package only. Output under $WEBGPT_RUNTIME_ROOT/soak-lite/
(default ~/.local/share/webgpt/soak-lite) — never ~/Downloads.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

COOLDOWN_ENV = "WEBGPT_RATELIMIT_COOLDOWN_SECONDS"
STAMP = ""
RateLimited: Any = None
SessionState: Any = None
TurnResult: Any = None
ResponseStarted: Any = None
ResponseDelta: Any = None
ResponseCompleted: Any = None
BackendCoolingDown: Any = None


# --------------------------------------------------------------------------- #
# Fake drivers / sessions (patterns from tests/test_worker_poison.py)
# --------------------------------------------------------------------------- #

class FakeManager:
    connected = True

    def __init__(self):
        self.stop = AsyncMock()

    async def new_page(self):  # pragma: no cover
        raise AssertionError("page recovery was not expected")


def make_page():
    page = MagicMock()
    page.url = "https://chatgpt.com/"
    page.is_closed.return_value = False
    page.close = AsyncMock()
    return page


class SuccessUI:
    """Completes a normal turn through the real event pipeline."""

    def __init__(self):
        self.sent = 0

    async def send(self, request, event_callback=None):
        self.sent += 1
        await event_callback(ResponseStarted(turn_id=f"assistant-{self.sent}"))
        await event_callback(
            ResponseCompleted(
                turn_id=f"assistant-{self.sent}", text="done",
                conversation_id=f"conv-{self.sent}",
            )
        )
        return TurnResult(
            turn_id=f"assistant-{self.sent}",
            conversation_id=f"conv-{self.sent}",
            text="done",
        )

    def conversation_id(self):
        return "conv-ok"

    async def history(self):
        return []


class RateLimitedUI:
    """Backend answers 429: exercises the breaker trip path."""

    async def send(self, request, event_callback=None):
        raise RateLimited("mock 429 free quota reached")

    def conversation_id(self):
        return "conv-rl"

    async def history(self):
        return []


class HangingUI:
    """Reaches GENERATING then hangs forever; harness cancels mid-send."""

    def __init__(self):
        self.entered = asyncio.Event()

    async def send(self, request, event_callback=None):
        self.entered.set()
        await event_callback(ResponseStarted(turn_id="assistant-hang"))
        await event_callback(ResponseDelta(text="par", accumulated_text="par"))
        await asyncio.Event().wait()  # cancelled here
        raise AssertionError("unreachable")  # pragma: no cover

    def conversation_id(self):
        return "conv-hang"

    async def history(self):
        return []


# --------------------------------------------------------------------------- #
# Soak harness
# --------------------------------------------------------------------------- #

class Counters:
    def __init__(self):
        self.outcomes: dict[str, int] = {}
        self.unexpected_errors: list[str] = []
        self.events_dropped = 0
        self.history_evictions = 0
        self.max_turn_s = 0.0
        self.max_lease_s = 0.0
        self.history_final_len = 0
        self.history_cap = 0

    def bump(self, key: str) -> None:
        self.outcomes[key] = self.outcomes.get(key, 0) + 1

    @property
    def n_unexpected(self) -> int:
        return len(self.unexpected_errors)


class BreakerWatcher(asyncio.Task):  # typing hint only; created via create_task
    pass


async def watch_breaker(breaker, stop: asyncio.Event, transitions: list,
                        counts: dict) -> None:
    last = None
    while not stop.is_set():
        snap = breaker.snapshot()
        cur = snap.state
        if last is not None and cur != last:
            transitions.append({
                "t": round(time.monotonic(), 3),
                "from": last, "to": cur,
                "trips": snap.trips, "penalty": snap.penalty_seconds,
            })
            key = f"{last}->{cur}"
            counts[key] = counts.get(key, 0) + 1
        last = cur
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05)
        except asyncio.TimeoutError:
            pass


def rss_kb_ps(pid: int) -> int | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        return None
    if not out:
        return None
    try:
        return int(out.split()[0])
    except (IndexError, ValueError):
        return None


def rss_sampler(pid: int, interval: float, sink: list, jsonl_path: Path,
                stop: threading.Event) -> None:
    while not stop.wait(interval):
        t_ps = time.monotonic()
        kb = rss_kb_ps(pid)
        dt_ms = (time.monotonic() - t_ps) * 1000.0
        if dt_ms > 500:
            print(f"[rss-sampler] slow ps call: {dt_ms:.0f}ms", flush=True)
        if kb is None:
            continue
        rec = {"kind": "rss", "t": round(time.monotonic(), 3),
               "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "rss_kb": kb}
        sink.append(rec)
        append_jsonl(jsonl_path, rec)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="SOAK-LITE-MOCK harness")
    p.add_argument("--duration", type=float, default=480.0,
                   help="soak window seconds (default 480)")
    p.add_argument("--rate", type=float, default=2.0,
                   help="turns per second (default 2)")
    p.add_argument("--cooldown", type=float, default=6.0,
                   help="breaker cooldown seconds via env (compressed-time "
                        "stand-in for the production 90 s; default 6)")
    p.add_argument("--rl-spacing", type=float, default=20.0,
                   help="seconds between RateLimited injections")
    p.add_argument("--cancel-spacing", type=float, default=13.0,
                   help="seconds between mid-send cancel injections")
    p.add_argument("--big-spacing", type=float, default=7.0,
                   help="seconds between near-cap prompt turns")
    p.add_argument("--big-chars", type=int, default=200_000,
                   help="near-cap prompt size in chars")
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--rss-interval", type=float, default=5.0)
    p.add_argument("--grace", type=float, default=15.0,
                   help="seconds to wait for in-flight turns after duration")
    p.add_argument("--stuck-lease-s", type=float, default=30.0,
                   help="a lease held longer than this flags stuck workers")
    p.add_argument("--out-dir", type=Path,
                   default=Path(os.environ.get("WEBGPT_RUNTIME_ROOT",
                             str(Path.home() / ".local" / "share" / "webgpt")))
                   / "soak-lite")
    return p.parse_args(argv)


async def run_soak(args) -> tuple[bool, dict]:
    # Env must be set BEFORE global_rate_limit_breaker() builds the singleton.
    os.environ[COOLDOWN_ENV] = str(args.cooldown)

    from gpt.factory import (
        ChatGPTWebSession,
        ChatGPTWorkerFactory,
        reset_global_rate_limit_breaker,
    )
    from gpt.state import SessionState
    from gpt.transport.breaker import global_rate_limit_breaker
    from gpt.transport.factory import _Worker

    reset_global_rate_limit_breaker()
    breaker = global_rate_limit_breaker()
    counters = Counters()
    transitions: list[dict] = []
    trans_counts: dict[str, int] = {}
    stop_watch = asyncio.Event()
    watcher = asyncio.create_task(
        watch_breaker(breaker, stop_watch, transitions, trans_counts))

    sessions_wrapped: set[int] = set()

    def wrap_session(session) -> None:
        if id(session) in sessions_wrapped:
            return
        sessions_wrapped.add(id(session))
        orig_emit = session._emit
        sess = session

        def counting_emit(event):
            if sess._events.qsize() >= 1_000:
                counters.events_dropped += 1
            return orig_emit(event)

        session._emit = counting_emit  # type: ignore[method-assign]

        orig_append = session._append_history_cache

        def counting_append(*turns):
            pre = len(sess._history_cache)
            orig_append(*turns)
            post = len(sess._history_cache)
            if post == sess._history_cache_max and pre + len(turns) > post:
                counters.history_evictions += 1
            counters.history_final_len = post
            counters.history_cap = sess._history_cache_max

        session._append_history_cache = counting_append  # type: ignore[method-assign]

    def make_session() -> ChatGPTWebSession:
        session = ChatGPTWebSession(cast(Any, FakeManager()), make_page())
        session.ui_driver = cast(Any, SuccessUI())
        return session

    factory = ChatGPTWorkerFactory(
        cast(Any, FakeManager()), max_workers=args.max_workers,
        warm_workers=min(2, args.max_workers),
    )

    async def patched_new_worker():
        session = make_session()
        # Real ChatGPTWebSession.create() lands the worker on READY after
        # bootstrap; mirror that so pooled sessions can accept sends.
        await session.state_machine.transition_to(SessionState.READY)
        wrap_session(session)
        worker = _Worker(
            worker_id=f"worker_{uuid.uuid4().hex[:12]}",
            session=session,
            created_at=time.monotonic(),
            last_used=time.monotonic(),
        )
        async with factory._lock:
            factory._all[worker.worker_id] = worker
            factory._created_workers += 1
        return worker

    factory._new_worker = patched_new_worker  # type: ignore[method-assign]

    t_start = time.monotonic()
    next_rl = t_start + args.rl_spacing
    next_cancel = t_start + args.cancel_spacing
    next_big = t_start + args.big_spacing
    in_flight: set = set()
    turn_log_path = args.out_dir / f"soak-lite-turns-{STAMP}.jsonl"

    def pick_kind(now: float) -> str:
        """Decide the fault kind at EXECUTION time (not spawn time) so a turn
        never races into becoming an unintended half-open probe."""
        nonlocal next_rl, next_cancel, next_big
        if now >= next_cancel:
            next_cancel = now + args.cancel_spacing
            return "cancel"
        if now >= next_rl and breaker.snapshot().state == "closed":
            next_rl = now + args.rl_spacing
            return "rate_limited"
        if now >= next_rl:  # breaker still recovering: defer injection
            next_rl = now + 2.0
        if now >= next_big:
            next_big = now + args.big_spacing
            return "big_prompt"
        return "normal"

    async def do_turn(index: int, due: float) -> None:
        delay = due - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        wake_ts = time.monotonic()
        kind = pick_kind(wake_ts)
        t0 = time.monotonic()
        lease_t0 = t0
        outcome = "ok"
        err = ""
        try:
            async with factory.lease() as session:
                if kind == "cancel":
                    hang = HangingUI()
                    session.ui_driver = cast(Any, hang)
                    inner = asyncio.create_task(session.send("poison-probe"))
                    try:
                        await asyncio.wait_for(hang.entered.wait(), timeout=5)
                    except asyncio.TimeoutError as exc:
                        inner.cancel()
                        raise RuntimeError("hanging driver never entered send") from exc
                    inner.cancel()
                    try:
                        await inner
                    except asyncio.CancelledError:
                        pass
                    # Worker-poison guard contract: cancellation must land a
                    # terminal state (never wedge mid-send states).
                    if session.state not in {SessionState.FATAL_ERROR,
                                             SessionState.CLOSED}:
                        raise RuntimeError(
                            f"poison guard failed: state={session.state.value}"
                        )
                    outcome = "cancel_poison_ok"
                elif kind == "rate_limited":
                    session.ui_driver = cast(Any, RateLimitedUI())
                    await session.send("rl-probe")
                else:
                    # Near-cap prompt: the full text enters the history cache,
                    # so eviction pressure (and its memory bound) is real.
                    size = args.big_chars if kind == "big_prompt" else 24
                    session.ui_driver = cast(Any, SuccessUI())
                    await session.send("x" * size)
        except RateLimited:
            outcome = "rate_limited"
        except BackendCoolingDown as exc:
            outcome = "cooling_down"
            err = str(exc)[:80]
        except asyncio.CancelledError:
            outcome = "cancelled_outer"
        except Exception as exc:  # anything unexpected fails the verdict
            outcome = "unexpected_error"
            err = f"{type(exc).__name__}: {exc}"[:200]
            counters.unexpected_errors.append(f"turn {index}: {err}")
        finally:
            dt = time.monotonic() - t0
            counters.bump(outcome)
            counters.max_turn_s = max(counters.max_turn_s, dt)
            counters.max_lease_s = max(counters.max_lease_s,
                                       time.monotonic() - lease_t0)
            append_jsonl(turn_log_path, {
                "kind": "turn", "index": index, "type": kind,
                "outcome": outcome, "dur_s": round(dt, 4),
                "wake_late_s": round(wake_ts - due, 4),
                "err": err,
            })

    # --- schedule turns at fixed rate -------------------------------------
    interval = 1.0 / args.rate
    n_turns = int(args.duration * args.rate)
    for i in range(n_turns):
        due = t_start + i * interval
        task = asyncio.create_task(do_turn(i, due))
        in_flight.add(task)
        task.add_done_callback(in_flight.discard)

    # Let the FULL soak window elapse first; the grace timeout below only
    # bounds turns still in flight after the window closes.
    await asyncio.sleep(max(0.0, t_start + args.duration - time.monotonic()))
    snapshot = set(in_flight)
    if snapshot:
        _done, pending = await asyncio.wait(snapshot, timeout=args.grace)
    else:
        _done, pending = set(), set()
    stuck_turns = len(pending)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    stop_watch.set()
    watcher.cancel()
    try:
        await watcher
    except asyncio.CancelledError:
        pass
    final_stats = await factory.stats()
    end_state = breaker.snapshot()
    await factory.close()

    summary = {
        "planned_turns": n_turns,
        "outcomes": dict(sorted(counters.outcomes.items())),
        "unexpected_errors": counters.unexpected_errors[:10],
        "n_unexpected": len(counters.unexpected_errors),
        "events_dropped": counters.events_dropped,
        "history_evictions": counters.history_evictions,
        "history_final_len": counters.history_final_len,
        "history_cap": counters.history_cap,
        "max_turn_s": round(counters.max_turn_s, 3),
        "max_lease_s": round(counters.max_lease_s, 3),
        "stuck_turns_after_grace": stuck_turns,
        "final_stats": vars(final_stats),
        "breaker_transitions": trans_counts,
        "breaker_transition_log_tail": transitions[-8:],
        "breaker_trips": end_state.trips,
        "breaker_end_state": end_state.state,
    }
    # Race-proof breaker accounting: every trip opens the window either from
    # closed or from a failed half-open probe; every open window must expire
    # into probing (a direct open->closed sample implies an unobserved
    # sub-poll half_open pass-through); final state closed proves the last
    # probe recovered.
    n_trips = end_state.trips
    t_closed_open = trans_counts.get("closed->open", 0)
    t_open_half = trans_counts.get("open->half_open", 0)
    t_open_closed = trans_counts.get("open->closed", 0)
    t_half_open_open = trans_counts.get("half_open->open", 0)
    t_half_open_closed = trans_counts.get("half_open->closed", 0)
    passed = (
        stuck_turns == 0
        and counters.n_unexpected == 0
        and final_stats.leased_workers == 0
        and counters.max_lease_s < args.stuck_lease_s
        and end_state.state == "closed"
        and n_trips > 0
        and t_closed_open + t_half_open_open == n_trips
        and t_open_half + t_open_closed == n_trips
        and t_half_open_closed >= 1
    )
    summary["breaker_check"] = {
        "trips": n_trips,
        "closed_to_open": t_closed_open,
        "open_to_half_open": t_open_half,
        "open_to_closed_direct_sampled": t_open_closed,
        "half_open_to_open_failed_probe": t_half_open_open,
        "half_open_to_closed_recovered": t_half_open_closed,
        "end_state": end_state.state,
    }
    return passed, summary


def evaluate_rss(samples: list[dict], duration: float) -> dict:
    if len(samples) < 4:
        return {"error": "too few samples", "n": len(samples)}
    values = [s["rss_kb"] for s in samples]
    baseline_n = max(1, len(values) // 10)
    baseline = sum(values[:baseline_n]) / baseline_n
    final = sum(values[-3:]) / 3.0
    growth_pct = (final - baseline) / baseline * 100.0
    max_dev_pct = max(abs(v - baseline) for v in values) / baseline * 100.0
    first, last = values[0], values[-1]
    mid_idx = min(range(len(samples)),
                  key=lambda i: abs(samples[i]["t"] - samples[0]["t"] - duration / 2))
    return {
        "n_samples": len(samples),
        "rss_first_kb": first,
        "rss_mid_kb": samples[mid_idx]["rss_kb"],
        "rss_last_kb": last,
        "baseline_kb": round(baseline, 1),
        "final_avg_kb": round(final, 1),
        "peak_kb": max(values),
        "growth_pct": round(growth_pct, 2),
        "max_excursion_pct": round(max_dev_pct, 2),
    }


def main(argv=None) -> int:
    global STAMP, RateLimited, SessionState, TurnResult
    global ResponseStarted, ResponseDelta, ResponseCompleted, BackendCoolingDown

    args = parse_args(argv)
    if args.rate <= 0 or args.duration <= 0:
        print("error: --duration and --rate must be > 0", file=sys.stderr)
        return 2
    if args.cooldown <= 0:
        print("error: --cooldown must be > 0 (breaker disabled would void the "
              "recovery-cycle check)", file=sys.stderr)
        return 2

    STAMP = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = args.out_dir / f"soak-lite-rss-{STAMP}.jsonl"
    report_path = args.out_dir / f"soak-lite-{STAMP}.md"

    from gpt.state import RateLimited
    from gpt.transport.breaker import BackendCoolingDown
    from gpt.types import (
        ResponseCompleted,
        ResponseDelta,
        ResponseStarted,
        TurnResult,
    )

    globals().update(RateLimited=RateLimited, TurnResult=TurnResult,
                     ResponseStarted=ResponseStarted,
                     ResponseDelta=ResponseDelta,
                     ResponseCompleted=ResponseCompleted,
                     BackendCoolingDown=BackendCoolingDown)

    pid = os.getpid()
    print(f"[soak-lite] pid={pid} duration={args.duration}s rate={args.rate}rps "
          f"planned_turns={int(args.duration * args.rate)} "
          f"cooldown={args.cooldown}s out={args.out_dir}", flush=True)
    print("[soak-lite] mock transport: no browser, no network, :18000 untouched",
          flush=True)

    samples: list[dict] = []
    kb0 = rss_kb_ps(pid)
    if kb0 is not None:
        samples.append({"kind": "rss", "t": round(time.monotonic(), 3),
                        "ts": "", "rss_kb": kb0})
        append_jsonl(jsonl_path, samples[-1])
    stop_sampler = threading.Event()
    sampler = threading.Thread(
        target=rss_sampler,
        args=(pid, args.rss_interval, samples, jsonl_path, stop_sampler),
        daemon=True, name="rss-sampler",
    )
    sampler.start()

    passed, summary = asyncio.run(run_soak(args))
    stop_sampler.set()
    sampler.join(timeout=args.rss_interval + 2)
    kb_end = rss_kb_ps(pid)
    if kb_end is not None:
        samples.append({"kind": "rss", "t": round(time.monotonic(), 3),
                        "ts": "", "rss_kb": kb_end})
    duration = args.duration
    rss = evaluate_rss(samples, duration)
    summary["rss"] = rss

    rss_ok = ("error" not in rss and abs(rss["growth_pct"]) < 15.0
              and rss["max_excursion_pct"] < 15.0)
    bc = summary.get("breaker_check", {})
    breaker_ok = (
        bc.get("trips", 0) > 0
        and bc.get("end_state") == "closed"
        and bc.get("closed_to_open", 0) + bc.get("half_open_to_open_failed_probe", 0)
        == bc.get("trips")
        and bc.get("open_to_half_open", 0) + bc.get("open_to_closed_direct_sampled", 0)
        == bc.get("trips")
        and bc.get("half_open_to_closed_recovered", 0) >= 1
    )
    workers_ok = (summary["stuck_turns_after_grace"] == 0
                  and summary["final_stats"]["leased_workers"] == 0
                  and summary["max_lease_s"] < args.stuck_lease_s)
    errors_ok = summary["n_unexpected"] == 0
    passed = bool(rss_ok and breaker_ok and workers_ok and errors_ok)

    checks = [
        ("rss_flat_lt_15pct", rss_ok,
         f"growth={rss.get('growth_pct', 'n/a')}% "
         f"excursion={rss.get('max_excursion_pct', 'n/a')}%"),
        ("breaker_recovered_every_cycle", breaker_ok,
         f"check={bc}"),
        ("no_stuck_workers", workers_ok,
         f"stuck_turns={summary['stuck_turns_after_grace']} "
         f"leased_final={summary['final_stats']['leased_workers']} "
         f"max_lease_s={summary['max_lease_s']}"),
        ("zero_unexpected_errors", errors_ok,
         f"outcomes={summary['outcomes']}"),
    ]

    lines = [
        "# SOAK-LITE-MOCK report", "",
        f"- Stamp: {STAMP}",
        f"- Window: {args.duration:.0f}s @ {args.rate}rps "
        f"(planned {summary['planned_turns']} turns)",
        f"- Fault mix: RateLimited every {args.rl_spacing:.0f}s "
        f"(cooldown {args.cooldown:.0f}s), mid-send cancel every "
        f"{args.cancel_spacing:.0f}s, near-cap prompt ({args.big_chars} chars) "
        f"every {args.big_spacing:.0f}s",
        "- Transport: MOCK in-process (no browser/network; :18000 untouched)", "",
        "## Raw numbers", "",
        f"- Outcomes: `{summary['outcomes']}`",
        f"- Breaker transitions: `{summary['breaker_transitions']}` "
        f"(trips={summary['breaker_trips']}, end={summary['breaker_end_state']})",
        f"- Transition log tail: `{summary['breaker_transition_log_tail']}`",
        f"- Events dropped (_emit cap 1000): {summary['events_dropped']}",
        f"- History cache: evictions={summary['history_evictions']} "
        f"final_len={summary['history_final_len']}/{summary['history_cap']}",
        f"- Max turn {summary['max_turn_s']}s, max lease {summary['max_lease_s']}s, "
        f"stuck turns after grace: {summary['stuck_turns_after_grace']}",
        f"- Final factory stats: `{summary['final_stats']}`",
        f"- Unexpected errors: {summary['n_unexpected']} "
        f"`{summary['unexpected_errors']}`",
        f"- RSS (ps, KB): first={rss.get('rss_first_kb')} "
        f"mid={rss.get('rss_mid_kb')} last={rss.get('rss_last_kb')} "
        f"peak={rss.get('peak_kb')} growth={rss.get('growth_pct')}% "
        f"max_excursion={rss.get('max_excursion_pct')}% "
        f"(n={rss.get('n_samples')})", "",
        "## Verdict", "",
        "| check | result | detail |", "|---|---|---|",
    ]
    for name, ok, detail in checks:
        lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
    lines += ["", f"# OVERALL: {'PASS' if passed else 'FAIL'}", ""]
    report_path.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str),
          flush=True)
    print(f"[soak-lite] report: {report_path}", flush=True)
    print(f"[soak-lite] metrics: {jsonl_path}", flush=True)
    print(f"[soak-lite] VERDICT: {'PASS' if passed else 'FAIL'}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
