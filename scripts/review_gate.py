#!/usr/bin/env python3
"""Automated review gate for the 24/7 loop.

Single command aggregating quality signals:
  - pytest over tests/
  - ruff check over the repository (respecting pyproject exclusions)
  - mypy over the repository when [tool.mypy] is configured
  - git diff stat vs HEAD
  - quick "danger pattern" scan over newly added diff lines

Exit codes: 0 = PASS, 1 = WARN, 2 = FAIL.

Usage:
    python scripts/review_gate.py [--json] [--repo PATH]

Stdlib only. Degrades gracefully when git has no diff, static-analysis tools
are missing, or the repo layout differs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

SEVERITY_ORDER = {"high": 2, "medium": 1}
EXIT_CODES = {"PASS": 0, "WARN": 1, "FAIL": 2}

SECRET_RE = re.compile(r"sk-[a-z0-9]{20,}")
VERIFY_FALSE_RE = re.compile(r"verify\s*=\s*False")
WHILE_TRUE_RE = re.compile(r"\bwhile\s+True\b")
HOME_PATH_RE = re.compile(r"/home/[A-Za-z0-9_.-]{1,32}/")
EXCEPT_PASS_SAME_LINE_RE = re.compile(r"except\s+Exception\s*:\s*pass\b")
EXCEPT_BARE_RE = re.compile(r"except\s+Exception\s*:\s*$")
DEADLINE_HINT_RE = re.compile(
    r"(deadline|timeout|time_limit|time-limit|_LIMIT|max_seconds|"
    r"environ|getenv|break|return|asyncio\.sleep|task\.cancel|sys\.exit)",
    re.IGNORECASE,
)

CONTEXT_WINDOW = 5  # lines around a `while True` to look for a deadline hint


def find_repo_root() -> Path:
    """Repo root: --repo arg, $REVIEW_GATE_REPO, or script's parent dir."""
    env = os.environ.get("REVIEW_GATE_REPO")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# pytest
# --------------------------------------------------------------------------


class PytestResult(TypedDict, total=False):
    passed: int
    failed: int
    errors: int
    returncode: int | None
    ran: bool
    note: str


