#!/usr/bin/env .venv/bin/python
"""Verify harness for the gateway --transport browser -> hybrid flip.

Speaks the Anthropic Messages protocol directly against the local gateway
(same env contract as the other live scripts: ANTHROPIC_BASE_URL /
ANTHROPIC_API_KEY are read from the environment, never hardcoded).

Levels:
  t1   streaming turn      -- counts text deltas, measures first-token latency
  t2   single-step tool_use -- model must create a file with exact content
  t3   multi-step mini loop -- <=10 tool_use rounds, closed loop, no controller
                              correction; harness only executes tools mechanically

Artifacts for t2/t3 are written under $WEBGPT_RUNTIME_ROOT/tmp
(default ~/.local/share/webgpt/tmp) — never ~/Downloads.

Usage:
    .venv/bin/python scripts/verify_hybrid_flip.py --level all --timeout 120
    ANTHROPIC_BASE_URL=http://127.0.0.1:1 .venv/bin/python scripts/verify_hybrid_flip.py --level t1 --timeout 8

Exit code: 0 when every requested level PASSES, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:18000"
ANTHROPIC_VERSION = "2023-06-01"
MAX_T3_TOOL_ROUNDS = 10
T3_EXPECTED_SUM = sum(i * i for i in range(1, 101))  # 338350


# --------------------------------------------------------------------------- infra


def build_headers(api_key: str) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "x-api-key": api_key,
        "authorization": f"Bearer {api_key}",
        "anthropic-version": ANTHROPIC_VERSION,
    }


def parse_sse_stream(response: httpx.Response) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event_name, data_dict) pairs."""
    events: list[tuple[str, dict]] = []
    event_name = ""
    data_lines: list[str] = []
    for raw_line in response.iter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                try:
                    payload = json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    payload = {}
                events.append((event_name or "message", payload))
            event_name = ""
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    if data_lines:
        try:
            events.append((event_name or "message", json.loads("\n".join(data_lines))))
        except json.JSONDecodeError:
            pass
    return events


def stream_turn(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    body: dict,
    timeout_s: float,
) -> tuple[dict | None, dict]:
    """POST a streaming /v1/messages turn. Returns (final_message, metrics)."""
    metrics: dict = {
        "http_status": None,
        "delta_count": 0,
        "first_token_latency_s": None,
        "total_duration_s": None,
        "text": "",
        "message_stop": False,
        "error": None,
    }
    started = time.monotonic()
    url = base_url.rstrip("/") + "/v1/messages"
    final_message: dict | None = None
    try:
        with client.stream("POST", url, headers=headers, json=body) as response:
            metrics["http_status"] = response.status_code
            if response.status_code != 200:
                metrics["error"] = response.read().decode(errors="replace")[:500]
                return None, metrics
            text_parts: list[str] = []
            for name, payload in parse_sse_stream(response):
                etype = payload.get("type", name)
                if etype == "message_start":
                    final_message = payload.get("message")
                elif etype == "content_block_delta":
                    delta = payload.get("delta", {})
                    if metrics["first_token_latency_s"] is None:
                        metrics["first_token_latency_s"] = round(time.monotonic() - started, 3)
                    if delta.get("type") == "text_delta":
                        metrics["delta_count"] += 1
                        text_parts.append(delta.get("text", ""))
                elif etype == "message_stop":
                    metrics["message_stop"] = True
                elif etype == "error":
                    metrics["error"] = json.dumps(payload, ensure_ascii=False)[:500]
    except httpx.HTTPError as exc:
        metrics["error"] = f"{type(exc).__name__}: {exc}"
        metrics["total_duration_s"] = round(time.monotonic() - started, 3)
        return None, metrics
    metrics["total_duration_s"] = round(time.monotonic() - started, 3)
    metrics["text"] = "".join(text_parts)
    return final_message, metrics


def complete_turn(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    body: dict,
    timeout_s: float,
) -> tuple[dict | None, dict]:
    """POST a non-streaming /v1/messages turn. Returns (response_json, metrics)."""
    metrics: dict = {
        "http_status": None,
        "duration_s": None,
        "stop_reason": None,
        "error": None,
    }
    started = time.monotonic()
    url = base_url.rstrip("/") + "/v1/messages"
    try:
        response = client.post(url, headers=headers, json=body, timeout=timeout_s)
    except httpx.HTTPError as exc:
        metrics["error"] = f"{type(exc).__name__}: {exc}"
        metrics["duration_s"] = round(time.monotonic() - started, 3)
        return None, metrics
    metrics["duration_s"] = round(time.monotonic() - started, 3)
    metrics["http_status"] = response.status_code
    if response.status_code != 200:
        metrics["error"] = response.text[:500]
        return None, metrics
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        metrics["error"] = f"non-JSON body: {exc}"
        return None, metrics
    metrics["stop_reason"] = payload.get("stop_reason")
    if isinstance(payload.get("error"), dict):
        metrics["error"] = json.dumps(payload["error"], ensure_ascii=False)[:500]
        return None, metrics
    return payload, metrics


