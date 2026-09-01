#!/usr/bin/env python3
"""Practical CLI benchmark: three everyday tasks solved by Claude Code CLI
through the webgpt gateway, auto-graded by a mechanical harness that never
trusts the model's prose.

Tasks (increasing difficulty)
-----------------------------
bugfix   : project ``calc`` ships one real bug (integer division where a true
           division is required) and a failing pytest suite.  Claude must find
           and fix it without touching tests.
feature  : project ``notes`` is a working read/write-JSON CLI.  Claude must add
           a ``--search <keyword>`` flag to the ``list`` subcommand.
refactor : project ``shop`` has a ~60 line repetitive function.  Claude must
           split it into small helpers with identical behavior.

Grading philosophy: every criterion is verified mechanically -- pytest exit
codes, subprocess invocations of the produced CLI, AST measurements of the
edited source, and hash diffs of the file tree.  Model stdout is only reported,
never graded.

Modes
-----
default        : target a live gateway (default http://127.0.0.1:18000).
                 /health is probed first; if the gateway is down the script
                 exits early with remediation instructions.
--mock-gateway : spawn a PRIVATE gateway instance on a random free port with
                 ``--mock-backend`` (browser-free, deterministic, no ChatGPT
                 quota), wait for /health, run claude against it, then tear it
                 down.  Used to smoke-verify the harness itself.  Assertions
                 are expected to FAIL in this mode because the mock backend
                 does not actually solve anything.

Exit codes: 0 all selected tasks passed all assertions; 1 one or more
assertions failed or a claude run failed; 2 gateway unreachable / environment
problem.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
CLAUDE_BIN = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
DEFAULT_GATEWAY_URL = os.environ.get("WEBGPT_GATEWAY_URL", "http://127.0.0.1:18000")
DEFAULT_API_KEY = os.environ.get("WEBGPT_API_KEY", "sk-webgpt-local")
DEFAULT_MODEL = os.environ.get("E2E_CLAUDE_MODEL", "claude-3-5-sonnet")
HEALTH_WAIT_SECONDS = 60.0
PYTEST_TIMEOUT = 300.0
CLI_TIMEOUT = 60.0

# Sandbox holds built projects (can be tens of MB) and is disposable, so it
# must NEVER land on tmpfs (/tmp).  Default scratch root is under ~/Downloads;
# $WEBGPT_SCRATCH_ROOT overrides.  Removed after the run unless --keep-workdir.
SCRATCH_ROOT_ENV = "WEBGPT_SCRATCH_ROOT"
DEFAULT_SCRATCH_ROOT = Path.home() / "Downloads" / "practical-cli-bench-scratch"


def make_sandbox() -> Path:
    base = Path(os.environ.get(SCRATCH_ROOT_ENV) or DEFAULT_SCRATCH_ROOT)
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="practical-cli-bench-", dir=str(base)))

# Cache/bytecode noise that appears whenever someone runs pytest/python inside
# the fixture; never counts as a "changed file" for diff-based assertions.
IGNORED_PATH_PARTS = ("__pycache__", ".pytest_cache")
IGNORED_SUFFIXES = (".pyc", ".pyo")


def venv_python() -> str:
    return str(VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable))


def pytest_cmd(extra: list[str] | None = None) -> list[str]:
    cmd = [venv_python(), "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    return cmd + list(extra or [])


# ---------------------------------------------------------------------------
# Generic plumbing
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


class Runner(Protocol):
    def __call__(
        self,
        cmd: list[str],
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...


def parse_pytest_pass_count(output: str) -> int | None:
    match = re.search(r"(\d+)\s+passed", output)
    return int(match.group(1)) if match else None


@dataclass
class AssertionResult:
    name: str
    passed: bool
    detail: str


# ---------------------------------------------------------------------------
# Fixture templates
# ---------------------------------------------------------------------------

CALC_PYPROJECT = """\
[project]
name = "calc"
version = "1.0.0"
requires-python = ">=3.10"

