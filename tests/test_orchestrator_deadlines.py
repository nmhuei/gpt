"""Track 3B: deadline guards + exception propagation + solve-slot semaphore.

All network / browser / claude subprocess calls are fully mocked here —
no real instance, CLI, or HTTP traffic is ever touched.
"""

from __future__ import annotations

import asyncio
import time
import types as py_types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from gpt.orchestrator import race_solver as race_solver_mod
from gpt.orchestrator.race_solver import SwarmRaceSolver
from gpt.orchestrator.session_runner import ClaudeCodeSessionRunner
from gpt.orchestrator.types import ChallengeStatus, ChallengeTask, InstanceNotLiveError


def _make_task(tmp_path: Path, target_url: str | None = "http://target.invalid") -> ChallengeTask:
    return ChallengeTask(directory=tmp_path, name="Deadlined Challenge", target_url=target_url)


@pytest.fixture()
def always_offline(monkeypatch):
    """Force every urllib request to fail like an unreachable container."""

    def _fail(*args, **kwargs):
        raise OSError("[Errno -3] connection refused (fake)")

    monkeypatch.setattr(urllib.request, "urlopen", _fail)


# ---------------------------------------------------------------------------
# B1.1 — ensure_instance_live deadline
# ---------------------------------------------------------------------------


def test_ensure_instance_live_raises_after_max_wait(always_offline, tmp_path: Path):
    runner = ClaudeCodeSessionRunner(_make_task(tmp_path))

    start = time.monotonic()
    with pytest.raises(InstanceNotLiveError) as exc_info:
        asyncio.run(runner.ensure_instance_live(max_wait_seconds=0.5))
    elapsed = time.monotonic() - start

    assert 0.4 <= elapsed <= 4.0, f"deadline nên kích hoạt sau ~0.5s, thực tế {elapsed:.2f}s"
    assert "không sống" in str(exc_info.value)


def test_solve_challenge_escscalates_when_instance_never_lives(
    always_offline, monkeypatch, tmp_path: Path
):
    """Caller solve_challenge bắt InstanceNotLiveError -> ESCALATED + NEEDS_HUMAN_REVIEW.md."""
    monkeypatch.setenv("WEBGPT_INSTANCE_LIVE_DEADLINE", "0.5")
    task = _make_task(tmp_path)
    runner = ClaudeCodeSessionRunner(task)
    # Không đụng ~/.claude.json trong test.
    monkeypatch.setattr(runner, "_ensure_trusted_workspace", lambda: None)

    turn_calls: list[str] = []

    async def _fake_turn(prompt):
        turn_calls.append(prompt)
        return 0, ""

    monkeypatch.setattr(runner, "run_claude_turn", _fake_turn)

    start = time.monotonic()
    result = asyncio.run(runner.solve_challenge())
    elapsed = time.monotonic() - start

    assert result is task
    assert task.status == ChallengeStatus.ESCALATED
    assert turn_calls == []  # không được phép vào vòng giải khi instance chết
    assert elapsed <= 5.0

    review = tmp_path / "NEEDS_HUMAN_REVIEW.md"
    assert review.exists()
    content = review.read_text(encoding="utf-8")
    assert "instance không sống" in content


def test_zero_deadline_keeps_waiting_forever(monkeypatch, tmp_path: Path):
    """WEBGPT_INSTANCE_LIVE_DEADLINE=0 -> chờ vô hạn như behavior cũ (test bằng cancel)."""
    monkeypatch.setenv("WEBGPT_INSTANCE_LIVE_DEADLINE", "0")

    def _fail(*args, **kwargs):
        raise OSError("[Errno -3] connection refused (fake)")

    monkeypatch.setattr(urllib.request, "urlopen", _fail)

    runner = ClaudeCodeSessionRunner(_make_task(tmp_path))

    async def _scenario():
        await asyncio.sleep(0)  # đảm bảo loop chạy trước
        try:
            await asyncio.wait_for(runner.ensure_instance_live(), timeout=0.8)
        except asyncio.TimeoutError:
            return "still-waiting"
        return "returned"

    outcome = asyncio.run(_scenario())
    assert outcome == "still-waiting"


# ---------------------------------------------------------------------------
# B1.2 — run_claude_turn timeout từ env
# ---------------------------------------------------------------------------


def test_claude_turn_timeout_from_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WEBGPT_CLAUDE_TURN_TIMEOUT", "12.5")
    recorded: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):
        recorded.update(kwargs)
        return py_types.SimpleNamespace(returncode=0, stdout="", stderr="")

    import gpt.orchestrator.session_runner as sr_mod

    monkeypatch.setattr(sr_mod.subprocess, "run", _fake_run)

    runner = ClaudeCodeSessionRunner(_make_task(tmp_path, target_url=None))
    code, out = asyncio.run(runner.run_claude_turn("hello"))

    assert (code, out) == (0, "\n")
    assert recorded.get("timeout") == 12.5


def test_claude_turn_timeout_default_300(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("WEBGPT_CLAUDE_TURN_TIMEOUT", raising=False)
    recorded: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):
        recorded.update(kwargs)
        return py_types.SimpleNamespace(returncode=0, stdout="", stderr="")

    import gpt.orchestrator.session_runner as sr_mod

    monkeypatch.setattr(sr_mod.subprocess, "run", _fake_run)

    runner = ClaudeCodeSessionRunner(_make_task(tmp_path, target_url=None))
    asyncio.run(runner.run_claude_turn("hello"))

    assert recorded.get("timeout") == 300.0


