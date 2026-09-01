"""Harness tests for scripts/practical_cli_bench.py.

These never invoke the real claude CLI nor a real gateway.  They exercise the
fixture builders, the AST measurement helpers and every grader against both
correct and incorrect project trees (real subprocesses only where they are
cheap: pytest inside tmp fixtures and the notes CLI itself).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, cast

import pytest

import scripts.practical_cli_bench as bench
from scripts.practical_cli_bench import (
    BUGFIX_PROMPT,
    FEATURE_PROMPT,
    REFACTOR_PROMPT,
    TASKS,
    CommandResult,
    build_calc_project,
    build_claude_env_from,
    build_notes_project,
    build_shop_project,
    changed_files,
    count_function_lines,
    grade_bugfix,
    grade_feature,
    grade_refactor,
    parse_pytest_pass_count,
    run_task,
    top_level_functions,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeRunner:
    """Scripted command runner keyed by (argv[0], argv[1])."""

    def __init__(self, responses: dict[tuple[str, ...], CommandResult]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(
        self,
        cmd: list[str],
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(cmd)
        return self.responses.get(
            tuple(cmd[:2]), CommandResult(returncode=1, stderr="unexpected command")
        )


GREEN_PYTEST = CommandResult(0, stdout="........\n8 passed in 0.02s\n")
RED_PYTEST = CommandResult(1, stdout="3 failed, 5 passed in 0.02s\n")


@pytest.fixture()
def workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="practical-bench-test-"))


def apply_calc_fix(project_dir: Path) -> None:
    ops = project_dir / "calc" / "ops.py"
    text = ops.read_text(encoding="utf-8")
    ops.write_text(text.replace("total // len(values)", "total / len(values)"), encoding="utf-8")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def test_bugfix_prompt_pins_rules() -> None:
    low = BUGFIX_PROMPT.lower()
    assert "failing" in low
    assert "tests/" in BUGFIX_PROMPT or "tests" in low
    assert "root cause" in low
    assert "style" in low


def test_feature_prompt_pins_search_semantics() -> None:
    assert '--search <keyword>' in FEATURE_PROMPT
    assert "case-insensitive" in FEATURE_PROMPT.lower()
    assert "exit 0" in FEATURE_PROMPT
    assert "empty" in FEATURE_PROMPT.lower()
    assert "--file" in FEATURE_PROMPT


def test_refactor_prompt_pins_api_and_behaviour() -> None:
    assert "(items, customer=None)" in REFACTOR_PROMPT
    assert "python -m pytest" in REFACTOR_PROMPT
    assert "public api" in REFACTOR_PROMPT.lower()


def test_task_registry_covers_three_tasks() -> None:
    assert set(TASKS) == {"bugfix", "feature", "refactor"}
    for spec in TASKS.values():
        assert spec.prompt.strip(), spec.name


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def test_build_calc_layout(workdir: Path) -> None:
    baseline = build_calc_project(workdir / "calc-proj")
    rels = set(baseline)
    assert {"pyproject.toml", "calc/ops.py", "tests/test_ops.py"} <= rels
    assert "total // len(values)" in (workdir / "calc-proj/calc/ops.py").read_text()


def test_fixture_bugs_and_greens(workdir: Path) -> None:
    """Real pytest: bugfix suite must fail, feature/refactor suites must pass."""
    results = {}
    for name, builder in (
        ("bugfix", build_calc_project),
        ("feature", build_notes_project),
        ("refactor", build_shop_project),
    ):
        proj = workdir / name
        builder(proj)
        proc = bench._default_runner(bench.pytest_cmd(), proj, 120)
        results[name] = proc.returncode
    assert results == {"bugfix": 1, "feature": 0, "refactor": 0}


def test_process_order_baseline_is_long_enough() -> None:
    lines = count_function_lines(bench.SHOP_PRICING_SRC, "process_order")
    assert lines is not None and lines >= 60


# ---------------------------------------------------------------------------
# Hash diff helpers
# ---------------------------------------------------------------------------

def test_snapshot_and_changed_files(workdir: Path) -> None:
    proj = workdir / "p"
    baseline = build_calc_project(proj)
    assert changed_files(proj, baseline) == []

    apply_calc_fix(proj)
    assert changed_files(proj, baseline) == ["calc/ops.py"]

    (proj / "extra.txt").write_text("hi", encoding="utf-8")
    (proj / "__pycache__" / "ops.cpython-312.pyc").parent.mkdir(parents=True)
    (proj / "__pycache__" / "ops.cpython-312.pyc").write_bytes(b"\x00")
    (proj / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    changed = changed_files(proj, baseline)
    assert "extra.txt" in changed
    assert not any("__pycache__" in c or c.endswith(".pyc") for c in changed)
    assert not any(".pytest_cache" in c for c in changed)


def test_changed_files_detects_deletion(workdir: Path) -> None:
    proj = workdir / "p"
    baseline = build_calc_project(proj)
    (proj / "calc" / "ops.py").unlink()
    assert "calc/ops.py" in changed_files(proj, baseline)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def test_count_function_lines_basic() -> None:
    src = "def a():\n    x = 1\n    y = 2\n\n\ndef b():\n    pass\n"
    assert count_function_lines(src, "a") == 3
    assert count_function_lines(src, "b") == 2
    assert count_function_lines(src, "missing") is None
    assert count_function_lines("def broken(:", "broken") is None


def test_top_level_functions_signatures() -> None:
    sigs = top_level_functions(bench.SHOP_PRICING_SRC)
    assert sigs == {"process_order": ["items", "customer"]}
    assert top_level_functions("not python !!!") == {}


# ---------------------------------------------------------------------------
# Bugfix grader
# ---------------------------------------------------------------------------

def _bugfix_runner(pytest_result: CommandResult) -> FakeRunner:
    return FakeRunner({tuple(bench.pytest_cmd()[:2]): pytest_result})


def test_grade_bugfix_good_tree(workdir: Path) -> None:
    proj = workdir / "good"
    baseline = build_calc_project(proj)
    apply_calc_fix(proj)
    results = grade_bugfix(proj, baseline, runner=_bugfix_runner(GREEN_PYTEST))
    assert all(r.passed for r in results), [(r.name, r.detail) for r in results]
    assert {r.name for r in results} == {
        "bugfix_pytest_green",
        "bugfix_tests_untouched",
        "bugfix_only_logic_changed",
    }


def test_grade_bugfix_unfixed_fails_pytest_only(workdir: Path) -> None:
    proj = workdir / "unfixed"
    baseline = build_calc_project(proj)
    results = grade_bugfix(proj, baseline, runner=_bugfix_runner(RED_PYTEST))
    by_name = {r.name: r for r in results}
    assert not by_name["bugfix_pytest_green"].passed
    assert by_name["bugfix_tests_untouched"].passed
    assert by_name["bugfix_only_logic_changed"].passed  # nothing changed at all


def test_grade_bugfix_tampered_tests_detected(workdir: Path) -> None:
    proj = workdir / "cheater"
    baseline = build_calc_project(proj)
    # "Fix" the bug by gutting the failing tests instead.
    (proj / "tests" / "test_ops.py").write_text("def test_nothing():\n    assert True\n", encoding="utf-8")
    results = grade_bugfix(proj, baseline, runner=_bugfix_runner(GREEN_PYTEST))
    by_name = {r.name: r for r in results}
    assert not by_name["bugfix_tests_untouched"].passed
    assert not by_name["bugfix_only_logic_changed"].passed
    assert by_name["bugfix_pytest_green"].passed  # suite itself is green -- cheating caught elsewhere


def test_grade_bugfix_real_fix_passes_end_to_end(workdir: Path) -> None:
    """Full mechanical loop without claude: fix applied -> real pytest green."""
    proj = workdir / "real"
    baseline = build_calc_project(proj)
    apply_calc_fix(proj)
    results = grade_bugfix(proj, baseline)  # real default runner
    assert all(r.passed for r in results), [(r.name, r.detail) for r in results]


# ---------------------------------------------------------------------------
# Feature grader
# ---------------------------------------------------------------------------

FEATURE_SOLUTION_CLI = '''\
"""Command line interface for the notes demo project."""

import argparse
import sys

from notes.store import load_notes, save_notes


def build_parser():
    parser = argparse.ArgumentParser(prog="notes")
    parser.add_argument("--file", default="notes.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="add a note")
    add_parser.add_argument("title")
    add_parser.add_argument("--body", default="")

    list_parser = subparsers.add_parser("list", help="list note titles")
    list_parser.add_argument("--limit", type=int, default=None)
    list_parser.add_argument("--search", default=None, help="filter by title")

    count_parser = subparsers.add_parser("count")

    remove_parser = subparsers.add_parser("remove")
    remove_parser.add_argument("title")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.command == "add":
        notes = load_notes(args.file)
        notes.append({"title": args.title, "body": args.body})
        save_notes(args.file, notes)
        print(f"added: {args.title}")
        return 0

    if args.command == "list":
        notes = load_notes(args.file)
        titles = [note["title"] for note in notes]
        if args.search is not None:
            needle = args.search.lower()
            titles = [t for t in titles if needle in t.lower()]
        if args.limit is not None:
            titles = titles[: args.limit]
        for title in titles:
            print(title)
        return 0

    if args.command == "count":
        print(len(load_notes(args.file)))
        return 0

    if args.command == "remove":
        notes = load_notes(args.file)
        remaining = [note for note in notes if note["title"] != args.title]
        save_notes(args.file, remaining)
        print(f"removed: {args.title}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
'''


def test_grade_feature_with_solution(workdir: Path) -> None:
    proj = workdir / "feat-good"
    build_notes_project(proj)
    (proj / "notes" / "cli.py").write_text(FEATURE_SOLUTION_CLI, encoding="utf-8")
    results = grade_feature(proj)  # real runner: real subprocess CLI + real pytest
    failed = [(r.name, r.detail) for r in results if not r.passed]
    assert not failed, failed
    assert {r.name for r in results} == {
        "feature_search_match",
        "feature_search_no_match",
        "feature_search_empty_file",
        "feature_old_tests_green",
    }


def test_grade_feature_without_feature_fails_cases(workdir: Path) -> None:
    proj = workdir / "feat-bad"
    build_notes_project(proj)  # stock CLI has no --search
    results = grade_feature(proj)
    by_name = {r.name: r for r in results}
    assert not by_name["feature_search_match"].passed
    assert not by_name["feature_search_no_match"].passed
    assert not by_name["feature_search_empty_file"].passed
    assert by_name["feature_old_tests_green"].passed  # old behaviour intact


def test_grade_feature_old_tests_red_detected(workdir: Path) -> None:
    proj = workdir / "feat-regress"
    build_notes_project(proj)
    (proj / "notes" / "cli.py").write_text(FEATURE_SOLUTION_CLI, encoding="utf-8")
    runner = FakeRunner({tuple(bench.pytest_cmd()[:2]): RED_PYTEST})
    results = grade_feature(proj, runner=runner)
    by_name = {r.name: r for r in results}
    assert not by_name["feature_old_tests_green"].passed
    # The three CLI cases still ran through the injected runner's canned path.
    assert sum(1 for r in results if r.name.startswith("feature_search_")) == 3


# ---------------------------------------------------------------------------
# Refactor grader
# ---------------------------------------------------------------------------

REFACTORED_PRICING = '''\
"""Order pricing for the shop demo project."""

STUDENT_DISCOUNT = 0.10
MEMBER_DISCOUNT = 0.15
BULK_THRESHOLD = 5
BULK_DISCOUNT = 0.05
GIFT_WRAP_FEE = 2.0
SHIPPING_FLAT = 5.0
FREE_SHIPPING_LIMIT = 50.0
TAX_RATE = 0.08

VALID_CATEGORIES = frozenset({"food", "book", "toy", "electronics"})


def _line_total(price, quantity):
    total = price * quantity
    if quantity >= BULK_THRESHOLD:
        total -= total * BULK_DISCOUNT
    return total


def _order_subtotal(items):
    subtotal = 0.0
    for item in items:
        category = item["category"]
        if category not in VALID_CATEGORIES:
            raise ValueError(f"unknown category: {category!r}")
        subtotal += _line_total(item["price"], item["quantity"])
    return subtotal


def _customer_discount(subtotal, customer):
    discount = 0.0
    if customer.get("member"):
        discount += subtotal * MEMBER_DISCOUNT
    if customer.get("student"):
        discount += subtotal * STUDENT_DISCOUNT
    return discount


def _shipping(net, items):
    if net < FREE_SHIPPING_LIMIT and items:
        return SHIPPING_FLAT
    return 0.0


def _gift_wrap_fee(customer, items):
    if customer.get("gift_wrap"):
        return GIFT_WRAP_FEE * len(items)
    return 0.0


def process_order(items, customer=None):
    """Compute the final charge for an order (same contract as before)."""
    customer = customer or {}
    subtotal = _order_subtotal(items)
    discount = _customer_discount(subtotal, customer)
    net = subtotal - discount
    shipping = _shipping(net, items)
    gift_wrap_fee = _gift_wrap_fee(customer, items)
    tax = net * TAX_RATE
    total = net + shipping + gift_wrap_fee + tax
    return {
        "subtotal": round(subtotal, 2),
        "discount": round(discount, 2),
        "shipping": round(shipping, 2),
        "gift_wrap_fee": round(gift_wrap_fee, 2),
        "tax": round(tax, 2),
        "total": round(total, 2),
    }
'''


def test_grade_refactor_with_good_refactoring(workdir: Path) -> None:
    proj = workdir / "ref-good"
    build_shop_project(proj)
    (proj / "shop" / "pricing.py").write_text(REFACTORED_PRICING, encoding="utf-8")
    results = grade_refactor(proj)  # real runner
    failed = [(r.name, r.detail) for r in results if not r.passed]
    assert not failed, failed


def test_grade_refactor_unchanged_fails_shrink_only(workdir: Path) -> None:
    proj = workdir / "ref-bad"
    build_shop_project(proj)
    results = grade_refactor(proj)
    by_name = {r.name: r for r in results}
    assert by_name["refactor_behaviour_tests_green"].passed
    assert not by_name["refactor_function_shrunk"].passed
    assert by_name["refactor_public_api_preserved"].passed


def test_grade_refactor_renamed_api_detected(workdir: Path) -> None:
    proj = workdir / "ref-rename"
    build_shop_project(proj)
    pricing = proj / "shop" / "pricing.py"
    pricing.write_text(
        pricing.read_text(encoding="utf-8").replace(
            "def process_order(", "def calculate_order("
        ),
        encoding="utf-8",
    )
    results = grade_refactor(proj)
    by_name = {r.name: r for r in results}
    assert not by_name["refactor_public_api_preserved"].passed
    detail = by_name["refactor_public_api_preserved"].detail
    assert "calculate_order" in detail or "process_order" in detail


def test_grade_refactor_signature_change_detected(workdir: Path) -> None:
    proj = workdir / "ref-sig"
    build_shop_project(proj)
    pricing = proj / "shop" / "pricing.py"
    src = pricing.read_text(encoding="utf-8")
    src = src.replace("def process_order(items, customer=None):", "def process_order(items):")
    src = src.replace("    customer = customer or {}\n", "")
    src = src.replace("customer.get(", "{}.get(").replace("customer or {}", "{}")
    pricing.write_text(src, encoding="utf-8")
    results = grade_refactor(proj)
    by_name = {r.name: r for r in results}
    assert not by_name["refactor_public_api_preserved"].passed
    assert "signature" in by_name["refactor_public_api_preserved"].detail


def test_grade_refactor_missing_file_handled(workdir: Path) -> None:
    proj = workdir / "ref-empty"
    proj.mkdir()
    results = grade_refactor(proj, runner=FakeRunner({}))
    {r.name: r for r in results}
    assert all(not r.passed for r in results)


# ---------------------------------------------------------------------------
# Orchestration plumbing
# ---------------------------------------------------------------------------

def test_run_task_skip_claude_grades_directly(workdir: Path) -> None:
    sandbox = workdir / "sandbox"
    spec = TASKS["bugfix"]
    assertions, overall = run_task(
        spec, sandbox, base_url="http://127.0.0.1:9", claude_timeout=5, skip_claude=True
    )
    assert len(assertions) == 3
    assert overall is False  # unfixed fixture cannot pass


def test_run_task_bugfix_baseline_reaches_grader(workdir: Path) -> None:
    """The bugfix grader must receive the fixture hash snapshot via kwargs."""
    captured: dict = {}

    class SpyTask:
        name = "bugfix"
        prompt = "x"

        @staticmethod
        def build(project_dir: Path) -> dict[str, str]:
            return {"a": "1"}

        @staticmethod
        def grade(project_dir: Path, **kwargs) -> list:
            captured.update(kwargs)
            return []

    run_task(cast(Any, SpyTask()), workdir, base_url="http://127.0.0.1:9", claude_timeout=5, skip_claude=True)
    assert captured == {"runner": bench._default_runner, "baseline_hashes": {"a": "1"}}


def test_build_claude_env_points_at_gateway() -> None:
    env = build_claude_env_from({}, "http://127.0.0.1:19999/")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:19999"
    assert env["ANTHROPIC_API_KEY"]
    assert env["CLAUDE_DEFAULT_MODEL"]


def test_parse_pytest_pass_count_variants() -> None:
    assert parse_pytest_pass_count("3 failed, 5 passed in 0.02s") == 5
    assert parse_pytest_pass_count("8 passed in 0.01s") == 8
    assert parse_pytest_pass_count("no tests ran") is None