[tool.pytest.ini_options]
pythonpath = ["."]
"""

CALC_OPS_SRC = '''\
"""Basic arithmetic operations for the calc demo project."""


def add(a, b):
    """Return the sum of ``a`` and ``b``."""
    return a + b


def subtract(a, b):
    """Return ``a`` minus ``b``."""
    return a - b


def multiply(a, b):
    """Return the product of ``a`` and ``b``."""
    return a * b


def average(values):
    """Return the arithmetic mean of ``values``.

    Raises ValueError when ``values`` is empty.
    """
    if not values:
        raise ValueError("average() requires at least one value")
    total = 0
    for value in values:
        total += value
    return total // len(values)
'''

CALC_TEST_SRC = '''\
"""Tests for calc.ops -- these MUST keep passing after any bug fix."""

import pytest

from calc.ops import add, average, multiply, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(7, 4) == 3


def test_multiply():
    assert multiply(6, 7) == 42


def test_average_of_integers():
    assert average([1, 2, 4]) == pytest.approx(7 / 3)


def test_average_single_value():
    assert average([7]) == 7


def test_average_of_floats():
    assert average([0.5, 1.0]) == pytest.approx(0.75)


def test_average_with_negatives():
    assert average([-1, 2]) == pytest.approx(0.5)


def test_average_empty_raises():
    with pytest.raises(ValueError):
        average([])
'''

CALC_INIT = '"""calc demo project."""\n'

NOTES_STORE_SRC = '''\
"""JSON-file backed note storage for the notes demo project."""

import json
from pathlib import Path


def load_notes(path):
    """Return the list of notes stored in ``path`` ([] when missing)."""
    notes_file = Path(path)
    if not notes_file.exists():
        return []
    with notes_file.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return [dict(item) for item in data]


def save_notes(path, notes):
    """Write ``notes`` (list of dicts) to ``path`` as pretty JSON."""
    with Path(path).open("w", encoding="utf-8") as handle:
        json.dump(notes, handle, indent=2, ensure_ascii=False)
        handle.write("\\n")
'''

NOTES_CLI_SRC = '''\
"""Command line interface for the notes demo project."""

import argparse
import sys

from notes.store import load_notes, save_notes


def build_parser():
    parser = argparse.ArgumentParser(prog="notes")
    parser.add_argument(
        "--file",
        default="notes.json",
        help="path to the notes JSON file (default: notes.json)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="add a note")
    add_parser.add_argument("title", help="note title")
    add_parser.add_argument("--body", default="", help="optional note body")

    list_parser = subparsers.add_parser("list", help="list note titles")
    list_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="show at most N titles",
    )

    count_parser = subparsers.add_parser("count", help="print number of notes")

    remove_parser = subparsers.add_parser("remove", help="remove a note by title")
    remove_parser.add_argument("title", help="exact title to remove")

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

    print(f"unknown command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
'''

NOTES_TEST_SRC = '''\
"""Existing behaviour tests for the notes CLI -- must always stay green."""

import json

import pytest

from notes.cli import main
from notes.store import load_notes, save_notes


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "notes.json"
    save_notes(path, [{"title": "A", "body": "x"}])
    assert load_notes(path) == [{"title": "A", "body": "x"}]


def test_load_missing_file_returns_empty_list(tmp_path):
    assert load_notes(tmp_path / "nope.json") == []


def test_cli_add_then_list(tmp_path, capsys):
    path = tmp_path / "notes.json"
    assert main(["--file", str(path), "add", "Buy milk"]) == 0
    capsys.readouterr()  # drop the "added:" confirmation line
    assert main(["--file", str(path), "list"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Buy milk"]


def test_cli_count_and_remove(tmp_path, capsys):
    path = tmp_path / "notes.json"
    main(["--file", str(path), "add", "A"])
    main(["--file", str(path), "add", "B"])
    capsys.readouterr()
    assert main(["--file", str(path), "count"]) == 0
    assert capsys.readouterr().out.strip() == "2"
    assert main(["--file", str(path), "remove", "A"]) == 0
    assert [n["title"] for n in load_notes(path)] == ["B"]


def test_cli_list_limit(tmp_path, capsys):
    path = tmp_path / "notes.json"
    save_notes(path, [{"title": f"n{i}", "body": ""} for i in range(5)])
    assert main(["--file", str(path), "list", "--limit", "2"]) == 0
    assert capsys.readouterr().out.splitlines() == ["n0", "n1"]
'''

SHOP_PRICING_SRC = '''\
"""Order pricing for the shop demo project."""

STUDENT_DISCOUNT = 0.10
MEMBER_DISCOUNT = 0.15
BULK_THRESHOLD = 5
BULK_DISCOUNT = 0.05
GIFT_WRAP_FEE = 2.0
SHIPPING_FLAT = 5.0
FREE_SHIPPING_LIMIT = 50.0
TAX_RATE = 0.08


def process_order(items, customer=None):
    """Compute the final charge for an order.

    items: list of dicts with keys name, price, quantity, category.
    customer: optional dict with boolean keys student, member, gift_wrap.
    Returns a dict with subtotal, discount, shipping, gift_wrap_fee, tax and
    total (all rounded to 2 decimals).
    """
    customer = customer or {}

    subtotal = 0.0
    for item in items:
        price = item["price"]
        quantity = item["quantity"]
        category = item["category"]

        if category == "food":
            line_total = price * quantity
            if quantity >= BULK_THRESHOLD:
                line_total -= line_total * BULK_DISCOUNT
            subtotal += line_total
        elif category == "book":
            line_total = price * quantity
            if quantity >= BULK_THRESHOLD:
                line_total -= line_total * BULK_DISCOUNT
            subtotal += line_total
        elif category == "toy":
            line_total = price * quantity
            if quantity >= BULK_THRESHOLD:
                line_total -= line_total * BULK_DISCOUNT
            subtotal += line_total
        elif category == "electronics":
            line_total = price * quantity
            if quantity >= BULK_THRESHOLD:
                line_total -= line_total * BULK_DISCOUNT
            subtotal += line_total
        else:
            raise ValueError(f"unknown category: {category!r}")

    discount = 0.0
    if customer.get("member"):
        discount += subtotal * MEMBER_DISCOUNT
    if customer.get("student"):
        discount += subtotal * STUDENT_DISCOUNT

    net = subtotal - discount

    shipping = 0.0
    if net < FREE_SHIPPING_LIMIT and items:
        shipping = SHIPPING_FLAT

    gift_wrap_fee = 0.0
    if customer.get("gift_wrap"):
        gift_wrap_fee = GIFT_WRAP_FEE * len(items)

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

SHOP_TEST_SRC = '''\
"""Behaviour contract for shop.pricing.process_order -- must not change."""

import pytest

from shop.pricing import process_order


def item(name, price, quantity, category):
    return {"name": name, "price": price, "quantity": quantity, "category": category}


def test_empty_order():
    result = process_order([])
    assert result == {
        "subtotal": 0,
        "discount": 0,
        "shipping": 0,
        "gift_wrap_fee": 0,
        "tax": 0,
        "total": 0,
    }


def test_single_food_item_no_bulk():
    result = process_order([item("apple", 2.0, 3, "food")])
    assert result["subtotal"] == pytest.approx(6.0)
    assert result["discount"] == pytest.approx(0.0)
    assert result["shipping"] == pytest.approx(5.0)
    assert result["tax"] == pytest.approx(0.48)
    assert result["total"] == pytest.approx(11.48)


def test_bulk_discount_applies_at_threshold():
    result = process_order([item("rice", 3.0, 5, "food")])
    assert result["subtotal"] == pytest.approx(14.25)  # 15 * 0.95
    assert result["total"] == pytest.approx(14.25 + 5.0 + 14.25 * 0.08)


def test_member_and_student_discounts_stack():
    result = process_order(
        [item("novel", 10.0, 10, "book")], customer={"member": True, "student": True}
    )
    assert result["subtotal"] == pytest.approx(95.0)
    assert result["discount"] == pytest.approx(23.75)  # 9.5 + 14.25
    assert result["shipping"] == pytest.approx(0.0)  # net >= 50
    assert result["tax"] == pytest.approx((95.0 - 23.75) * 0.08)
    assert result["total"] == pytest.approx(71.25 + 71.25 * 0.08)


def test_free_shipping_threshold():
    # net exactly 50 -> free shipping kicks in (condition is strictly below).
    result = process_order([item("cable", 25.0, 2, "electronics")])
    assert result["shipping"] == pytest.approx(0.0)
    assert result["total"] == pytest.approx(54.0)

    # just below the limit -> flat shipping applies.
    result = process_order([item("cable", 24.0, 2, "electronics")])
    assert result["subtotal"] == pytest.approx(48.0)
    assert result["shipping"] == pytest.approx(5.0)


def test_gift_wrap_fee_per_line_item():
    order = [item("bear", 5.0, 1, "toy"), item("car", 7.0, 2, "toy")]
    plain = process_order(order)
    wrapped = process_order(order, customer={"gift_wrap": True})
    assert wrapped["gift_wrap_fee"] == pytest.approx(4.0)  # 2 lines * 2.0
    assert wrapped["total"] == pytest.approx(plain["total"] + 4.0)


def test_mixed_categories():
    result = process_order(
        [
            item("apple", 2.0, 3, "food"),
            item("novel", 10.0, 1, "book"),
            item("bear", 5.0, 1, "toy"),
            item("cable", 25.0, 2, "electronics"),
        ]
    )
    assert result["subtotal"] == pytest.approx(6.0 + 10.0 + 5.0 + 50.0)
    assert result["shipping"] == pytest.approx(0.0)


def test_unknown_category_raises():
    with pytest.raises(ValueError):
        process_order([item("rock", 1.0, 1, "mineral")])


def test_customer_none_means_no_discounts():
    result = process_order([item("apple", 2.0, 3, "food")], customer=None)
    assert result["discount"] == pytest.approx(0.0)
'''

GENERIC_INIT = ""

FEATURE_CASES: tuple[tuple[str, tuple[str, ...], str, list[str]], ...] = (
    # (case name, note titles, keyword, expected stdout titles)
    ("match", ("Buy milk", "buy dog food", "Plan vacation"), "buy", ["Buy milk", "buy dog food"]),
    ("no_match", ("Buy milk", "Plan vacation"), "zebra", []),
    ("empty_file", (), "anything", []),
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_hashes(project_dir: Path) -> dict[str, str]:
    """Map every relevant file (relative path) to its content hash."""
    hashes: dict[str, str] = {}
    for path in sorted(project_dir.rglob("*")):
        rel = path.relative_to(project_dir).as_posix()
        if path.is_dir() or _is_noise(rel):
            continue
        hashes[rel] = _sha256(path.read_text(encoding="utf-8", errors="replace"))
    return hashes


def _is_noise(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    return (
        any(part in IGNORED_PATH_PARTS for part in parts)
        or rel_path.endswith(IGNORED_SUFFIXES)
    )


def changed_files(project_dir: Path, baseline: dict[str, str]) -> list[str]:
    """Relative paths that were added, deleted or modified vs ``baseline``."""
    current = snapshot_hashes(project_dir)
    changed = {rel for rel, digest in baseline.items() if current.get(rel) != digest}
    changed |= set(current) - set(baseline)
    return sorted(changed)


def build_calc_project(project_dir: Path) -> dict[str, str]:
    _write(project_dir / "pyproject.toml", CALC_PYPROJECT)
    _write(project_dir / "calc" / "__init__.py", CALC_INIT)
    _write(project_dir / "calc" / "ops.py", CALC_OPS_SRC)
    _write(project_dir / "tests" / "test_ops.py", CALC_TEST_SRC)
    return snapshot_hashes(project_dir)


def build_notes_project(project_dir: Path) -> dict[str, str]:
    _write(project_dir / "pyproject.toml", CALC_PYPROJECT.replace('"calc"', '"notes"'))
    _write(project_dir / "notes" / "__init__.py", GENERIC_INIT)
    _write(project_dir / "notes" / "store.py", NOTES_STORE_SRC)
    _write(project_dir / "notes" / "cli.py", NOTES_CLI_SRC)
    _write(project_dir / "tests" / "test_notes_cli.py", NOTES_TEST_SRC)
    return snapshot_hashes(project_dir)


def build_shop_project(project_dir: Path) -> dict[str, str]:
    _write(project_dir / "pyproject.toml", CALC_PYPROJECT.replace('"calc"', '"shop"'))
    _write(project_dir / "shop" / "__init__.py", GENERIC_INIT)
    _write(project_dir / "shop" / "pricing.py", SHOP_PRICING_SRC)
    _write(project_dir / "tests" / "test_pricing.py", SHOP_TEST_SRC)
    return snapshot_hashes(project_dir)


# ---------------------------------------------------------------------------
# AST measurement helpers
# ---------------------------------------------------------------------------

def count_function_lines(source: str, func_name: str) -> int | None:
    """Physical source span (lines) of top-level ``func_name``, or None."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return (node.end_lineno or node.lineno) - node.lineno + 1
    return None


