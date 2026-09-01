"""B3-full — Cooperative cancel cho subprocess trong session_runner + race_solver.

Không gọi claude/network thật: binary "claude" là script python giả lập (sleep/echo),
network chỉ qua monkeypatch urllib. Mọi assertion thời gian đều rộng đủ để không flaky.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
import types as py_types
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

from gpt.orchestrator import race_solver as race_solver_mod
from gpt.orchestrator import session_runner as sr_mod
from gpt.orchestrator.race_solver import SwarmRaceSolver
from gpt.orchestrator.session_runner import ClaudeCodeSessionRunner
from gpt.orchestrator.types import ChallengeTask, InstanceNotLiveError

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FAKE_SLEEP_BIN = """#!/usr/bin/env python3
import sys, time
sys.stdout.write("fake-claude-started\\n")
sys.stdout.flush()
sys.stderr.write("fake-claude-stderr\\n")
duration = float(sys.argv[-1]) if sys.argv[-1].replace(".", "", 1).isdigit() else 60.0
time.sleep(duration)
"""

FAKE_ECHO_BIN = """#!/usr/bin/env python3
print("legacy-ok")
"""


@pytest.fixture()
def fake_claude_bin(tmp_path: Path, monkeypatch):
    """Binary 'claude' giả lập: in ra vài dòng rồi sleep 60s."""
    bin_path = tmp_path / "fake-claude.py"
    bin_path.write_text(FAKE_SLEEP_BIN, encoding="utf-8")
    bin_path.chmod(0o755)
    monkeypatch.setenv("WEBGPT_CLAUDE_BIN", str(bin_path))
    monkeypatch.delenv("WEBGPT_COOPERATIVE_CANCEL", raising=False)
    return bin_path


def _make_task(directory: Path, target_url: str | None = None) -> ChallengeTask:
    return ChallengeTask(directory=directory, name="Coop Challenge", target_url=target_url)


def _flaky_timing_retry(scenario: Callable[[int], None], *, attempts: int = 2) -> None:
    """Vòng thử lại nội bộ (không phải plugin pytest) cho các deadline wall-clock.

    Chạy ``scenario`` tối đa ``attempts`` lượt; số lượt đang chạy được truyền vào
    làm tham số để scenario tự dọn state trước khi thử lại. Hấp thụ nhiễu thời
    gian khi chạy song song nhiều suite mà không phải nới assert.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            scenario(attempt)
            return
        except Exception as exc:
            last_exc = exc
    assert last_exc is None, (
        f"cả {attempts} lượt thử đều fail (nhiễu timing?): {last_exc!r}"
    )