def extract_tool_uses(message: dict | None) -> list[dict]:
    blocks = (message or {}).get("content") or []
    if isinstance(blocks, str):
        return []
    return [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]


def extract_text(message: dict | None) -> str:
    blocks = (message or {}).get("content") or []
    if isinstance(blocks, str):
        return blocks
    return "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")


# --------------------------------------------------------------------------- levels


WRITE_FILE_TOOL = {
    "name": "write_file",
    "description": "Write text content to a file at an absolute path, replacing any existing file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute destination path."},
            "content": {"type": "string", "description": "Exact file content."},
        },
        "required": ["path", "content"],
    },
}

RUN_PYTHON_TOOL = {
    "name": "run_python",
    "description": "Execute a Python script file with the venv interpreter and capture stdout/stderr.",
    "input_schema": {
        "type": "object",
        "properties": {
            "script_path": {"type": "string", "description": "Absolute path of the script to run."},
        },
        "required": ["script_path"],
    },
}

# T2 harness parity (debug-t2-2026-08-25): the real CLI surface always carries a
# shell tool named exactly "Bash" (gpt/utils/toolcall.py _shell_tool_name only
# recognizes Bash/bash). Declaring it here gives the soft <cmd> protocol a real
# mapping target instead of dying on a write_file-only surface.
BASH_TOOL = {
    "name": "Bash",
    "description": "Execute a shell command inside the task workdir and capture stdout/stderr.",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Full shell command to run."},
        },
        "required": ["command"],
    },
}


def exec_write_file(workdir: Path, args: dict) -> str:
    path = Path(str(args.get("path", "")))
    if not path.is_absolute():
        path = workdir / path
    content = str(args.get("content", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} bytes to {path}"


def exec_run_python(workdir: Path, args: dict) -> str:
    script = Path(str(args.get("script_path", "")))
    if not script.is_absolute():
        script = workdir / script
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=str(workdir),
        timeout=60,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    return f"exit={proc.returncode}\nstdout:\n{out[:4000]}\nstderr:\n{err[:2000]}"


def exec_bash(workdir: Path, args: dict) -> str:
    proc = subprocess.run(
        ["bash", "-c", str(args.get("command", ""))],
        capture_output=True,
        text=True,
        cwd=str(workdir),
        timeout=120,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    return f"exit={proc.returncode}\nstdout:\n{out[:4000]}\nstderr:\n{err[:2000]}"


TOOL_EXECUTORS = {
    "write_file": exec_write_file,
    "run_python": exec_run_python,
    "Bash": exec_bash,
}


def run_tool_loop(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    model: str,
    system_prompt: str,
    task: str,
    tools: list[dict],
    workdir: Path,
    max_rounds: int,
    timeout_s: float,
) -> tuple[bool, dict]:
    """Mechanical closed tool loop: execute tool_use, feed tool_result back.

    The harness never injects corrections or hints; it only runs the requested
    tools and returns their output verbatim.
    """
    messages: list[dict] = [{"role": "user", "content": task}]
    body_base = {
        "model": model,
        "max_tokens": 2048,
        "system": system_prompt,
        "tools": tools,
    }
    metrics: dict = {
        "rounds_used": 0,
        "tool_use_count": 0,
        "stop_reasons": [],
        "exec_results": [],
        "loop_error": None,
        "hit_round_cap": False,
    }
    while True:
        body = {**body_base, "messages": messages}
        reply, req_metrics = complete_turn(client, base_url, headers, body, timeout_s)
        if reply is None:
            metrics["loop_error"] = f"transport failure: {json.dumps(req_metrics, ensure_ascii=False)}"
            return False, metrics
        stop_reason = reply.get("stop_reason")
        metrics["stop_reasons"].append(stop_reason)
        calls = extract_tool_uses(reply)
        assistant_blocks: list[dict] = []
        text = extract_text(reply)
        if text:
            assistant_blocks.append({"type": "text", "text": text})
        assistant_blocks.extend(calls)
        messages.append({"role": "assistant", "content": assistant_blocks})
        if not calls:
            return True, metrics
        results: list[dict] = []
        for call in calls:
            metrics["tool_use_count"] += 1
            name_obj = call.get("name")
            name = str(name_obj) if name_obj is not None else ""
            args = call.get("input") or {}
            executor = TOOL_EXECUTORS.get(name)
            if executor is None:
                result_text = f"ERROR: unknown tool {name!r}"
            else:
                try:
                    result_text = executor(workdir, args)
                except Exception as exc:  # mechanical execution failure is reported, not corrected
                    result_text = f"ERROR: {type(exc).__name__}: {exc}"
            metrics["exec_results"].append(result_text[:300])
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.get("id"),
                    "content": result_text,
                }
            )
        metrics["rounds_used"] += 1
        messages.append({"role": "user", "content": results})
        if metrics["rounds_used"] >= max_rounds:
            metrics["hit_round_cap"] = True
            return False, metrics


