"""Pure-logic tests for scripts/bench/e2e_project_benchmark.py.

These never spawn the real claude CLI or a real gateway: they exercise prompt
construction, assertion parsing on fixture directory trees, and port picking.
"""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

from scripts.bench.e2e_project_benchmark import (
    CommandResult,
    build_scenario_prompt,
    detect_escaped_paths,
    find_free_port,
    parse_pytest_pass_count,
    run_assertions,
    snapshot_dir,
)

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


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def test_prompt_contains_all_required_requirements() -> None:
    prompt = build_scenario_prompt()
    for token in REQUIRED_PROMPT_TOKENS:
        assert token in prompt, f"prompt missing requirement: {token}"


def test_prompt_forbids_touching_outside_cwd() -> None:
    prompt = build_scenario_prompt().lower()
    assert "only inside" in prompt
    assert "outside" in prompt


def test_prompt_demands_five_passing_tests() -> None:
    prompt = build_scenario_prompt()
    assert "5" in prompt
    assert "pass" in prompt.lower()


# ---------------------------------------------------------------------------
# Assertion engine on fixture trees
# ---------------------------------------------------------------------------

class FakeRunner:
    """Scripted command runner: maps (argv[0], argv[1]) to canned results."""

    def __init__(self, responses: dict[tuple[str, str], CommandResult]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], cwd: Path, timeout: float) -> CommandResult:
        self.calls.append(cmd)
        key = (cmd[0], cmd[1]) if len(cmd) >= 2 else (cmd[0] if cmd else "", "")
        return self.responses.get(key, CommandResult(returncode=1, stderr="unexpected"))


PASS_RESPONSES = {
    ("pytest", "-q"): CommandResult(0, stdout="........................  7 passed in 0.12s\n"),
    ("git", "log"): CommandResult(0, stdout="abc1234 initial commit\n"),
}


@pytest.fixture()
def good_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    pkg = project / "taskmanager"
    pkg.mkdir(parents=True)
    (project / "README.md").write_text("# taskmanager\n\nA demo project.\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname = 'taskmanager'\n", encoding="utf-8")
    for name in ("__init__", "models", "storage", "cli"):
        (pkg / f"{name}.py").write_text("", encoding="utf-8")
    tests = project / "tests"
    tests.mkdir()
    (tests / "test_smoke.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    return project


@pytest.fixture()
def incomplete_project(tmp_path: Path) -> Path:
    """Pass case minus storage.py, README, manifest and git history."""
    project = tmp_path / "project"
    pkg = project / "taskmanager"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text("", encoding="utf-8")
    (pkg / "cli.py").write_text("", encoding="utf-8")
    return project


def test_run_assertions_all_pass(good_project: Path) -> None:
    results = run_assertions(good_project, runner=FakeRunner(PASS_RESPONSES))
    by_name = {r.name: r for r in results}
    assert {r.name for r in results} == {
        "readme_nonempty",
        "dependency_manifest",
        "package_layout",
        "pytest_suite_5_passed",
        "git_commit_exists",
    }
    failed = [name for name, r in by_name.items() if not r.passed]
    assert failed == [], failed


def test_run_assertions_detects_missing_files(incomplete_project: Path) -> None:
    runner = FakeRunner(
        {
            ("pytest", "-q"): CommandResult(0, stdout="2 passed in 0.01s\n"),
            ("git", "log"): CommandResult(128, stderr="fatal: not a git repository"),
        }
    )
    results = run_assertions(incomplete_project, runner=runner)
    by_name = {r.name: r for r in results}
    assert not by_name["readme_nonempty"].passed
    assert not by_name["dependency_manifest"].passed
    assert not by_name["package_layout"].passed
    assert "storage.py" in by_name["package_layout"].detail
    # pytest exits 0 but only 2 passed -> still fails the >=5 gate
    assert not by_name["pytest_suite_5_passed"].passed
    assert not by_name["git_commit_exists"].passed


def test_run_assertions_failing_pytest_exit(good_project: Path) -> None:
    runner = FakeRunner(
        {
            ("pytest", "-q"): CommandResult(1, stdout="1 failed, 6 passed in 0.3s\n"),
            ("git", "log"): CommandResult(0, stdout="abc1234 init\n"),
        }
    )
    by_name = {r.name: r for r in run_assertions(good_project, runner=runner)}
    assert not by_name["pytest_suite_5_passed"].passed
    assert "exit=1" in by_name["pytest_suite_5_passed"].detail


def test_run_assertions_counts_tests_not_prose(good_project: Path) -> None:
    runner = FakeRunner(
        {
            # exit 0 but fewer than 5 tests collected
            ("pytest", "-q"): CommandResult(0, stdout="3 passed in 0.02s\n"),
            ("git", "log"): CommandResult(0, stdout="abc1234 init\n"),
        }
    )
    by_name = {r.name: r for r in run_assertions(good_project, runner=runner)}
    assert not by_name["pytest_suite_5_passed"].passed


def test_run_assertions_runs_pytest_and_git(good_project: Path) -> None:
    runner = FakeRunner(PASS_RESPONSES)
    run_assertions(good_project, runner=runner)
    commands = [cmd[0] for cmd in runner.calls]
    assert "pytest" in commands
    assert "git" in commands


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_parse_pytest_pass_count_variants() -> None:
    assert parse_pytest_pass_count("=========== 5 passed in 0.10s ===========") == 5
    assert parse_pytest_pass_count("12 passed, 1 warning") == 12
    assert parse_pytest_pass_count("no tests ran") is None
    assert parse_pytest_pass_count("") is None


def test_find_free_port_is_bindable_and_distinct() -> None:
    first = find_free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", first))
        sock.listen(1)
        second = find_free_port()
        assert second != first  # must not hand out an already-bound port


def test_snapshot_and_escape_detection(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    before = snapshot_dir(tmp_path)
    assert detect_escaped_paths(tmp_path, before) == []

    # claude stayed inside cwd: sandbox unchanged
    (project / "taskmanager").mkdir()
    assert detect_escaped_paths(tmp_path, before) == []

    # something appeared next to the project dir: escape detected
    (tmp_path / "stray.txt").write_text("leaked", encoding="utf-8")
    assert detect_escaped_paths(tmp_path, before) == ["stray.txt"]