def _set_stop_from_thread(delay: float) -> Callable[[asyncio.Event], None]:
    """Trả về hàm set stop_event sau ``delay`` giây từ một OS thread, dùng
    loop.call_soon_threadsafe (fd-driven wakeup — đúng cơ chế mà child-exit
    notification của asyncio dùng, nên không phụ thuộc timer của event loop)."""

    def _arm(event: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()

        def _worker():
            time.sleep(delay)
            loop.call_soon_threadsafe(event.set)

        threading.Thread(target=_worker, daemon=True).start()

    return _arm


# ---------------------------------------------------------------------------
# (a) stop_event hủy run_claude_turn giữa chừng, trả về nhanh, process chết hẳn
# ---------------------------------------------------------------------------


@pytest.mark.flaky_timing
def test_stop_event_cancels_claude_turn_under_5s(fake_claude_bin, tmp_path: Path):
    def scenario(_attempt: int):
        runner = ClaudeCodeSessionRunner(_make_task(tmp_path))
        arm_stop = _set_stop_from_thread(0.2)

        async def _scenario():
            stop = asyncio.Event()
            arm_stop(stop)
            start = time.monotonic()
            rc, out = await runner.run_claude_turn("hello", stop_event=stop)
            return rc, out, time.monotonic() - start

        rc, out, elapsed = asyncio.run(_scenario())

        assert elapsed < 5.0, f"phải hủy xong dưới 5s, thực tế {elapsed:.2f}s"
        # Process đã được reap hoàn toàn (không zombie): returncode khác None.
        assert rc is not None
        assert "fake-claude-started" in out

    _flaky_timing_retry(scenario)


@pytest.mark.flaky_timing
def test_stop_event_cancels_solve_script_and_returns_collected_output(tmp_path: Path):
    (tmp_path / "solve.py").write_text(
        "import time\nprint('partial-out', flush=True)\ntime.sleep(60)\n", encoding="utf-8"
    )

    def scenario(_attempt: int):
        runner = ClaudeCodeSessionRunner(_make_task(tmp_path))
        arm_stop = _set_stop_from_thread(0.3)

        async def _scenario():
            stop = asyncio.Event()
            arm_stop(stop)
            start = time.monotonic()
            res = await runner.execute_solve_script(timeout=120, stop_event=stop)
            return res, time.monotonic() - start

        (retcode, _stdout, _stderr, flag), elapsed = asyncio.run(_scenario())

        assert elapsed < 5.0, f"execute_solve_script phải hủy được, thực tế {elapsed:.2f}s"
        assert retcode is not None
        assert flag is None

    _flaky_timing_retry(scenario)


# ---------------------------------------------------------------------------
# (b) Timeout thuần giữ nguyên semantic cũ
# ---------------------------------------------------------------------------


@pytest.mark.flaky_timing
def test_pure_timeout_solve_script_returns_124(fake_claude_bin, tmp_path: Path):
    (tmp_path / "solve.py").write_text(
        "import time\ntime.sleep(30)\n", encoding="utf-8"
    )

    def scenario(_attempt: int):
        runner = ClaudeCodeSessionRunner(_make_task(tmp_path))

        start = time.monotonic()
        retcode, _stdout, stderr, flag = asyncio.run(runner.execute_solve_script(timeout=1))
        elapsed = time.monotonic() - start

        assert retcode == 124, "behavior cũ: timeout thuần của solve script -> mã 124"
        assert "Hết thời gian thực thi" in stderr
        assert flag is None
        assert elapsed < 8.0  # SIGTERM phải giết được process sleep(30)

    _flaky_timing_retry(scenario)


@pytest.mark.flaky_timing
def test_pure_timeout_claude_turn_legacy_raises(fake_claude_bin, tmp_path: Path, monkeypatch):
    """Không có stop_event -> đường cũ: run_claude_turn raise TimeoutExpired như trước."""
    monkeypatch.setenv("WEBGPT_CLAUDE_TURN_TIMEOUT", "0.5")

    def scenario(_attempt: int):
        runner = ClaudeCodeSessionRunner(_make_task(tmp_path, target_url=None))

        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            asyncio.run(runner.run_claude_turn("hello"))
        elapsed = time.monotonic() - start

        assert elapsed < 8.0  # SIGTERM dọn process sleep(60), không chờ hết 60s

    _flaky_timing_retry(scenario)


@pytest.mark.flaky_timing
def test_timeout_with_stop_event_present_simulates_124(fake_claude_bin, tmp_path: Path, monkeypatch):
    """Có stop_event (đường asyncio) nhưng hết timeout thuần -> vẫn giả lập 124."""
    monkeypatch.setenv("WEBGPT_CLAUDE_TURN_TIMEOUT", "0.5")

    def scenario(_attempt: int):
        runner = ClaudeCodeSessionRunner(_make_task(tmp_path, target_url=None))
        stop = asyncio.Event()  # không bao giờ set

        start = time.monotonic()
        rc, out = asyncio.run(runner.run_claude_turn("hello", stop_event=stop))
        elapsed = time.monotonic() - start

        assert rc == 124
        assert "Hết thời gian thực thi" in out
        assert elapsed < 8.0

    _flaky_timing_retry(scenario)


# ---------------------------------------------------------------------------
# (c) Race integration: winner bắt flag trong khi loser ngủ 60s -> tổng < 15s
# ---------------------------------------------------------------------------


class CoopRaceRunner:
    """Fake runner: worker 1 ngủ 60s ở execute_solve_script, worker 2 thắng ngay.

    Loser ngủ 'cooperatively': thức dậy ngay khi stop_event được set — chứng tỏ
    relay global->worker hoạt động và attempt không chạy hết 60s.
    """

    instances: list[CoopRaceRunner] = []  # noqa: RUF012

    def __init__(self, task: ChallengeTask, logger=None):
        self.task = task
        self.worker_idx = int(task.directory.name.rsplit("_", 1)[-1])
        self.saw_worker_stop = False
        type(self).instances.append(self)

    def _collect_files(self) -> str:
        return ""

    async def run_claude_turn(self, prompt, stop_event=None):
        await asyncio.sleep(0.05)
        return 0, "```python\nprint('flag{coop_race}')\n```"

    async def ensure_instance_live(self, *args, **kwargs):
        return True

    async def execute_solve_script(self, timeout=30, stop_event=None):
        assert stop_event is not None, "race solver phải truyền per-worker stop_event"
        if self.worker_idx == 1:
            # Loser "ngủ" 60s nhưng thức dậy khi stop_event set (fd/timer-agnostic).
            sleep_task = asyncio.create_task(asyncio.sleep(60))
            stop_task = asyncio.create_task(stop_event.wait())
            done, _ = await asyncio.wait(
                {sleep_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            self.saw_worker_stop = stop_task in done
            sleep_task.cancel()
            return 0, "", "", None
        # Winner: hoàn tất sau 0.05s qua thread + call_soon_threadsafe (fd-driven,
        # giống cơ chế child-exit notification nên không phụ thuộc timer của loop).
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def _finish():
            loop.call_soon_threadsafe(fut.set_result, None)

        threading.Timer(0.05, _finish).start()
        await fut
        return 0, "", "", "flag{coop_race}"


@pytest.mark.flaky_timing
def test_race_cooperative_cancel_finishes_under_15s(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(race_solver_mod, "ClaudeCodeSessionRunner", CoopRaceRunner)

    def scenario(_attempt: int):
        CoopRaceRunner.instances = []
        logs: list[str] = []
        solver = SwarmRaceSolver(
            challenge_dir=tmp_path,
            target_url=None,
            num_workers=2,
            max_attempts_per_worker=1,
            logger=logs.append,
        )

        start = time.monotonic()
        winner = asyncio.run(solver.run_race())
        elapsed = time.monotonic() - start

        assert elapsed < 15.0, f"race phải kết thúc sớm nhờ cooperative cancel, thực tế {elapsed:.2f}s"
        assert winner is not None and winner.flag == "flag{coop_race}"
        loser = next(i for i in CoopRaceRunner.instances if i.worker_idx == 1)
        assert loser.saw_worker_stop, "loser phải được đánh thức bởi per-worker stop_event"

    _flaky_timing_retry(scenario)


# ---------------------------------------------------------------------------
# (d) Attempt bị abort KHÔNG ghi vào error_history
# ---------------------------------------------------------------------------


class AbortAtTurnRunner(CoopRaceRunner):
    """Worker 1 bị hủy ngay ở giai đoạn run_claude_turn."""

    instances: list[CoopRaceRunner] = []  # noqa: RUF012

    def __init__(self, task: ChallengeTask, logger=None):
        super().__init__(task, logger)

    async def run_claude_turn(self, prompt, stop_event=None):
        if self.worker_idx == 1:
            assert stop_event is not None
            await stop_event.wait()  # ngủ tới khi race kết thúc
            return 124, "đã bị ngắt giữa đường"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def _finish():
            loop.call_soon_threadsafe(fut.set_result, None)

        threading.Timer(0.05, _finish).start()
        await fut
        return 0, "```python\nprint('flag{abort_race}')\n```"

    async def execute_solve_script(self, timeout=30, stop_event=None):
        assert stop_event is not None
        if self.worker_idx == 1:
            return 0, "", "", None  # không bao giờ đến đây
        return 0, "", "", "flag{abort_race}"


def test_aborted_attempt_not_recorded_in_error_history(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(race_solver_mod, "ClaudeCodeSessionRunner", AbortAtTurnRunner)
    AbortAtTurnRunner.instances = []

    logs: list[str] = []
    solver = SwarmRaceSolver(
        challenge_dir=tmp_path,
        target_url=None,
        num_workers=2,
        max_attempts_per_worker=1,
        logger=logs.append,
    )

    winner = asyncio.run(solver.run_race())

    assert winner is not None and winner.flag == "flag{abort_race}"
    joined = "\n".join(logs)
    assert "bị hủy do race đã kết thúc" in joined

    loser = next(i for i in AbortAtTurnRunner.instances if i.worker_idx == 1)
    assert loser.task.error_history == [], "attempt bị abort không được ghi vào error_history"
    assert len(solver.worker_errors) == 0


# ---------------------------------------------------------------------------
# (e) WEBGPT_COOPERATIVE_CANCEL=0 -> fallback đường cũ (subprocess.run trong thread)
# ---------------------------------------------------------------------------


def test_rollback_flag_forces_legacy_subprocess_run(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WEBGPT_COOPERATIVE_CANCEL", "0")

    recorded: dict[str, object] = {}

    def _spy_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        recorded["kwargs"] = kwargs
        return py_types.SimpleNamespace(returncode=0, stdout="legacy-ok\n", stderr="")

    monkeypatch.setattr(sr_mod.subprocess, "run", _spy_run)

    runner = ClaudeCodeSessionRunner(_make_task(tmp_path))
    stop_already_set = asyncio.Event()
    stop_already_set.set()  # dù stop đã set, env=0 buộc đi đường cũ

    rc, out = asyncio.run(runner.run_claude_turn("hello", stop_event=stop_already_set))

    assert rc == 0
    assert "legacy-ok" in out
    assert "cmd" in recorded, "env=0 phải đi qua subprocess.run (đường legacy)"
    recorded_kwargs = recorded.get("kwargs")
    assert isinstance(recorded_kwargs, dict)
    assert recorded_kwargs.get("timeout") == 300.0


# ---------------------------------------------------------------------------
# Bổ trợ: wire stop_event vào ensure_instance_live (thức dậy giữa các chu kỳ sleep)
# ---------------------------------------------------------------------------


@pytest.fixture()
def always_offline_urlopen(monkeypatch):
    def _fail(*args, **kwargs):
        raise OSError("[Errno -3] connection refused (fake)")

    monkeypatch.setattr(urllib.request, "urlopen", _fail)


@pytest.mark.flaky_timing
def test_ensure_instance_live_wakes_on_stop_event(always_offline_urlopen, tmp_path: Path):
    def scenario(_attempt: int):
        runner = ClaudeCodeSessionRunner(_make_task(tmp_path, target_url="http://target.invalid"))
        arm_stop = _set_stop_from_thread(0.4)

        async def _scenario():
            stop = asyncio.Event()
            arm_stop(stop)
            start = time.monotonic()
            try:
                await runner.ensure_instance_live(max_wait_seconds=None, stop_event=stop)
            except InstanceNotLiveError as exc:
                return str(exc), time.monotonic() - start
            raise AssertionError("phải raise InstanceNotLiveError khi bị hủy bởi stop_event")

        msg, elapsed = asyncio.run(_scenario())

        assert "Cooperative Cancel" in msg
        assert elapsed < 3.0, f"phải thức dậy ngay khi stop set, thực tế {elapsed:.2f}s"

    _flaky_timing_retry(scenario)