def level_t1(ctx: dict) -> tuple[bool, dict]:
    client, base_url, headers, model, timeout = ctx["client"], ctx["base_url"], ctx["headers"], ctx["model"], ctx["timeout"]
    body = {
        "model": model,
        "max_tokens": 256,
        "stream": True,
        "messages": [
            {
                "role": "user",
                "content": "Count from 1 to 20, numbers separated by single spaces. Output only the numbers.",
            }
        ],
    }
    _final, metrics = stream_turn(client, base_url, headers, body, timeout)
    passed = (
        metrics["http_status"] == 200
        and metrics["message_stop"]
        and metrics["delta_count"] >= 1
        and len(metrics["text"].strip()) > 0
        and metrics["error"] is None
    )
    return passed, metrics


def level_t2(ctx: dict) -> tuple[bool, dict]:
    client, base_url, headers, model, timeout = ctx["client"], ctx["base_url"], ctx["headers"], ctx["model"], ctx["timeout"]
    workdir: Path = ctx["workdir"]
    target = workdir / "t2_hello.txt"
    expected = "HYBRID_FLIP_T2_OK"
    target.unlink(missing_ok=True)
    task = (
        f"Use the write_file tool to create a file at exactly {target} "
        f"whose entire content is exactly this string: {expected}\n"
        "Do not add anything else. Finish after the tool call."
    )
    ok_loop, loop_metrics = run_tool_loop(
        client, base_url, headers, model,
        system_prompt="You complete tasks by calling the provided tools. Be precise.",
        task=task,
        tools=[WRITE_FILE_TOOL, BASH_TOOL],
        workdir=workdir,
        max_rounds=3,
        timeout_s=timeout,
    )
    actual = ""
    exists = target.exists()
    if exists:
        actual = target.read_text(encoding="utf-8")
    match = actual.strip() == expected
    metrics = {
        **loop_metrics,
        "target_file": str(target),
        "file_exists": exists,
        "content_match": match,
        "file_content_verbatim": actual,
    }
    passed = ok_loop and exists and match
    return passed, metrics


def level_t3(ctx: dict) -> tuple[bool, dict]:
    client, base_url, headers, model, timeout = ctx["client"], ctx["base_url"], ctx["headers"], ctx["model"], ctx["timeout"]
    workdir: Path = ctx["workdir"]
    script = workdir / "t3_task.py"
    result_file = workdir / "t3_result.txt"
    script.unlink(missing_ok=True)
    result_file.unlink(missing_ok=True)
    task = (
        f"Multi-step task, use the provided tools only:\n"
        f"1. Use write_file to create a Python script at {script} that computes "
        f"sum(i*i for i in range(1, 101)), prints 'SUM=<value>' to stdout, and writes that exact "
        f"same 'SUM=<value>' line to {result_file}.\n"
        f"2. Use run_python to execute {script}.\n"
        "3. After seeing the execution output, end your turn."
    )
    ok_loop, loop_metrics = run_tool_loop(
        client, base_url, headers, model,
        system_prompt=(
            "You complete multi-step tasks strictly by calling the provided tools in sequence. "
            "Never claim success without running the script. End your turn when done."
        ),
        task=task,
        tools=[WRITE_FILE_TOOL, RUN_PYTHON_TOOL, BASH_TOOL],
        workdir=workdir,
        max_rounds=MAX_T3_TOOL_ROUNDS,
        timeout_s=timeout,
    )
    expected_line = f"SUM={T3_EXPECTED_SUM}"
    script_ok = False
    stdout = ""
    if script.exists():
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                cwd=str(workdir),
                timeout=60,
            )
            stdout = (proc.stdout or "").strip()
            script_ok = proc.returncode == 0 and expected_line in stdout
        except Exception as exc:
            stdout = f"harness re-run error: {type(exc).__name__}: {exc}"
    result_ok = result_file.exists() and expected_line in result_file.read_text(encoding="utf-8")
    metrics = {
        **loop_metrics,
        "max_rounds_allowed": MAX_T3_TOOL_ROUNDS,
        "script_path": str(script),
        "script_runs_cleanly": script_ok,
        "script_stdout_verbatim": stdout,
        "result_file": str(result_file),
        "result_file_has_expected_line": result_ok,
        "expected_line": expected_line,
    }
    passed = (
        ok_loop
        and not metrics["hit_round_cap"]
        and metrics["tool_use_count"] <= MAX_T3_TOOL_ROUNDS
        and script.exists()
        and script_ok
        and result_ok
    )
    return passed, metrics


