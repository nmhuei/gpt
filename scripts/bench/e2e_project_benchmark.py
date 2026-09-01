#!/usr/bin/env python3
"""E2E benchmark: Claude Code CLI -> webgpt gateway -> ChatGPT Web.

Claude is asked to actually BUILD a software project (files, tests, git) inside
a temporary working directory.  The harness then asserts on the produced
artifacts -- it never trusts the model's prose.

Modes
-----
default        : target a live gateway (default http://127.0.0.1:18000).
                 /health is probed first; if the gateway is down the script
                 exits early with remediation instructions.
--mock-gateway : spawn a PRIVATE gateway instance on a random free port with
                 ``--mock-backend`` (browser-free, deterministic, no ChatGPT
                 quota), wait for /health, run claude against it, then tear it
                 down.  Used to smoke-verify the harness itself.

Exit codes: 0 all assertions passed; 1 one or more assertions failed or claude
run failed; 2 gateway unreachable / environment problem.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
CLAUDE_BIN = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
DEFAULT_GATEWAY_URL = os.environ.get("WEBGPT_GATEWAY_URL", "http://127.0.0.1:18000")
DEFAULT_API_KEY = os.environ.get("WEBGPT_API_KEY", "sk-webgpt-local")
DEFAULT_MODEL = os.environ.get("E2E_CLAUDE_MODEL", "claude-3-5-sonnet")
HEALTH_WAIT_SECONDS = 60.0
PYTEST_TIMEOUT = 300.0
GIT_TIMEOUT = 60.0

# Sandbox holds a full built project (can be tens of MB) and is disposable, so
# it must NEVER land on tmpfs (/tmp).  Default scratch root is under ~/Downloads;
# $WEBGPT_SCRATCH_ROOT overrides.  Removed after the run unless --keep-workdir.
SCRATCH_ROOT_ENV = "WEBGPT_SCRATCH_ROOT"
DEFAULT_SCRATCH_ROOT = Path.home() / "Downloads" / "e2e-project-bench-scratch"


def make_sandbox() -> Path:
    base = Path(os.environ.get(SCRATCH_ROOT_ENV) or DEFAULT_SCRATCH_ROOT)
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="e2e-taskmanager-", dir=str(base)))


# ---------------------------------------------------------------------------
# Scenario prompt
# ---------------------------------------------------------------------------

REQUIRED_PROMPT_TOKENS = (
    "taskmanager",
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "taskmanager/models.py",
    "taskmanager/storage.py",
    "taskmanager/cli.py",
    "pytest",
    "Makefile",
    "git init",
)


def build_scenario_prompt() -> str:
    """Return the full English scenario prompt handed to Claude Code CLI."""
    return """\
You are an autonomous senior software engineer. Your current working directory \
is an EMPTY project directory that has been prepared for you. Build a complete, \
professional Python software project in it named `taskmanager`. Do not ask any \
questions and do not wait for confirmation: use your tools to create every file \
and run every command yourself, right now.

STRICT RULES:
- Work ONLY inside the current working directory. Never create, modify or delete \
anything outside it.
- Do not merely describe the project in text: you must CREATE all files on disk \
with your file tools and RUN the commands yourself.
- When you are done, print a short summary of what you built.

DELIVERABLES (all of them are mandatory):

1. README.md - project overview, installation instructions, usage examples and \
a "Running the tests" section. It must be substantive, not empty.
2. pyproject.toml (preferred) or requirements.txt declaring the project name \
`taskmanager` and its dependencies/metadata.
3. A Python package `taskmanager/` containing at least these modules:
   - taskmanager/__init__.py
   - taskmanager/models.py - a `Task` dataclass (id, title, priority, done) plus \
any helpful helpers such as `to_dict`/`from_dict`.
   - taskmanager/storage.py - JSON-file-backed storage class (`TaskStorage`) with \
add/list/get/complete operations.
   - taskmanager/cli.py - an argparse-based command line interface with at least \
`add` and `list` subcommands, plus a `main()` entry point.
4. A pytest suite under `tests/` with AT LEAST 5 test functions that ALL PASS. \
Cover both `models.py` and `storage.py` (use tmp_path fixtures for file storage). \
Run `pytest` yourself and make sure it exits 0 with 5+ passed before finishing.
5. A Makefile with working `test` and `lint` targets (test runs pytest; lint may \
invoke ruff if available, otherwise a python -m compileall fallback).
6. Version control: run `git init`, configure a local user if needed \
(`git config user.email` / `user.name` locally in this repo only), `git add` all \
project files and create the first commit with a descriptive message.

VERIFICATION CHECKLIST (the harness will check each of these mechanically):
- README.md exists and is non-empty
- pyproject.toml or requirements.txt exists
- taskmanager/models.py, taskmanager/storage.py, taskmanager/cli.py exist
- `pytest` exits 0 with at least 5 tests passed
- `git log` shows at least one commit
- no files were created outside the current working directory

