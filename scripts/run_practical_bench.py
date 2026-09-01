#!/usr/bin/env python3
"""Runner for the offline practical benchmark (benchmarks/practical).

One command per run when the gateway is healthy:

    .venv/bin/python scripts/run_practical_bench.py                # all tasks
    .venv/bin/python scripts/run_practical_bench.py --task bugfix
    .venv/bin/python scripts/run_practical_bench.py --dry-run      # plan only

Per-task flow
-------------
1. Build a FRESH workspace from the pristine fixture:
   ``benchmarks/practical/tasks/<t>/fixture`` -> ``<bench-run>/<ts>/<t>/``
2. Run the Claude Code CLI inside that workspace with the task prompt taken
   verbatim from ``task.json``, pointed at the local webgpt gateway via
   ``ANTHROPIC_BASE_URL`` (same env pattern as the other scripts here).
3. Grade mechanically: ``benchmarks/practical/grade.py --task <t> --ws <dir>``
   (exit 0 = PASS, 1 = FAIL).
4. Append a JSON entry (task / pass / wall-time / tool_use when traceable /
   grader check counts) to ``<bench-run>/<ts>/results.json`` and
   print a summary table at the end.

Cleanup policy: after grading, a PASSED workspace is deleted unless
``--keep-ws``; a FAILED workspace is always kept for inspection (plus logs,
grader output and raw claude output live next to it under the run dir).

Modes
-----
default   : target the live gateway; probe /health first and refuse to start
            if unreachable (exit 2) instead of burning timeouts.
--dry-run : print the plan (workspace path, prompt sent, exact commands)
            and touch NOTHING on disk.

Exit codes: 0 all selected tasks PASSed grading; 1 at least one task failed;
2 environment/usage problem (missing fixture, claude binary, gateway down).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from gpt.agent.events import AgentEvent
from gpt.agent.runner import AgentRunner, AgentRunnerConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = REPO_ROOT / "benchmarks" / "practical"
TASKS_DIR = BENCH_DIR / "tasks"
GRADE_PY = BENCH_DIR / "grade.py"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

def discover_tasks() -> tuple[str, ...]:
    """Discover every practical benchmark task carrying task.json."""
    return tuple(sorted(p.parent.name for p in TASKS_DIR.glob("*/task.json")))


TASKS = discover_tasks()

# Bench run root: $WEBGPT_BENCH_RUN_ROOT wins, else the XDG runtime root
# (~/.local/share/webgpt/bench-run).  Resolved lazily so a late env change
# still applies; never ~/Downloads.
BENCH_RUN_ROOT_ENV = "WEBGPT_BENCH_RUN_ROOT"


def bench_run_parent() -> Path:
    override = os.environ.get(BENCH_RUN_ROOT_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "webgpt" / "bench-run"

# Same convention as webgpt-claude.sh / practical_cli_bench.py: shell-provided
# values win, we only fill gaps. These are LOCAL-gateway placeholders, not
# upstream credentials (the gateway accepts its own dummy key).
CLAUDE_BIN = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
DEFAULT_GATEWAY_URL = os.environ.get("WEBGPT_GATEWAY_URL", "http://127.0.0.1:18000")
DEFAULT_API_KEY = os.environ.get("WEBGPT_API_KEY", "sk-webgpt-local")
DEFAULT_CLAUDE_MODEL = os.environ.get("E2E_CLAUDE_MODEL", "claude-3-5-sonnet")
DEFAULT_DIRECT_MODEL = os.environ.get("WEBGPT_DIRECT_MODEL", "gpt-5-5-thinking")


def venv_python() -> str:
    return str(VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable))


def load_task_spec(task: str) -> dict:
    path = TASKS_DIR / task / "task.json"
    if not path.is_file():
        raise FileNotFoundError(f"task.json not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def build_claude_env(base_url: str) -> dict[str, str]:
    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
    env.setdefault("ANTHROPIC_API_KEY", DEFAULT_API_KEY)
    env.setdefault("CLAUDE_DEFAULT_MODEL", DEFAULT_CLAUDE_MODEL)
    env.setdefault("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "200000")
    env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "8192")
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    env.setdefault("DISABLE_TELEMETRY", "1")
    env.setdefault("DISABLE_ERROR_REPORTING", "1")
    return env


def claude_cmd(prompt: str, trace: bool) -> list[str]:
    cmd = [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions", "--print"]
    if trace:
        # Best-effort tool_use tracing; parsed defensively, never fatal.
        cmd += ["--verbose", "--output-format", "stream-json"]
    return cmd


def describe_cmd(cmd: list[str], env: dict[str, str]) -> str:
    """Human-readable command line for the dry-run plan (env summarized)."""
    shown_keys = (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
        "CLAUDE_DEFAULT_MODEL",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
    )
    parts = []
    for k in shown_keys:
        if k in env:
            val = "<set>" if k == "ANTHROPIC_API_KEY" else env[k]
            parts.append(f"{k}={val}")
    return (f"cd <workspace> && {' '.join(parts)} "
            f"{cmd[0]} {' '.join(cmd[1:])}")


def parse_stream_json(raw: str) -> tuple[int | None, str]:
    """Extract (tool_use_count, final_result_text) from stream-json output.

    Returns (None, "") when nothing parseable was found (caller records
    tool_use=null rather than inventing a number).
    """
    tools = 0
    saw_any = False
    final_text = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        saw_any = True
        if event.get("type") == "assistant":
            msg = event.get("message") or {}
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tools += 1
        elif event.get("type") == "result":
            final_text = str(event.get("result") or "")
    return (tools if saw_any else None), final_text


def initialize_git_workspace(ws: Path, spec: dict) -> None:
    """Create a deterministic local baseline commit for process-aware V2 tasks."""
    if not spec.get("init_git"):
        return
    commands = [
        ["git", "init", "-q"],
        ["git", "config", "user.email", "bench@example.invalid"],
        ["git", "config", "user.name", "WebGPT Practical Bench"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "[bench] pristine fixture"],
    ]
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_DATE", "2026-01-01T00:00:00+00:00")
    env.setdefault("GIT_COMMITTER_DATE", "2026-01-01T00:00:00+00:00")
    for cmd in commands:
        proc = subprocess.run(cmd, cwd=str(ws), env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"git bootstrap failed ({' '.join(cmd)}): "
                f"{(proc.stderr or proc.stdout).strip()[:200]}"
            )


def run_claude(ws: Path, spec: dict, timeout_s: int, base_url: str,
               log_path: Path) -> dict:
    """Run the claude CLI in the workspace. Returns a partial result entry."""
    prompt = spec.get("prompt", "")
    env = build_claude_env(base_url)

    def _invoke(cmd: list[str]) -> tuple[subprocess.CompletedProcess | None, str]:
        try:
            proc = subprocess.run(
                cmd, cwd=str(ws), env=env, timeout=timeout_s,
                capture_output=True, text=True)
            return proc, ""
        except FileNotFoundError:
            return None, f"claude binary not found: {CLAUDE_BIN}"
        except subprocess.TimeoutExpired:
            return None, f"TIMEOUT after {timeout_s}s"

    started = time.monotonic()
    cmd = claude_cmd(prompt, trace=True)
    proc, err = _invoke(cmd)
    retried_plain = False
    if proc is None and not err:
        # _invoke returned (None, "") only via unexpected path; treat as failure
        err = "unknown invocation failure"
    if proc is not None and proc.returncode != 0 and not (proc.stdout or "").strip():
        # stream-json flags rejected by this CLI version -> retry plainly
        # (flag-parse failures happen before any model traffic, so this is free)
        retried_plain = True
        cmd = claude_cmd(prompt, trace=False)
        proc, err = _invoke(cmd)

    elapsed = time.monotonic() - started

    log_path.write_text(
        "--- cmd ---\n" + " ".join(cmd) +
        "\n--- rc ---\n" + (str(proc.returncode) if proc else "n/a") +
        "\n--- stdout ---\n" + (proc.stdout if proc else "") +
        "\n--- stderr ---\n" + (proc.stderr if proc else "") +
        ("\n--- note ---\nretried without stream-json\n" if retried_plain else ""),
        encoding="utf-8")

    entry: dict = {
        "claude_rc": proc.returncode if proc is not None else None,
        "time_s": round(elapsed, 1),
        "error": None,
    }
    if proc is None:
        entry["error"] = err
        entry["pass"] = False
        entry["tool_use"] = None
        return entry

    tool_use, final_text = parse_stream_json(proc.stdout or "")
    if tool_use is None and not retried_plain:
        # unparseable stream output; keep raw tail already logged, mark unknown
        tool_use = None
    entry["tool_use"] = tool_use
    entry["result_chars"] = len(final_text or (proc.stdout or ""))
    if proc.returncode != 0:
        entry["error"] = (proc.stderr or proc.stdout or "").strip()[-400:] or \
            f"exit={proc.returncode}"
    return entry


def run_direct(
    ws: Path,
    spec: dict,
    timeout_s: int,
    base_url: str,
    model: str,
    log_path: Path,
) -> dict:
    """Run the shared AgentRunner in-process; no CLI/subprocess hop."""
    events: list[dict] = []

    def on_event(event: AgentEvent) -> None:
        events.append(
            {
                "kind": event.kind,
                "round": event.round_index,
                "data": event.data,
            }
        )

    config = AgentRunnerConfig(
        base_url=base_url,
        model=model,
        max_rounds=30,
        timeout_seconds=min(float(timeout_s), 300.0),
        overall_timeout_seconds=float(timeout_s),
        verify="auto",
        persist_session=False,
    )
    started = time.monotonic()
    with AgentRunner(
        workspace=ws,
        config=config,
        event_callback=on_event,
    ) as agent:
        result = agent.run(str(spec.get("prompt", "")))
    elapsed = time.monotonic() - started
    log_path.write_text(
        json.dumps(
            {
                "result": {
                    "success": result.success,
                    "text": result.text,
                    "rounds": result.rounds,
                    "tool_calls": result.tool_calls,
                    "stop_reason": result.stop_reason,
                    "elapsed_seconds": result.elapsed_seconds,
                    "error": result.error,
                    "verification_gate_count": result.verification_gate_count,
                },
                "events": events,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "agent_rc": 0 if result.success else 1,
        "claude_rc": None,
        "time_s": round(elapsed, 1),
        "tool_use": result.tool_calls,
        "result_chars": len(result.text),
        "error": result.error,
        "agent_rounds": result.rounds,
        "agent_session_id": result.session_id,
        "verification_gate_count": result.verification_gate_count,
    }

def run_grader(task: str, ws: Path, log_path: Path) -> dict:
    """Invoke benchmarks/practical/grade.py --task --ws; parse its JSON line."""
    cmd = [venv_python(), str(GRADE_PY), "--task", task, "--ws", str(ws), "--json"]
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True,
                              text=True, timeout=900)
    except FileNotFoundError as exc:
        log_path.write_text(f"grader spawn failed: {exc}", encoding="utf-8")
        return {"verdict": "FAIL", "passed_checks": None, "total_checks": None}
    except subprocess.TimeoutExpired:
        log_path.write_text("grader TIMEOUT >900s", encoding="utf-8")
        return {"verdict": "FAIL", "passed_checks": None, "total_checks": None}

    log_path.write_text(
        f"--- cmd ---\n{' '.join(cmd)}\n--- rc ---\n{proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
        encoding="utf-8")

    verdict = "PASS" if proc.returncode == 0 else "FAIL"
    passed_checks = total_checks = None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and '"checks"' in line:
            try:
                data = json.loads(line)
                passed_checks = data.get("passed")
                total_checks = data.get("total")
            except json.JSONDecodeError:
                pass
            break
    return {"verdict": verdict, "passed_checks": passed_checks,
            "total_checks": total_checks}


def gateway_alive(base_url: str, seconds: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/health",
                                    timeout=seconds) as resp:
            return resp.status == 200
    except Exception:
        return False


def print_summary(run_dir: Path, results: list[dict], ts: str) -> None:
    print()
    print("=" * 72)
    print(f"practical-bench run {ts} | results: {run_dir / 'results.json'}")
    print("=" * 72)
    header = f"{'task':<10} {'result':<7} {'time':>8} {'tools':>6} {'checks':>8}"
    print(header)
    print("-" * 72)
    for r in results:
        pass_state = r.get("pass")
        res = "PASS" if pass_state is True else "FAIL" if pass_state is False else "?"
        checks = "-" if r.get("passed_checks") is None \
            else f"{r['passed_checks']}/{r['total_checks']}"
        tools = "-" if r.get("tool_use") is None else str(r["tool_use"])
        print(f"{r['task']:<10} {res:<7} {r['time_s']:>7.1f}s {tools:>6} "
              f"{checks:>8}")
    print("=" * 72)


def execute_task(task: str, spec: dict, run_dir: Path, args) -> dict:
    fixture = TASKS_DIR / task / "fixture"
    if not fixture.is_dir():
        return {"task": task, "pass": False, "time_s": 0.0, "tool_use": None,
                "claude_rc": None, "error": f"fixture missing: {fixture}"}

    ws = run_dir / task
    print(f"[{task}] fresh workspace: {ws}")
    shutil.copytree(fixture, ws)
    try:
        initialize_git_workspace(ws, spec)
    except RuntimeError as exc:
        return {"task": task, "pass": False, "time_s": 0.0, "tool_use": None,
                "claude_rc": None, "error": str(exc)}

    log_dir = run_dir / "logs"
    log_dir.mkdir(exist_ok=True)

    timeout_s = int(spec.get("timeout_s", args.timeout))
    entry = {"task": task, "timeout_s": timeout_s, "agent": args.agent}
    if args.agent == "direct":
        print(f"[{task}] running direct GPT agent (timeout {timeout_s}s)...")
        entry.update(
            run_direct(
                ws,
                spec,
                timeout_s,
                args.gateway_url,
                args.model,
                log_dir / f"{task}_direct.log",
            )
        )
        if entry.get("agent_rc") not in (None, 0):
            error_text = str(entry.get("error") or "")
            print(f"[{task}] direct agent exited rc={entry['agent_rc']}: {error_text[:200]}")
    else:
        print(f"[{task}] running claude (timeout {timeout_s}s)...")
        entry.update(
            run_claude(
                ws,
                spec,
                timeout_s,
                args.gateway_url,
                log_dir / f"{task}_claude.log",
            )
        )
        if entry.get("claude_rc") is not None and entry["claude_rc"] != 0:
            error_text = str(entry.get("error") or "")
            print(f"[{task}] claude exited rc={entry['claude_rc']}: {error_text[:200]}")

    print(f"[{task}] grading via grade.py ...")
    grade = run_grader(task, ws, log_dir / f"{task}_grade.log")
    entry.update({
        "pass": grade["verdict"] == "PASS",
        "passed_checks": grade["passed_checks"],
        "total_checks": grade["total_checks"],
    })
    print(f"[{task}] grader verdict: {grade['verdict']}")

    if not args.keep_ws and entry["pass"]:
        shutil.rmtree(ws, ignore_errors=True)
        print(f"[{task}] workspace cleaned (PASS; use --keep-ws to retain)")
    elif not args.keep_ws:
        print(f"[{task}] workspace kept for inspection (FAIL)")
    else:
        print(f"[{task}] workspace kept (--keep-ws)")
    return entry


def plan_task(task: str, spec: dict, run_dir: Path, args) -> None:
    ws = run_dir / task
    print(f"\n----- plan: {task} -----")
    print(f"title       : {spec.get('title', '?')}")
    print(f"fixture     : {TASKS_DIR / task / 'fixture'}")
    print(f"workspace   : {ws}  (fresh copy per run)")
    if spec.get("init_git"):
        print("git         : initialize local repo + deterministic pristine baseline commit")
    print("prompt      :")
    for ln in spec.get("prompt", "").splitlines():
        print(f"  | {ln}")
    timeout_s = int(spec.get("timeout_s", args.timeout))
    if args.agent == "direct":
        direct = [
            venv_python(), "-m", "gpt.direct_agent",
            "--workspace", str(ws),
            "--base-url", args.gateway_url,
            "--model", args.model,
            "--timeout", str(timeout_s),
            "--max-rounds", "30",
            "--json", "--quiet",
            spec.get("prompt", ""),
        ]
        print(f"agent cmd   : {' '.join(direct)}")
    else:
        env = build_claude_env(args.gateway_url)
        print(f"claude cmd  : {describe_cmd(claude_cmd(spec.get('prompt',''), True), env)}")
        print(f"              (cwd={ws}, timeout={timeout_s}s; falls back to plain "
              f"`claude -p ... --print` if stream-json is unsupported)")
    grade_cmd = ([venv_python(), str(GRADE_PY), "--task", task, "--ws", str(ws),
                  "--json"])
    print(f"grade cmd   : {' '.join(grade_cmd)}")
    print("cleanup     : "
          + ("keep workspace (--keep-ws)" if args.keep_ws
             else "delete ws on PASS, keep on FAIL"))


def dry_run(args) -> int:
    names = list(TASKS) if args.task == "all" else [args.task]
    ts = "DRYRUN-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = bench_run_parent() / ts
    print("practical-bench DRY RUN -- nothing will be created or executed")
    print(f"run dir (would be): {run_dir}")
    print(f"gateway           : {args.gateway_url} (/health NOT probed in dry-run)")
    for name in names:
        try:
            spec = load_task_spec(name)
        except FileNotFoundError as exc:
            print(f"\nERROR: {exc}", file=sys.stderr)
            return 2
        plan_task(name, spec, run_dir, args)
    print("\ndry run complete: no workspace created, no agent invoked, "
          "no grading executed")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the offline practical benchmark through the webgpt "
                    "gateway and grade it with benchmarks/practical/grade.py.",
        epilog="examples:\n"
               "  %(prog)s                          # run all tasks\n"
               "  %(prog)s --task refactor          # single task\n"
               "  %(prog)s --dry-run               # print plan, do nothing\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", choices=[*TASKS, "all"], default="all",
                        help="which task to run (default: all)")
    parser.add_argument("--timeout", type=int, default=900,
                        help="fallback per-task agent timeout in seconds (default 900)")
    parser.add_argument("--agent", choices=["direct", "claude"], default="direct",
                        help="live agent backend (default: direct GPT; Claude Code is compatibility only)")
    parser.add_argument("--model", default=DEFAULT_DIRECT_MODEL,
                        help="model label for --agent direct (default: %(default)s)")
    parser.add_argument("--keep-ws", action="store_true",
                        help="keep candidate workspaces after grading "
                             "(default: PASS-workspaces are deleted, FAIL kept)")
    parser.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL,
                        help="webgpt gateway base URL "
                             "(default: WEBGPT_GATEWAY_URL or %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the execution plan and exit; no disk writes, "
                             "no agent run, no grading")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.dry_run:
        return dry_run(args)

    names = list(TASKS) if args.task == "all" else [args.task]

    # preflight ------------------------------------------------------------
    if not GRADE_PY.is_file():
        print(f"ERROR: grader not found: {GRADE_PY}", file=sys.stderr)
        return 2
    if args.agent == "claude" and not Path(CLAUDE_BIN).exists():
        print(f"ERROR: claude CLI not found: {CLAUDE_BIN}", file=sys.stderr)
        return 2
    for name in names:
        try:
            load_task_spec(name)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    if not gateway_alive(args.gateway_url):
        print(f"ERROR: gateway not responding at {args.gateway_url}/health -- "
              f"start/recover webgpt-gateway.service first.", file=sys.stderr)
        return 2

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = bench_run_parent() / ts
    run_dir.mkdir(parents=True, exist_ok=False)
    print(f"[run] {ts} | tasks={','.join(names)} | run dir: {run_dir}")

    results_path = run_dir / "results.json"
    payload: dict = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "gateway_url": args.gateway_url,
        "timeout_s": args.timeout,
        "agent": args.agent,
        "model": args.model if args.agent == "direct" else DEFAULT_CLAUDE_MODEL,
        "tasks": [],
    }
    for name in names:
        spec = load_task_spec(name)
        entry = execute_task(name, spec, run_dir, args)
        payload["tasks"].append(entry)
        results_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    payload["finished_at"] = datetime.now().isoformat(timespec="seconds")
    results_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print_summary(run_dir, payload["tasks"], ts)

    return 0 if all(r.get("pass") is True for r in payload["tasks"]) else 1


if __name__ == "__main__":
    sys.exit(main())