LEVELS = {
    "t1": ("streaming deltas + first-token latency", level_t1),
    "t2": ("single-step tool_use creates correct file", level_t2),
    "t3": ("multi-step mini loop <=10 tool_use, closed", level_t3),
}


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_hybrid_flip.py",
        description=(
            "Live verify harness for the gateway transport flip browser -> hybrid. "
            f"Targets ANTHROPIC_BASE_URL (default {DEFAULT_BASE_URL}) with ANTHROPIC_API_KEY from env."
        ),
    )
    parser.add_argument(
        "--level",
        choices=["t1", "t2", "t3", "all"],
        default="all",
        help="verification level to run (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        metavar="N",
        help="per-request/level timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"override gateway base URL (default: $ANTHROPIC_BASE_URL or {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("CLAUDE_DEFAULT_MODEL", "claude-3-5-sonnet"),
        help="model field sent to the gateway",
    )
    args = parser.parse_args(argv)

    base_url = args.base_url or os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "sk-webgpt-local")
    model = args.model

    levels = list(LEVELS) if args.level == "all" else [args.level]
    needs_workdir = any(lv in levels for lv in ("t2", "t3"))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    runtime_root = Path(os.environ.get("WEBGPT_RUNTIME_ROOT") or (Path.home() / ".local" / "share" / "webgpt"))
    workdir = runtime_root / "tmp" / f"verify_hybrid_flip_{stamp}"
    if needs_workdir:
        workdir.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("VERIFY HYBRID FLIP HARNESS")
    print(f"  base_url : {base_url}")
    print(f"  model    : {model}")
    print(f"  levels   : {', '.join(levels)}  timeout: {args.timeout}s")
    if needs_workdir:
        print(f"  workdir  : {workdir}")
    print("=" * 72)

    headers = build_headers(api_key)
    results: dict[str, bool] = {}
    metrics_out: dict[str, dict] = {}
    timeout = httpx.Timeout(connect=min(5.0, args.timeout), read=args.timeout, write=args.timeout, pool=args.timeout)
    with httpx.Client(timeout=timeout) as client:
        ctx = {
            "client": client,
            "base_url": base_url,
            "headers": headers,
            "model": model,
            "timeout": args.timeout,
            "workdir": workdir,
        }
        for lv in levels:
            label, fn = LEVELS[lv]
            print(f"\n--- {lv.upper()}: {label} ---")
            started = time.monotonic()
            try:
                passed, metrics = fn(ctx)
            except Exception as exc:  # never hang, always report
                passed, metrics = False, {"harness_exception": f"{type(exc).__name__}: {exc}"}
            wall = round(time.monotonic() - started, 3)
            results[lv] = bool(passed)
            metrics_out[lv] = metrics
            status = "PASS" if passed else "FAIL"
            print(f"[{lv.upper()}] {status} ({wall}s)")
            print(f"[{lv.upper()}] RAW METRICS:")
            print(json.dumps(metrics, ensure_ascii=False, indent=2))

    print("\n" + "=" * 72)
    print("SUMMARY")
    for lv in levels:
        print(f"  {lv.upper()}: {'PASS' if results[lv] else 'FAIL'}")
    overall = all(results.values())
    print(f"OVERALL: {'PASS' if overall else 'FAIL'}")
    print("=" * 72)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
