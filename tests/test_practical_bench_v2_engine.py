from pathlib import Path

from benchmarks.practical.grade import (
    _changed_paths,
    _check_diff_confinement,
    _check_locked_paths,
    _count_asserts,
)
from scripts.run_practical_bench import discover_tasks


def test_dynamic_task_discovery_contains_v1_tasks():
    assert {"bugfix", "feature", "refactor"} <= set(discover_tasks())


def test_diff_confinement_and_locked_paths(tmp_path: Path):
    fixture = tmp_path / "fixture"
    ws = tmp_path / "ws"
    for root in (fixture, ws):
        (root / "pkg").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "pkg" / "a.py").write_text("x=1\n")
        (root / "tests" / "locked.py").write_text("assert True\n")
    (ws / "pkg" / "a.py").write_text("x=2\n")
    assert _changed_paths(fixture, ws) == {"pkg/a.py"}
    ok = _check_diff_confinement({"allowed_globs": ["pkg/**"]}, fixture, ws)
    assert ok and ok[0]
    locked = _check_locked_paths({"locked_paths": ["tests/locked.py"]}, fixture, ws)
    assert locked and locked[0]
    (ws / "tests" / "locked.py").write_text("assert False\n")
    locked = _check_locked_paths({"locked_paths": ["tests/locked.py"]}, fixture, ws)
    assert locked and not locked[0]


def test_count_asserts_handles_real_and_invalid_python(tmp_path: Path):
    good = tmp_path / "good.py"
    good.write_text("def test_x():\n    assert 1\n    assert 2\n")
    assert _count_asserts(good) == 2
    bad = tmp_path / "bad.py"
    bad.write_text("def ???")
    assert _count_asserts(bad) == 0
