from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from gpt.auth import AccountStore
from gpt.transport.account_health import (
    COOLDOWN,
    DEGRADED,
    HEALTHY,
    UNKNOWN,
    AccountHealthTracker,
    check_account_health,
    periodic_health_loop,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePage:
    url = "https://chatgpt.com/"

    async def evaluate(self, _script):
        return {"accessToken": "token-123"}


class UnauthenticatedPage(FakePage):
    url = "https://chatgpt.com/"

    async def evaluate(self, _script):
        return {}


def test_cooldown_expires_according_to_fake_clock():
    clock = FakeClock()
    tracker = AccountHealthTracker(clock=clock)

    tracker.mark_result("personal", ok=False, cooldown_seconds=60)
    assert tracker.snapshot("personal").status == COOLDOWN
    assert tracker.is_available("personal") is False

    clock.advance(59)
    assert tracker.is_available("personal") is False

    clock.advance(2)
    assert tracker.is_available("personal") is True


def test_success_resets_failure_streak_and_cooldown():
    clock = FakeClock()
    tracker = AccountHealthTracker(clock=clock)

    tracker.mark_result("a", ok=False, cooldown_seconds=30)
    tracker.mark_result("a", ok=False, cooldown_seconds=30)
    state = tracker.snapshot("a")
    assert state.consecutive_failures == 2
    assert state.last_checked_at == clock.now

    tracker.mark_result("a", ok=True)
    state = tracker.snapshot("a")
    assert state.status == HEALTHY
    assert state.consecutive_failures == 0
    assert state.cooldown_until == 0.0
    assert tracker.available_names(["a"]) == ["a"]


def test_all_names_in_cooldown_falls_back_to_full_list():
    clock = FakeClock()
    tracker = AccountHealthTracker(clock=clock)
    names = ["alpha", "beta", "gamma"]

    for name in names:
        tracker.mark_result(name, ok=False, cooldown_seconds=120)

    assert tracker.available_names(names) == names


def test_degraded_without_cooldown_still_available():
    tracker = AccountHealthTracker(clock=FakeClock())
    tracker.mark_result("a", ok=False, cooldown_seconds=0)

    assert tracker.snapshot("a").status == DEGRADED
    assert tracker.available_names(["a"]) == ["a"]


def test_unknown_state_defaults():
    tracker = AccountHealthTracker(clock=FakeClock())
    assert tracker.snapshot("ghost").status == UNKNOWN
    assert tracker.available_names(["ghost"]) == ["ghost"]


async def test_check_account_health_uses_auth_probe_and_updates_store(tmp_path):
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    store.ensure("good")
    store.ensure("bad")

    @asynccontextmanager
    async def good_page(profile_dir):
        yield FakePage()

    @asynccontextmanager
    async def bad_page(profile_dir):
        yield UnauthenticatedPage()

    assert await check_account_health(store, "good", page_context=good_page) is True
    assert store.get("good").auth_status == "authenticated"

    assert await check_account_health(store, "bad", page_context=bad_page) is False
    assert store.get("bad").auth_status == "login_required"


async def test_periodic_health_loop_marks_results_until_stopped(tmp_path):
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    store.ensure("ok-account")
    store.ensure("down-account")
    tracker = AccountHealthTracker(clock=FakeClock())
    stop_event = asyncio.Event()
    calls: list[str] = []

    async def checker(_store, name):
        calls.append(name)
        return name == "ok-account"

    task = asyncio.create_task(
        periodic_health_loop(
            tracker,
            store,
            ["ok-account", "down-account"],
            interval=0.01,
            checker=checker,
            failure_cooldown_seconds=120,
            stop_event=stop_event,
        )
    )
    for _ in range(100):
        if len(calls) >= 4:
            break
        await asyncio.sleep(0.001)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert calls[0] == "ok-account"
    assert calls[1] == "down-account"
    assert tracker.snapshot("ok-account").status == HEALTHY
    assert tracker.snapshot("down-account").status == COOLDOWN
    assert tracker.available_names(["ok-account", "down-account"]) == ["ok-account"]