def top_level_functions(source: str) -> dict[str, list[str]]:
    """Map top-level function name -> ordered positional arg names."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    signatures: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signatures[node.name] = [arg.arg for arg in node.args.args]
    return signatures


# ---------------------------------------------------------------------------
# Task prompts
# ---------------------------------------------------------------------------

BUGFIX_PROMPT = """\
You are working inside the current directory: a tiny Python project named \
`calc`. Its pytest suite is FAILING right now. Find the bug in the production \
code and fix it, keeping the existing code style exactly (same naming, same \
docstring style, same simplicity).

Rules:
- Fix the ROOT CAUSE in the production code. Do NOT change any file under \
`tests/`, do NOT weaken or delete tests, do NOT change any public function \
signature or its documented behaviour.
- Run `python -m pytest` yourself to confirm every test passes before finishing.
- Do not touch anything outside this directory.

When done, print a short summary of what the bug was and how you fixed it."""

FEATURE_PROMPT = """\
You are working inside the current directory: a tiny Python project named \
`notes`. It is a command-line tool that reads and writes a JSON file of notes \
(see `notes/cli.py`). Add a new feature to the `list` subcommand:

    notes --file <path> list --search <keyword>

Semantics (must be implemented EXACTLY like this):
- `--search KEYWORD` filters notes by TITLE with a case-insensitive substring \
match ("buy" matches both "Buy milk" and "buy dog food").
- Matching titles are printed in FILE ORDER, one per line, nothing else.
- When nothing matches, print NOTHING (no header, no message) and exit 0.
- It must also work on an empty notes file (`[]`) and on a missing notes file.
- Without `--search`, `list` behaves exactly as before. Do NOT change any \
existing flag, command or behaviour.