def run_pytest(repo: Path) -> PytestResult:
    """Run pytest tests/ -q and parse passed/failed/error counts."""
    result: PytestResult = {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "returncode": None,
        "ran": False,
    }
    tests_dir = repo / "tests"
    if not tests_dir.is_dir():
        result["note"] = "tests/ directory not found; pytest skipped"
        return result

    cmd = [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header"]
    try:
        proc = subprocess.run(
            cmd, cwd=str(repo), capture_output=True, text=True, timeout=1800
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # A gate that cannot run the suite must fail closed.
        result["failed"] = 1
        result["note"] = f"pytest could not run: {exc}"
        return result

    output = (proc.stdout or "") + (proc.stderr or "")
    result["ran"] = True
    result["returncode"] = proc.returncode
    m = re.search(r"(\d+) passed", output)
    if m:
        result["passed"] = int(m.group(1))
    m = re.search(r"(\d+) failed", output)
    if m:
        result["failed"] = int(m.group(1))
    m = re.search(r"(\d+) error", output)
    if m:
        result["errors"] = int(m.group(1))
    if (
        proc.returncode not in (0, 1)
        and result["failed"] == 0
        and result["errors"] == 0
    ):
        # Crash / usage error / interrupted: fail closed unless counts say why.
        result["failed"] = 1
        result["note"] = f"pytest exited with unexpected code {proc.returncode}"
    if "no tests ran" in output:
        result["note"] = "no tests collected"
    elif result["failed"] > 0 or result["errors"] > 0:
        # Keep a compact failure tail so automation reports are actionable
        # without rerunning the entire suite just to discover the test name.
        lines = [line for line in output.splitlines() if line.strip()]
        if lines:
            result["note"] = "pytest failure tail:\n" + "\n".join(lines[-30:])
    return result


# --------------------------------------------------------------------------
# ruff
# --------------------------------------------------------------------------


def find_ruff(repo: Path) -> list[str] | None:
    """Locate a usable ruff: .venv first, then PATH."""
    venv_bin = repo / ".venv" / "bin"
    candidates: list[list[str]] = [
        [str(venv_bin / "ruff")],
        [str(venv_bin / "python"), "-m", "ruff"],
        ["ruff"],
    ]
    for cmd in candidates:
        try:
            probe = subprocess.run(
                [*cmd, "--version"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return cmd
    return None


def run_ruff(repo: Path) -> dict:
    """Run repo-wide Ruff using the repository's own configuration/excludes."""
    result: dict = {"available": True, "total": 0, "by_rule": {}, "skipped_reason": None}
    cmd = find_ruff(repo)
    if cmd is None:
        result["available"] = False
        result["skipped_reason"] = "ruff not installed (checked .venv and PATH)"
        return result

    try:
        proc = subprocess.run(
            [*cmd, "check", ".", "--output-format=concise"],
            cwd=str(repo), capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["skipped_reason"] = f"ruff failed to run: {exc}"
        return result

    # Concise format: `path:line:col: CODE message`
    issue_re = re.compile(r"^\S+:\d+:\d+: ([A-Z]+[0-9]+) ")
    for line in (proc.stdout or "").splitlines():
        m = issue_re.match(line)
        if m:
            rule = m.group(1)
            result["total"] += 1
            result["by_rule"][rule] = result["by_rule"].get(rule, 0) + 1
    return result


# --------------------------------------------------------------------------
# mypy
# --------------------------------------------------------------------------


def find_mypy(repo: Path) -> list[str] | None:
    """Locate a usable mypy: .venv first, then PATH."""
    venv_bin = repo / ".venv" / "bin"
    candidates: list[list[str]] = [
        [str(venv_bin / "mypy")],
        [str(venv_bin / "python"), "-m", "mypy"],
        ["mypy"],
    ]
    for cmd in candidates:
        try:
            probe = subprocess.run(
                [*cmd, "--version"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return cmd
    return None


def _has_mypy_config(repo: Path) -> bool:
    pyproject = repo / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    return "[tool.mypy]" in text


def run_mypy(repo: Path) -> dict:
    """Run repo-wide mypy when the repository explicitly configures it."""
    result: dict = {
        "available": True,
        "ran": False,
        "errors": 0,
        "returncode": None,
        "skipped_reason": None,
    }
    if not _has_mypy_config(repo):
        result["available"] = False
        result["skipped_reason"] = "[tool.mypy] not configured; typecheck skipped"
        return result
    cmd = find_mypy(repo)
    if cmd is None:
        result["available"] = False
        result["skipped_reason"] = "mypy not installed (checked .venv and PATH)"
        return result
    try:
        proc = subprocess.run(
            [*cmd, ".", "--no-error-summary"],
            cwd=str(repo), capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["ran"] = True
        result["returncode"] = 2
        result["errors"] = 1
        result["skipped_reason"] = f"mypy failed to run: {exc}"
        return result
    result["ran"] = True
    result["returncode"] = proc.returncode
    output = (proc.stdout or "") + (proc.stderr or "")
    result["errors"] = sum(1 for line in output.splitlines() if ": error:" in line)
    if proc.returncode != 0 and result["errors"] == 0:
        # Config/usage/internal failures are still gate failures, not silent skips.
        result["errors"] = 1
        result["skipped_reason"] = "mypy exited non-zero without parseable type errors"
    return result


# --------------------------------------------------------------------------
# git diff
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


def diff_stat(repo: Path) -> tuple[dict | None, str | None]:
    """Return (summary_dict, error_note). summary None when unavailable."""
    out = _git(repo, "diff", "--stat", "HEAD")
    if out is None:
        return None, "git diff --stat HEAD unavailable (not a repo or no git)"

    # Exact per-file +/- from numstat (binary files show '-' placeholders).
    numstat = _git(repo, "diff", "--numstat", "HEAD") or ""
    stats: dict[str, tuple[int, int]] = {}
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            adds = int(parts[0]) if parts[0].isdigit() else 0
            dels = int(parts[1]) if parts[1].isdigit() else 0
            stats[parts[2]] = (adds, dels)

    files: list[dict] = []
    total_add = total_del = 0
    for line in out.splitlines():
        m = re.match(r"^(.+?)\s+\|\s+(?:Bin\b|\d+)", line)
        if not m:
            continue
        name = m.group(1).strip()
        adds, dels = stats.get(name, (0, 0))
        files.append({"file": name, "additions": adds, "deletions": dels})
        total_add += adds
        total_del += dels
    return {
        "files_changed": len(files),
        "insertions": total_add,
        "deletions": total_del,
        "files": files,
    }, None


def added_lines_with_positions(repo: Path) -> tuple[list[tuple[str, int, str]], list[str]]:
    """Collect (file, new_line_no, text) for every '+' line in the diff.

    Falls back to scanning untracked production files (git status '??'),
    since plain `git diff HEAD` does not cover them.
    """
    added: list[tuple[str, int, str]] = []
    notes: list[str] = []

    out = _git(repo, "diff", "--unified=0", "HEAD")
    if out is None:
        notes.append("git diff unavailable; danger scan limited")
    else:
        current_file = None
        new_line = 0
        for raw in out.splitlines():
            if raw.startswith("+++ b/"):
                current_file = raw[len("+++ b/"):]
                continue
            if raw.startswith("+++ ") or raw.startswith("--- "):
                continue
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if m:
                new_line = int(m.group(1))
                continue
            if raw.startswith("+") and not raw.startswith("+++"):
                if current_file:
                    added.append((current_file, new_line, raw[1:]))
                new_line += 1
            elif raw.startswith("-"):
                continue
            else:
                new_line += 1

    # Untracked files outside tests/scripts/docs are fair game for the scan.
    st = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if st:
        for line in st.splitlines():
            if not line.startswith("??"):
                continue
            path = line[3:].strip()
            # Only scan untracked production code (gpt/); tests/scripts/docs
            # and loose top-level files are out of scope for the gate.
            if not path.startswith("gpt/") or "__pycache__" in path:
                continue
            fp = repo / path
            if fp.is_dir() or not fp.is_file():
                continue
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, text in enumerate(content.splitlines(), start=1):
                added.append((path, i, text))
    return added, notes


# --------------------------------------------------------------------------
# Danger patterns
# --------------------------------------------------------------------------


def scan_danger(added: list[tuple[str, int, str]]) -> list[dict]:
    """Scan added lines for danger patterns; returns findings."""
    # Index lines per file for context lookups (while True deadline check).
    by_file: dict[str, dict[int, str]] = {}
    for f, ln, text in added:
        by_file.setdefault(f, {})[ln] = text

    findings: list[dict] = []

    def report(f: str, ln: int, pattern: str, severity: str) -> None:
        findings.append(
            {"file": f, "line": ln, "pattern": pattern, "severity": severity}
        )

    for f, ln, text in added:
        text.strip()
        if SECRET_RE.search(text):
            report(f, ln, "secret-like-token(sk-)", "high")
        if VERIFY_FALSE_RE.search(text):
            # Downgraded to medium per docs/automation/DECISIONS.md (2026-08-24):
            # the owner explicitly deferred SSL-verification hardening; flagging
            # it as a blocking "high" would permanently fail every gate run.
            report(f, ln, "verify=False", "medium")
        if HOME_PATH_RE.search(text):
            report(f, ln, "hardcoded-/home/<user>/", "medium")
        if EXCEPT_PASS_SAME_LINE_RE.search(text):
            report(f, ln, "except-Exception-pass", "medium")
        elif EXCEPT_BARE_RE.search(text):
            nxt = by_file.get(f, {}).get(ln + 1, "")
            if nxt.strip() == "pass":
                report(f, ln + 1, "except-Exception-pass", "medium")
        if WHILE_TRUE_RE.search(text):
            lines = by_file.get(f, {})
            window = range(max(1, ln - CONTEXT_WINDOW), ln + CONTEXT_WINDOW + 1)
            has_deadline = any(
                DEADLINE_HINT_RE.search(lines.get(i, "")) for i in window
            )
            if not has_deadline:
                report(f, ln, "while-True-without-deadline", "high")
            else:
                report(f, ln, "while-True-with-deadline-hint", "none")

    # Drop informational entries; keep them out of blocking logic but the
    # caller may want visibility. We keep severity "none" rows out of JSON
    # noise by filtering here.
    return [f for f in findings if f["severity"] != "none"]


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------


def compute_verdict(
    pytest_res: PytestResult, ruff_res: dict, mypy_res: dict, danger: list[dict]
) -> str:
    high = any(d.get("severity") == "high" for d in danger)
    medium = any(d.get("severity") == "medium" for d in danger)
    if (
        pytest_res.get("failed", 0) > 0
        or pytest_res.get("errors", 0) > 0
        or mypy_res.get("errors", 0) > 0
        or high
    ):
        return "FAIL"
    if ruff_res.get("total", 0) > 0 or medium:
        return "WARN"
    return "PASS"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def run_gate(repo: Path) -> dict:
    pytest_res = run_pytest(repo)
    ruff_res = run_ruff(repo)
    mypy_res = run_mypy(repo)
    diff_summary, diff_note = diff_stat(repo)
    added, notes = added_lines_with_positions(repo)
    if diff_note:
        notes.append(diff_note)
    danger = scan_danger(added)
    if mypy_res.get("skipped_reason"):
        notes.append(str(mypy_res["skipped_reason"]))
    if ruff_res.get("skipped_reason"):
        notes.append(str(ruff_res["skipped_reason"]))
    verdict = compute_verdict(pytest_res, ruff_res, mypy_res, danger)
    report = {
        "verdict": verdict,
        "pytest": {
            "passed": pytest_res["passed"],
            "failed": pytest_res["failed"],
            "errors": pytest_res.get("errors", 0),
            "note": pytest_res.get("note"),
        },
        "ruff_errors": ruff_res.get("total", 0),
        "ruff_by_rule": ruff_res.get("by_rule", {}),
        "ruff_available": ruff_res.get("available", False),
        "mypy_errors": mypy_res.get("errors", 0),
        "mypy_available": mypy_res.get("available", False),
        "mypy_ran": mypy_res.get("ran", False),
        "danger": danger,
        "diff_summary": diff_summary or {"files_changed": 0},
        "notes": notes,
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Automated review gate")
    parser.add_argument("--repo", type=Path, default=None,
                        help="target repo root (default: parent of this script)")
    parser.add_argument("--json", action="store_true",
                        help="print machine-readable JSON only")
    args = parser.parse_args(argv)

    repo = args.repo.resolve() if args.repo else find_repo_root()
    report = run_gate(repo)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        p = report["pytest"]
        d = report["diff_summary"]
        print(f"[review-gate] verdict={report['verdict']}")
        print(f"[review-gate] pytest passed={p['passed']} failed={p['failed']}")
        print(f"[review-gate] ruff_errors={report['ruff_errors']}")
        print(f"[review-gate] mypy_errors={report['mypy_errors']}")
        print(
            f"[review-gate] diff files={d.get('files_changed', 0)} "
            f"+{d.get('insertions', 0)}/-{d.get('deletions', 0)}"
        )
        for item in report["danger"]:
            print(
                f"[review-gate] DANGER {item['severity']}: "
                f"{item['file']}:{item['line']} {item['pattern']}"
            )
        for note in report["notes"]:
            print(f"[review-gate] note: {note}")
        print(json.dumps(report, indent=2, ensure_ascii=False))

    return EXIT_CODES[report["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
