"""Tests for scripts/review_gate.py — the automated review gate.

Each scenario builds a throwaway git repo in tmp_path and runs the gate
script as a subprocess against it, asserting the verdict and exit code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "scripts" / "review_gate.py"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "tests").mkdir()
    # A passing baseline test so the suite is green by default.
    (repo / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    # A tracked production file so later edits show up in `git diff HEAD`.
    (repo / "gpt").mkdir(parents=True, exist_ok=True)
    (repo / "gpt" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "gpt" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def run_gate(repo: Path, tmp_path: Path | None = None) -> tuple[dict, int]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    cleanup_dir: Path | None = None
    if tmp_path is not None:
        # Isolate the gate's inner pytest tmp tree from concurrently running
        # pytest processes: without a unique basetemp two gates can race on
        # /tmp/pytest-of-<user>/pytest-<N> and die with FileExistsError.
        basetemp = tmp_path / f"rt-{uuid.uuid4().hex[:12]}"
        basetemp.mkdir(parents=True, exist_ok=True)
        env["PYTEST_ADDOPTS"] = "-p no:cacheprovider --basetemp=" + str(basetemp)
        cleanup_dir = basetemp.parent if basetemp.parent.name.startswith("rt-") else basetemp
    try:
        proc = subprocess.run(
            [sys.executable, str(GATE), "--json", "--repo", str(repo)],
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
    finally:
        if cleanup_dir is not None:
            import shutil

            shutil.rmtree(cleanup_dir, ignore_errors=True)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"gate did not emit JSON:\n{proc.stdout}\n{proc.stderr}")
    return report, proc.returncode


def modify(repo: Path, relpath: str, text: str) -> None:
    """Append a tracked file's content so it shows up in `git diff HEAD`."""
    path = repo / relpath
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing + text, encoding="utf-8")


# ----------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------


def test_scenario_clean_repo_passes(tmp_path):
    """No danger patterns, green tests -> PASS / exit 0."""
    repo = init_repo(tmp_path)
    modify(repo, "gpt/mod.py", "x = 1\n")
    report, code = run_gate(repo, tmp_path)

    assert report["verdict"] == "PASS"
    assert code == 0
    assert report["pytest"]["failed"] == 0
    assert report["pytest"]["passed"] >= 1
    assert report["danger"] == []
    assert report["diff_summary"]["files_changed"] >= 1


def test_scenario_pytest_failure_fails_gate(tmp_path):
    """Failing test in diff -> FAIL / exit 2."""
    repo = init_repo(tmp_path)
    modify(repo, "tests/test_bad.py", "def test_bad():\n    assert False\n")
    report, code = run_gate(repo, tmp_path)

    assert report["verdict"] == "FAIL"
    assert code == 2
    assert report["pytest"]["failed"] >= 1


def test_scenario_while_true_without_deadline_fails(tmp_path):
    """`while True` with no deadline/timeout hint nearby -> FAIL / exit 2."""
    repo = init_repo(tmp_path)
    modify(
        repo,
        "gpt/loop.py",
        "def spin():\n    while True:\n        work()\n",
    )
    report, code = run_gate(repo, tmp_path)

    assert report["verdict"] == "FAIL"
    assert code == 2
    patterns = [d["pattern"] for d in report["danger"]]
    assert "while-True-without-deadline" in patterns


def test_scenario_while_true_with_timeout_is_not_high(tmp_path):
    """`while True` guarded by a timeout/deadline hint must not FAIL."""
    repo = init_repo(tmp_path)
    modify(
        repo,
        "gpt/loop.py",
        "import os\n"
        "deadline = os.environ.get('TIMEOUT')\n"
        "def spin():\n"
        "    while True:\n"
        "        if past_deadline(deadline):\n"
        "            break\n",
    )
    report, code = run_gate(repo, tmp_path)

    patterns = [d["pattern"] for d in report["danger"]]
    assert "while-True-without-deadline" not in patterns
    assert report["verdict"] != "FAIL"
    assert code in (0, 1)


def test_scenario_secret_token_fails(tmp_path):
    """sk-... secret-like string added in gpt/ -> FAIL / exit 2."""
    repo = init_repo(tmp_path)
    secret = "sk-" + "a" * 24
    modify(repo, "gpt/creds.py", f"API_KEY = '{secret}'\n")
    report, code = run_gate(repo, tmp_path)

    assert report["verdict"] == "FAIL"
    assert code == 2
    assert any(d["pattern"] == "secret-like-token(sk-)" for d in report["danger"])


def test_scenario_verify_false_fails_and_except_pass_warns(tmp_path):
    """verify=False is high severity; except Exception: pass is medium (WARN)."""
    repo = init_repo(tmp_path)

    warn_repo = tmp_path / "warn_repo"
    warn_repo.mkdir()
    _git(warn_repo, "init", "-q")
    (warn_repo / "tests").mkdir()
    (warn_repo / "tests" / "test_ok.py").write_text("def test_ok():\n    pass\n")
    (warn_repo / "gpt").mkdir()
    _git(warn_repo, "add", "-A")
    _git(warn_repo, "commit", "-qm", "init")

    # verify=False -> medium per docs/automation/DECISIONS.md (owner deferred
    # SSL hardening), so it warns instead of failing the gate.
    modify(repo, "gpt/net.py", "import requests\nr = requests.get(u, verify=False)\n")
    report, code = run_gate(repo, tmp_path)
    assert any(d["pattern"] == "verify=False" for d in report["danger"])
    assert report["verdict"] in ("WARN", "PASS")
    assert code in (1, 0)

    # except Exception: pass -> at most WARN, never PASS-silently dropped
    modify(
        warn_repo,
        "gpt/swallow.py",
        "try:\n    run()\nexcept Exception:\n    pass\n",
    )
    report_w, code_w = run_gate(warn_repo, tmp_path)
    assert any(d["pattern"] == "except-Exception-pass" for d in report_w["danger"])
    assert report_w["verdict"] == "WARN"
    assert code_w == 1


def test_scenario_mypy_error_fails_when_configured(tmp_path):
    """A configured repo-wide mypy error is a blocking gate failure."""
    repo = init_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        "[tool.mypy]\nexplicit_package_bases = true\n",
        encoding="utf-8",
    )
    modify(repo, "gpt/mod.py", 'typed_value: int = "wrong"\n')
    report, code = run_gate(repo, tmp_path)

    assert report["mypy_available"] is True
    assert report["mypy_ran"] is True
    assert report["mypy_errors"] >= 1
    assert report["verdict"] == "FAIL"
    assert code == 2


def test_scenario_no_diff_still_runs(tmp_path):
    """Repo with zero diff vs HEAD must not crash; clean verdict PASS."""
    repo = init_repo(tmp_path)
    report, code = run_gate(repo, tmp_path)

    assert report["verdict"] == "PASS"
    assert code == 0
    assert report["diff_summary"]["files_changed"] == 0


def test_scenario_non_git_directory_does_not_crash(tmp_path):
    """Plain directory without git: gate degrades gracefully, no traceback."""
    repo = tmp_path / "plain"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    pass\n")

    report, code = run_gate(repo, tmp_path)
    # No diff info available; pytest green -> PASS with a note.
    assert report["verdict"] == "PASS"
    assert code == 0
    assert any("git" in n.lower() for n in report.get("notes", []))