Begin now."""


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _default_runner(
    cmd: list[str],
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
) -> CommandResult:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env if env is not None else os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return CommandResult(returncode=-1, stderr=f"timeout after {timeout}s")
    except FileNotFoundError as exc:
        return CommandResult(returncode=127, stderr=str(exc))
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def parse_pytest_pass_count(output: str) -> int | None:
    """Extract the number of passed tests from a short pytest summary line."""
    match = re.search(r"(\d+)\s+passed", output)
    return int(match.group(1)) if match else None


def find_free_port() -> int:
    """Ask the kernel for a currently free TCP port (IPv4 loopback)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return sock.getsockname()[1]


@dataclass
class AssertionResult:
    name: str
    passed: bool
    detail: str


PACKAGE_MODULES = ("models", "storage", "cli")


def _package_layout_ok(project_dir: Path) -> tuple[bool, str]:
    missing = [
        f"taskmanager/{name}.py"
        for name in PACKAGE_MODULES
        if not (project_dir / "taskmanager" / f"{name}.py").is_file()
    ]
    if missing:
        return False, f"missing modules: {', '.join(missing)}"
    return True, "taskmanager/models.py, taskmanager/storage.py, taskmanager/cli.py present"


def run_assertions(
    project_dir: Path,
    runner=_default_runner,
    pytest_timeout: float = PYTEST_TIMEOUT,
) -> list[AssertionResult]:
    """Mechanically verify every deliverable of the scenario.

    ``runner`` is injectable so unit tests can exercise the logic without
    executing real pytest/git processes.
    """
    results: list[AssertionResult] = []
    project_dir = project_dir.resolve()

    # 1. README non-empty
    readme = project_dir / "README.md"
    readme_text = readme.read_text(encoding="utf-8", errors="replace").strip() if readme.is_file() else ""
    results.append(
        AssertionResult(
            "readme_nonempty",
            bool(readme_text),
            f"{len(readme_text)} chars" if readme_text else "README.md missing or empty",
        )
    )

    # 2. dependency manifest
    manifest = next(
        (name for name in ("pyproject.toml", "requirements.txt") if (project_dir / name).is_file()),
        None,
    )
    results.append(
        AssertionResult(
            "dependency_manifest",
            manifest is not None,
            manifest or "neither pyproject.toml nor requirements.txt found",
        )
    )

    # 3. package layout
    ok, detail = _package_layout_ok(project_dir)
    results.append(AssertionResult("package_layout", ok, detail))

    # 4. pytest suite passes with >= 5 tests
    proc = runner(["pytest", "-q"], project_dir, pytest_timeout)
    passed = parse_pytest_pass_count(proc.stdout) if proc.returncode == 0 else None
    ok = proc.returncode == 0 and passed is not None and passed >= 5
    tail = (proc.stdout or proc.stderr).strip().splitlines()
    results.append(
        AssertionResult(
            "pytest_suite_5_passed",
            ok,
            f"exit={proc.returncode} passed={passed}"
            + (f" | {tail[-1]}" if tail else ""),
        )
    )

    # 5. git history has at least one commit
    proc = runner(["git", "log", "--oneline"], project_dir, GIT_TIMEOUT)
    commits = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()] if proc.returncode == 0 else []
    results.append(
        AssertionResult("git_commit_exists", len(commits) >= 1, f"{len(commits)} commit(s)")
    )

    return results


def snapshot_dir(path: Path) -> set[str]:
    return {entry.name for entry in path.iterdir()}


def detect_escaped_paths(sandbox_dir: Path, expected_entries: set[str]) -> list[str]:
    """Return sandbox-level entries created outside the expected project dirs."""
    return sorted(snapshot_dir(sandbox_dir) - set(expected_entries))


# ---------------------------------------------------------------------------
# Gateway plumbing
# ---------------------------------------------------------------------------

_MOCK_GATEWAY_SNIPPET = """
import sys, uvicorn
from gpt.gateway.server import create_api_app
uvicorn.run(create_api_app(mock_backend=True), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
"""


def fetch_health(base_url: str, timeout: float = 5.0) -> tuple[bool, dict]:
    url = base_url.rstrip("/") + "/health"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
            return response.status == 200 and bool(payload.get("ok")), payload
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False, {}