# ---------------------------------------------------------------------------
# B1.3 + B2 — worker deadline & exception propagation trong race_solver
# ---------------------------------------------------------------------------


def test_run_race_swallows_worker_exception_and_records_it(tmp_path: Path):
    logs: list[str] = []
    solver = SwarmRaceSolver(
        challenge_dir=tmp_path,
        target_url=None,
        num_workers=2,
        max_attempts_per_worker=1,
        logger=logs.append,
    )

    async def _boom(worker_idx, worker_name, worker_angle):
        if worker_idx == 1:
            raise RuntimeError("explosion-in-grandmaster-1")
        return None

    solver._worker_runner = _boom  # type: ignore[method-assign]

    result = asyncio.run(solver.run_race())

    assert result is None  # contract trả về không đổi
    assert len(solver.worker_errors) == 1
    exc = next(iter(solver.worker_errors.values()))
    assert isinstance(exc, RuntimeError)
    assert "explosion-in-grandmaster-1" in str(exc)

    joined = "\n".join(logs)
    assert "[worker 1] FAILED:" in joined
    assert "explosion-in-grandmaster-1" in joined
    assert "TỔNG KẾT SWARM RACE" in joined


def test_worker_deadline_breaks_loop_before_first_attempt(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WEBGPT_WORKER_DEADLINE", "1e-9")

    class FakeRunner:
        turn_calls = 0

        def __init__(self, task, logger=None):
            self.task = task

        def _collect_files(self):
            return ""

        async def run_claude_turn(self, prompt):
            FakeRunner.turn_calls += 1
            return 0, ""

        async def ensure_instance_live(self, *args, **kwargs):
            return True

        async def execute_solve_script(self, timeout=30):
            return 0, "", "", None

    monkeypatch.setattr(race_solver_mod, "ClaudeCodeSessionRunner", FakeRunner)

    logs: list[str] = []
    solver = SwarmRaceSolver(
        challenge_dir=tmp_path,
        target_url=None,
        num_workers=1,
        max_attempts_per_worker=3,
        logger=logs.append,
    )

    result = asyncio.run(solver.run_race())

    assert result is None
    assert FakeRunner.turn_calls == 0  # deadline chặn ngay trước vòng đầu tiên
    assert any("Hết deadline tổng của worker" in line for line in logs)


# ---------------------------------------------------------------------------
# Regression (d) — winner vẫn copy solve.py / flag.txt như cũ
# ---------------------------------------------------------------------------


class WinningRunner:
    def __init__(self, task, logger=None):
        self.task = task

    def _collect_files(self):
        return ""

    async def run_claude_turn(self, prompt):
        return 0, "```python\nprint('flag{race_winner}')\n```"

    async def ensure_instance_live(self, *args, **kwargs):
        return True

    async def execute_solve_script(self, timeout=30):
        return 0, "", "", "flag{race_winner}"


def test_winner_still_copies_solve_py_and_flag_txt(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(race_solver_mod, "ClaudeCodeSessionRunner", WinningRunner)

    logs: list[str] = []
    solver = SwarmRaceSolver(
        challenge_dir=tmp_path,
        target_url=None,
        num_workers=2,
        max_attempts_per_worker=1,
        logger=logs.append,
    )

    winner = asyncio.run(solver.run_race())

    assert winner is not None
    assert winner.flag == "flag{race_winner}"
    assert winner.status == ChallengeStatus.SOLVED
    assert (tmp_path / "flag.txt").read_text(encoding="utf-8") == "flag{race_winner}"
    assert (tmp_path / "solve.py").exists()
    assert solver.winner is winner
    assert solver.worker_errors == {}


# ---------------------------------------------------------------------------
# B4-lite — semaphore giới hạn solve slot
# ---------------------------------------------------------------------------


class CountingRunner:
    state = {"cur": 0, "peak": 0, "execs": 0}  # noqa: RUF012

    def __init__(self, task, logger=None):
        self.task = task

    def _collect_files(self):
        return ""

    async def run_claude_turn(self, prompt):
        await asyncio.sleep(0)  # nhường control để các worker dồn về semaphore
        return 0, ""  # không có code block

    async def ensure_instance_live(self, *args, **kwargs):
        return True

    async def execute_solve_script(self, timeout=30):
        st = self.__class__.state
        st["cur"] += 1
        st["execs"] += 1
        st["peak"] = max(st["peak"], st["cur"])
        await asyncio.sleep(0.05)
        st["cur"] -= 1
        return 0, "", "", None


def test_solve_semaphore_caps_peak_concurrency(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WEBGPT_SOLVE_CONCURRENCY", "2")

    class FreshCountingRunner(CountingRunner):
        pass

    FreshCountingRunner.state = {"cur": 0, "peak": 0, "execs": 0}
    monkeypatch.setattr(race_solver_mod, "ClaudeCodeSessionRunner", FreshCountingRunner)

    solver = SwarmRaceSolver(
        challenge_dir=tmp_path,
        target_url=None,
        num_workers=6,
        max_attempts_per_worker=1,
        logger=lambda msg: None,
    )
    assert solver._solve_slots._value == 2

    result = asyncio.run(solver.run_race())

    assert result is None
    st = FreshCountingRunner.state
    assert st["execs"] == 6, "mọi worker đều phải chạy đủ 1 lần execute_solve_script"
    assert st["peak"] <= 2, f"peak concurrency {st['peak']} vượt quá 2 slots"
    assert st["cur"] == 0
