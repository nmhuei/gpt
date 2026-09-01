from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from gpt.state import AuthRequired, RateLimited
from gpt.transport.account_health import AccountHealthTracker
from gpt.transport.factory import WorkerFactoryStats
from gpt.transport.multi_account import MultiAccountWorkerFactory


class FakeSession:
    pass


class FakeBrowser:
    connected = True


class FakeFactory:
    def __init__(self, name: str, exc: Exception | None = None):
        self.name = name
        self.browser_manager = FakeBrowser()
        self.session = FakeSession()
        self.exc = exc

    @asynccontextmanager
    async def lease(self):
        if self.exc is not None:
            raise self.exc
        yield self.session

    async def stats(self):
        return WorkerFactoryStats(1, 1, 0, 0, 0, 1, 0)

    async def close(self):
        return None


class FakeHealthTracker(AccountHealthTracker):
    """Tracker with a fixed clock so tests never depend on wall time."""

    def __init__(self):
        super().__init__(clock=lambda: 0.0)


@pytest.mark.anyio
async def test_multi_account_round_robin_and_explicit_pin():
    factory = MultiAccountWorkerFactory(
        {"personal": FakeFactory("personal"), "work": FakeFactory("work")}
    )

    async with factory.lease() as first:
        assert first._webgpt_account_name == "personal"
    async with factory.lease() as second:
        assert second._webgpt_account_name == "work"
    async with factory.lease("personal") as pinned:
        assert pinned._webgpt_account_name == "personal"

    stats = await factory.stats()
    assert stats.max_workers == 2
    assert factory.browsers_connected is True


@pytest.mark.anyio
async def test_default_name_is_sticky_without_health_tracker():
    factory = MultiAccountWorkerFactory(
        {"personal": FakeFactory("personal"), "work": FakeFactory("work")},
        default_name="work",
    )
    for _ in range(3):
        async with factory.lease() as session:
            assert session._webgpt_account_name == "work"


@pytest.mark.anyio
async def test_sticky_default_rotates_when_in_cooldown():
    tracker = FakeHealthTracker()
    tracker.mark_result("work", ok=False, cooldown_seconds=900)
    factory = MultiAccountWorkerFactory(
        {"personal": FakeFactory("personal"), "work": FakeFactory("work")},
        health=tracker,
        default_name="work",
    )

    names = []
    for _ in range(2):
        async with factory.lease() as session:
            names.append(session._webgpt_account_name)
    assert names == ["personal", "personal"]


@pytest.mark.anyio
async def test_health_round_robin_skips_cooldown_and_falls_back():
    tracker = FakeHealthTracker()
    tracker.mark_result("a", ok=False, cooldown_seconds=60)
    factory = MultiAccountWorkerFactory(
        {"a": FakeFactory("a"), "b": FakeFactory("b"), "c": FakeFactory("c")},
        health=tracker,
    )
    names = []
    for _ in range(2):
        async with factory.lease() as session:
            names.append(session._webgpt_account_name)
    # 'a' is cooling down; only b/c rotate.
    assert set(names) == {"b", "c"}

    for name in ("b", "c"):
        tracker.mark_result(name, ok=False, cooldown_seconds=60)
    # Everyone is cooling down: fallback to the full list (old behavior).
    async with factory.lease() as session:
        assert session._webgpt_account_name in {"a", "b", "c"}


@pytest.mark.anyio
async def test_lease_marks_success_and_failure(monkeypatch):
    monkeypatch.setenv("WEBGPT_ACCOUNT_COOLDOWN_SECONDS", "456")
    tracker = FakeHealthTracker()
    factories = {
        "good": FakeFactory("good"),
        "limited": FakeFactory("limited", exc=RateLimited("slow down")),
        "auth": FakeFactory("auth", exc=AuthRequired("re-login")),
    }
    factory = MultiAccountWorkerFactory(factories, health=tracker)

    async with factory.lease("good"):
        pass
    assert tracker.snapshot("good").status == "healthy"

    with pytest.raises(RateLimited):
        async with factory.lease("limited"):
            pass
    state = tracker.snapshot("limited")
    assert state.status == "cooldown"
    assert state.cooldown_until == 456.0

    with pytest.raises(AuthRequired):
        async with factory.lease("auth"):
            pass
    assert tracker.snapshot("auth").status == "cooldown"


@pytest.mark.anyio
async def test_lease_without_health_is_noop_on_failure():
    factory = MultiAccountWorkerFactory(
        {"boom": FakeFactory("boom", exc=RateLimited("no tracker here"))}
    )

    with pytest.raises(RateLimited):
        async with factory.lease("boom"):
            pass

    assert factory.health is None