Verify your work: run the existing suite (`python -m pytest`, all must pass) \
and try the CLI by hand on sample data. Keep the existing code style. Print a \
short summary when done."""

REFACTOR_PROMPT = """\
You are working inside the current directory: a tiny Python project named \
`shop`. In `shop/pricing.py`, the function `process_order` is about sixty \
lines long and full of copy-pasted blocks. Refactor it into small focused \
helper functions so the repetition disappears.

Hard requirements:
- BEHAVIOUR MUST NOT CHANGE: run `python -m pytest`; every existing test must \
keep passing, unmodified. Do NOT edit anything under `tests/`.
- PUBLIC API MUST NOT CHANGE: the module must still define `process_order` \
with the same signature `(items, customer=None)` and the same return value, \
plus all existing module-level constants.
- Keep the existing code style.

When done, print a short summary of how you structured the refactoring."""


# ---------------------------------------------------------------------------
# Graders
# ---------------------------------------------------------------------------

BUGFIX_ALLOWED_FILES = frozenset({"calc/ops.py"})
BUGFIX_TEST_FILES = frozenset({"tests/test_ops.py"})


def grade_bugfix(
    project_dir: Path,
    baseline_hashes: dict[str, str],
    runner: Runner = _default_runner,
    timeout: float = PYTEST_TIMEOUT,
) -> list[AssertionResult]:
    results: list[AssertionResult] = []

    proc = runner(pytest_cmd(), project_dir, timeout)
    passed = parse_pytest_pass_count(proc.stdout) if proc.returncode == 0 else None
    tail = (proc.stdout or proc.stderr).strip().splitlines()
    results.append(
        AssertionResult(
            "bugfix_pytest_green",
            proc.returncode == 0 and passed is not None and passed > 0,
            f"exit={proc.returncode} passed={passed}"
            + (f" | {tail[-1]}" if tail else ""),
        )
    )

    changed = changed_files(project_dir, baseline_hashes)
    touched_tests = sorted(set(changed) & BUGFIX_TEST_FILES)
    results.append(
        AssertionResult(
            "bugfix_tests_untouched",
            not touched_tests,
            "test files clean" if not touched_tests else f"modified: {', '.join(touched_tests)}",
        )
    )

    disallowed = sorted(set(changed) - BUGFIX_ALLOWED_FILES)
    results.append(
        AssertionResult(
            "bugfix_only_logic_changed",
            not disallowed,
            "diff confined to calc/ops.py"
            if not disallowed
            else f"unexpected changes: {', '.join(disallowed)}",
        )
    )
    return results


def grade_feature(
    project_dir: Path,
    runner: Runner = _default_runner,
    timeout: float = CLI_TIMEOUT,
    pytest_timeout: float = PYTEST_TIMEOUT,
) -> list[AssertionResult]:
    results: list[AssertionResult] = []
    python = venv_python()

    for case_name, titles, keyword, expected in FEATURE_CASES:
        rel_name = f"_grade_{case_name}.json"
        notes_path = project_dir / rel_name
        notes_path.write_text(
            json.dumps([{"title": t, "body": f"body of {t}"} for t in titles], indent=2),
            encoding="utf-8",
        )
        cmd = [python, "-m", "notes.cli", "--file", rel_name, "list", "--search", keyword]
        proc = runner(cmd, project_dir, timeout)
        got = [line for line in proc.stdout.splitlines() if line.strip()]
        ok = proc.returncode == 0 and got == list(expected)
        detail = f"exit={proc.returncode} kw={keyword!r} got={got}"
        if not ok:
            detail += f" expected={list(expected)}"
        results.append(AssertionResult(f"feature_search_{case_name}", ok, detail))

    proc = runner(pytest_cmd(), project_dir, pytest_timeout)
    passed = parse_pytest_pass_count(proc.stdout) if proc.returncode == 0 else None
    results.append(
        AssertionResult(
            "feature_old_tests_green",
            proc.returncode == 0 and passed is not None and passed > 0,
            f"exit={proc.returncode} passed={passed}",
        )
    )
    return results


REFACTOR_SHRINK_RATIO = 0.5


def grade_refactor(
    project_dir: Path,
    runner: Runner = _default_runner,
    timeout: float = PYTEST_TIMEOUT,
) -> list[AssertionResult]:
    results: list[AssertionResult] = []

    proc = runner(pytest_cmd(), project_dir, timeout)
    passed = parse_pytest_pass_count(proc.stdout) if proc.returncode == 0 else None
    results.append(
        AssertionResult(
            "refactor_behaviour_tests_green",
            proc.returncode == 0 and passed is not None and passed > 0,
            f"exit={proc.returncode} passed={passed}",
        )
    )

    pricing_path = project_dir / "shop" / "pricing.py"
    src = pricing_path.read_text(encoding="utf-8", errors="replace") if pricing_path.is_file() else ""
    baseline_lines = count_function_lines(SHOP_PRICING_SRC, "process_order") or 0
    new_lines = count_function_lines(src, "process_order")
    max_allowed = baseline_lines * REFACTOR_SHRINK_RATIO
    ok = new_lines is not None and baseline_lines > 0 and new_lines <= max_allowed
    results.append(
        AssertionResult(
            "refactor_function_shrunk",
            ok,
            f"process_order {baseline_lines} -> "
            f"{new_lines if new_lines is not None else '?'} lines "
            f"(need <= {max_allowed:.0f})",
        )
    )

    original_api = top_level_functions(SHOP_PRICING_SRC)
    new_api = top_level_functions(src)
    missing = sorted(set(original_api) - set(new_api))
    sig_changed = new_api.get("process_order") != original_api.get("process_order")
    ok = not missing and not sig_changed
    detail = f"process_order{original_api.get('process_order', [])} preserved"
    if missing:
        detail = f"missing functions: {', '.join(missing)}"
    elif sig_changed:
        detail = (
            f"process_order signature changed: "
            f"{original_api.get('process_order')} -> {new_api.get('process_order')}"
        )
    results.append(AssertionResult("refactor_public_api_preserved", ok, detail))
    return results


# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskSpec:
    name: str
    prompt: str
    build: Callable[[Path], dict[str, str]]
    grade: Callable[..., list[AssertionResult]]


TASKS: dict[str, TaskSpec] = {
    "bugfix": TaskSpec("bugfix", BUGFIX_PROMPT, build_calc_project, grade_bugfix),
    "feature": TaskSpec("feature", FEATURE_PROMPT, build_notes_project, grade_feature),
    "refactor": TaskSpec("refactor", REFACTOR_PROMPT, build_shop_project, grade_refactor),
}


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


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return sock.getsockname()[1]


class MockGateway:
    """A private browser-free gateway instance used by --mock-gateway."""

    def __init__(self) -> None:
        self.port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        python = venv_python()
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


def build_claude_env_from(base_env: Mapping[str, str], base_url: str) -> dict[str, str]:
    env = dict(base_env)
    env["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
    env.setdefault("ANTHROPIC_API_KEY", DEFAULT_API_KEY)
    env.setdefault("CLAUDE_DEFAULT_MODEL", DEFAULT_MODEL)
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    env.setdefault("DISABLE_TELEMETRY", "1")
    env.setdefault("DISABLE_ERROR_REPORTING", "1")
    return env


def run_claude(
    prompt: str,
    project_dir: Path,
    timeout_s: float,
    base_url: str,
    runner: Runner = _default_runner,
    claude_bin: str | None = None,
) -> tuple[CommandResult, float]:
    cmd = [
        claude_bin or CLAUDE_BIN,
        "-p",
        prompt,
        "--dangerously-skip-permissions",
        "--print",
    ]
    env = build_claude_env_from(os.environ, base_url)
    started = time.monotonic()
    result = runner(cmd, project_dir, timeout_s, env=env)
    return result, time.monotonic() - started


# ---------------------------------------------------------------------------
# Orchestration and reporting
# ---------------------------------------------------------------------------

def print_task_report(
    task: str,
    assertions: list[AssertionResult],
    total_seconds: float,
    response_chars: int,
    claude_ok: bool,
) -> bool:
    overall = all(item.passed for item in assertions) and claude_ok
    print(f"\n========== TASK '{task}' REPORT ==========")
    print(f"{'criterion':<36} {'result':<7} detail")
    print("-" * 88)
    for item in assertions:
        status = "PASS" if item.passed else "FAIL"
        print(f"{item.name:<36} {status:<7} {item.detail}")
    print("-" * 88)
    print(f"{'claude run exit ok':<36} {'PASS' if claude_ok else 'FAIL':<7}")
    print(f"{'total wall time':<36} {total_seconds:>5.1f}s")
    print(f"{'response characters':<36} {response_chars}")
    print(f"{'TASK OVERALL':<36} {'PASS' if overall else 'FAIL'}")
    return overall


def run_task(
    spec: TaskSpec,
    sandbox_dir: Path,
    base_url: str,
    claude_timeout: float,
    runner: Runner = _default_runner,
    claude_bin: str | None = None,
    skip_claude: bool = False,
) -> tuple[list[AssertionResult], bool]:
    project_dir = sandbox_dir / spec.name
    project_dir.mkdir(parents=True)
    baseline_hashes = spec.build(project_dir)
    print(f"[harness] task '{spec.name}' fixture ready at {project_dir}")

    started = time.monotonic()
    claude_ok = True
    response_chars = 0
    if skip_claude:
        print("[harness] skipping claude run (grading fixtures directly)")
    else:
        if not Path(claude_bin or CLAUDE_BIN).exists():
            raise RuntimeError(f"claude CLI not found at {claude_bin or CLAUDE_BIN}")
        result, elapsed = run_claude(
            spec.prompt, project_dir, claude_timeout, base_url,
            runner=runner, claude_bin=claude_bin,
        )
        response_chars = len(result.stdout)
        claude_ok = result.returncode == 0
        print(
            f"[harness] claude finished '{spec.name}' in {elapsed:.1f}s "
            f"(exit={result.returncode}, {response_chars} stdout chars)"
        )
        if result.stderr.strip():
            print(f"[harness] claude stderr preview: {result.stderr.strip()[:300]}")

    kwargs: dict = {"runner": runner}
    if spec.name == "bugfix":
        kwargs["baseline_hashes"] = baseline_hashes
    assertions = spec.grade(project_dir, **kwargs)
    total = time.monotonic() - started
    overall = print_task_report(spec.name, assertions, total, response_chars, claude_ok)
    return assertions, overall


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--task",
        choices=(*TASKS, "all"),
        default="all",
        help="which practical task to run (default: all)",
    )
    parser.add_argument(
        "--mock-gateway",
        action="store_true",
        help="spawn a private mock-backend gateway on a random port instead of "
        "targeting the shared live gateway (harness self-test; assertions are "
        "expected to FAIL)",
    )
    parser.add_argument("--timeout", type=float, default=1800.0, help="per-task claude timeout in seconds")
    parser.add_argument("--base-url", default=None, help=f"gateway base URL (default {DEFAULT_GATEWAY_URL})")
    parser.add_argument("--keep-workdir", action="store_true", help="keep temporary fixture directories")
    args = parser.parse_args(argv)

    selected = list(TASKS) if args.task == "all" else [args.task]
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
    outcomes: dict[str, bool] = {}
    try:
        for name in selected:
            _, outcomes[name] = run_task(TASKS[name], sandbox, base_url, args.timeout)
        print("\n================ PRACTICAL CLI BENCH SUMMARY ================")
        for name in selected:
            print(f"  {name:<10} {'PASS' if outcomes[name] else 'FAIL'}")
        print("=============================================================")
        return 0 if all(outcomes.values()) else 1
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