class MockGateway:
    """A private browser-free gateway instance used by --mock-gateway."""

    def __init__(self) -> None:
        self.port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        python = str(VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable))
        env = os.environ.copy()
        env["WEBGPT_LOCAL_MOCK"] = "1"
        self.proc = subprocess.Popen(
            [python, "-c", _MOCK_GATEWAY_SNIPPET.strip(), str(self.port)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + HEALTH_WAIT_SECONDS
        while time.monotonic() < deadline:
            alive, _ = fetch_health(self.base_url)
            if alive:
                return
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"mock gateway exited early with code {self.proc.returncode}"
                )
            time.sleep(0.5)
        raise RuntimeError(
            f"mock gateway did not become healthy within {HEALTH_WAIT_SECONDS:.0f}s"
        )

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.proc = None


# ---------------------------------------------------------------------------
# Claude invocation and orchestration
# ---------------------------------------------------------------------------

def build_claude_env(base_url: str) -> dict[str, str]:
    return build_claude_env_from(os.environ, base_url)


def build_claude_env_from(
    base_env: Mapping[str, str], base_url: str = DEFAULT_GATEWAY_URL
) -> dict[str, str]:
    """Compose the child environment pointing Claude at the gateway."""
    env = dict(base_env)
    env["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
    env.setdefault("ANTHROPIC_API_KEY", DEFAULT_API_KEY)
    env.setdefault("CLAUDE_DEFAULT_MODEL", DEFAULT_MODEL)
    # Keep the CLI from blocking on statsig/sentry traffic that cannot leave
    # a sandboxed environment; without these the run hangs nondeterministically.
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    env.setdefault("DISABLE_TELEMETRY", "1")
    env.setdefault("DISABLE_ERROR_REPORTING", "1")
    return env


def run_claude(
    prompt: str,
    project_dir: Path,
    timeout_s: float,
    base_url: str = DEFAULT_GATEWAY_URL,
) -> tuple[CommandResult, float]:
    cmd = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--dangerously-skip-permissions",
        "--print",
    ]
    env = build_claude_env_from(os.environ, base_url)
    started = time.monotonic()
    result = _default_runner(cmd, project_dir, timeout_s, env=env)
    return result, time.monotonic() - started


def print_report(
    assertions: list[AssertionResult],
    total_seconds: float,
    response_chars: int,
    claude_ok: bool,
) -> None:
    print("\n================ E2E PROJECT BENCHMARK REPORT ================")
    print(f"{'assertion':<26} {'result':<7} detail")
    print("-" * 78)
    for item in assertions:
        status = "PASS" if item.passed else "FAIL"
        print(f"{item.name:<26} {status:<7} {item.detail}")
    print("-" * 78)
    print(f"claude run exit ok      : {'PASS' if claude_ok else 'FAIL'}")
    print(f"total wall time         : {total_seconds:.1f}s")
    print(f"response characters     : {response_chars}")
    overall = all(item.passed for item in assertions) and claude_ok
    print(f"OVERALL                 : {'PASS' if overall else 'FAIL'}")
    print("==============================================================")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mock-gateway",
        action="store_true",
        help="spawn a private mock-backend gateway on a random port instead of "
        "targeting the shared live gateway",
    )
    parser.add_argument("--timeout", type=float, default=1800.0, help="claude run timeout in seconds")
    parser.add_argument("--base-url", default=None, help=f"gateway base URL (default {DEFAULT_GATEWAY_URL})")
    parser.add_argument("--keep-workdir", action="store_true", help="keep the temporary project directory")
    args = parser.parse_args(argv)

    base_url = (args.base_url or DEFAULT_GATEWAY_URL).rstrip("/")

    if args.mock_gateway:
        gateway = MockGateway()
        try:
            gateway.start()
        except RuntimeError as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            return 2
        base_url = gateway.base_url
        print(f"[harness] mock gateway healthy at {gateway.base_url} (port {gateway.port})")
    else:
        alive, _ = fetch_health(base_url)
        if not alive:
            print(
                f"FATAL: no healthy gateway at {base_url}/health.\n"
                "Start it first, e.g.:  gpt restart\n"
                "(or rerun with --mock-gateway to self-test the harness without ChatGPT quota)",
                file=sys.stderr,
            )
            return 2
        print(f"[harness] live gateway healthy at {base_url}")

    sandbox = make_sandbox()
    project_dir = sandbox / "project"
    project_dir.mkdir()
    before_snapshot = snapshot_dir(sandbox)

    started = time.monotonic()
    claude_result: CommandResult | None = None
    try:
        prompt = build_scenario_prompt()
        if not Path(CLAUDE_BIN).exists():
            print(f"FATAL: claude CLI not found at {CLAUDE_BIN}", file=sys.stderr)
            return 2
        claude_result, claude_elapsed = run_claude(
            prompt, project_dir, args.timeout, base_url=base_url
        )
        print(
            f"[harness] claude finished in {claude_elapsed:.1f}s "
            f"(exit={claude_result.returncode}, {len(claude_result.stdout)} stdout chars)"
        )
        if claude_result.stderr.strip():
            print("[harness] claude stderr (first 500 chars):")
            print(claude_result.stderr.strip()[:500])
        if claude_result.returncode != 0 and claude_result.stdout.strip():
            print("[harness] claude stdout preview (first 400 chars):")
            print(claude_result.stdout.strip()[:400])

        assertions = run_assertions(project_dir)
        escaped = detect_escaped_paths(sandbox, before_snapshot)
        assertions.append(
            AssertionResult(
                "no_escape_from_cwd",
                not escaped,
                "sandbox clean" if not escaped else f"unexpected entries: {', '.join(escaped)}",
            )
        )
        total = time.monotonic() - started
        print_report(assertions, total, len(claude_result.stdout), claude_result.returncode == 0)
        return 0 if (all(a.passed for a in assertions) and claude_result.returncode == 0) else 1
    finally:
        if args.keep_workdir:
            print(f"[harness] workdir kept at {sandbox}")
        else:
            shutil.rmtree(sandbox, ignore_errors=True)
        if args.mock_gateway:
            gateway.stop()
            print("[harness] mock gateway stopped")


if __name__ == "__main__":
    sys.exit(main())
