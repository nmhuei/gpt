import asyncio
from unittest.mock import AsyncMock

import pytest

from gpt.factory import ChatGPTWorkerFactory, WorkerQueueTimeout
from gpt.state import SessionState


class FakeManager:
    def __init__(self):
        self.start = AsyncMock()
        self.stop = AsyncMock()


class FakeSession:
    def __init__(self, name: str):
        self.name = name
        self.state = SessionState.READY
        self.close = AsyncMock()


@pytest.mark.anyio
async def test_worker_factory_start_prewarms_configured_workers(monkeypatch):
    manager = FakeManager()
    created: list[FakeSession] = []

    async def create(**_kwargs):
        session = FakeSession(f"warm{len(created)}")
        created.append(session)
        return session

    monkeypatch.setattr("gpt.factory.ChatGPTWebSession.create", create)
    factory = ChatGPTWorkerFactory(manager, max_workers=3, warm_workers=2)

    await factory.start()
    await factory.start()  # idempotent; does not create duplicate warm pages

    stats = await factory.stats()
    assert manager.start.await_count == 1
    assert stats.created_workers == 2
    assert stats.live_workers == 2
    assert stats.idle_workers == 2

    async with factory.lease() as first:
        assert first.name in {"warm0", "warm1"}
    assert (await factory.stats()).idle_workers == 2
    await factory.close()


@pytest.mark.anyio
async def test_worker_factory_reuses_one_warm_worker(monkeypatch):
    manager = FakeManager()
    created: list[FakeSession] = []

    async def create(**_kwargs):
        session = FakeSession(f"s{len(created)}")
        created.append(session)
        return session

    monkeypatch.setattr("gpt.factory.ChatGPTWebSession.create", create)
    factory = ChatGPTWorkerFactory(manager, max_workers=2, warm_workers=1)

    async with factory.lease() as first:
        assert first.name == "s0"
    async with factory.lease() as second:
        assert second is first

    stats = await factory.stats()
    assert stats.created_workers == 1
    assert stats.idle_workers == 1
    await factory.close()
    first.close.assert_awaited_once()
    manager.stop.assert_awaited_once()


@pytest.mark.anyio
async def test_worker_factory_enforces_capacity_and_queue_timeout(monkeypatch):
    manager = FakeManager()

    async def create(**_kwargs):
        return FakeSession("only")

    monkeypatch.setattr("gpt.factory.ChatGPTWebSession.create", create)
    factory = ChatGPTWorkerFactory(
        manager,
        max_workers=1,
        warm_workers=1,
        queue_timeout=0.03,
    )
    worker_id, _session = await factory.acquire()
    with pytest.raises(WorkerQueueTimeout):
        await factory.acquire()
    await factory.release(worker_id)
    await factory.close()


@pytest.mark.anyio
async def test_worker_factory_allows_bounded_parallel_leases(monkeypatch):
    manager = FakeManager()
    counter = 0

    async def create(**_kwargs):
        nonlocal counter
        counter += 1
        return FakeSession(f"s{counter}")

    monkeypatch.setattr("gpt.factory.ChatGPTWebSession.create", create)
    factory = ChatGPTWorkerFactory(manager, max_workers=2, warm_workers=2)
    active = 0
    peak = 0
    gate = asyncio.Event()

    async def work():
        nonlocal active, peak
        async with factory.lease():
            active += 1
            peak = max(peak, active)
            if active == 2:
                gate.set()
            await gate.wait()
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(work(), work())
    assert peak == 2
    assert (await factory.stats()).live_workers == 2
    await factory.close()


@pytest.mark.anyio
async def test_worker_factory_closes_unhealthy_worker_instead_of_reusing(monkeypatch):
    manager = FakeManager()
    session = FakeSession("bad")

    async def create(**_kwargs):
        return session

    monkeypatch.setattr("gpt.factory.ChatGPTWebSession.create", create)
    factory = ChatGPTWorkerFactory(manager, max_workers=1, warm_workers=1)
    worker_id, leased = await factory.acquire()
    leased.state = SessionState.FATAL_ERROR
    await factory.release(worker_id)
    stats = await factory.stats()
    assert stats.idle_workers == 0
    assert stats.closed_workers == 1
    session.close.assert_awaited_once()
    await factory.close()


@pytest.mark.anyio
async def test_worker_factory_cancelled_waiter_does_not_leak_queue_slot(monkeypatch):
    manager = FakeManager()

    async def create(**_kwargs):
        return FakeSession("only")

    monkeypatch.setattr("gpt.factory.ChatGPTWebSession.create", create)
    factory = ChatGPTWorkerFactory(
        manager,
        max_workers=1,
        warm_workers=1,
        queue_timeout=1.0,
    )
    worker_id, _ = await factory.acquire()
    waiter = asyncio.create_task(factory.acquire())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    stats = await factory.stats()
    assert stats.queue_waiters == 0
    assert stats.leased_workers == 1
    await factory.release(worker_id)
    async with factory.lease():
        pass
    await factory.close()


@pytest.mark.anyio
async def test_worker_factory_lease_released_when_caller_is_cancelled(monkeypatch):
    manager = FakeManager()

    async def create(**_kwargs):
        return FakeSession("cancelled")

    monkeypatch.setattr("gpt.factory.ChatGPTWebSession.create", create)
    factory = ChatGPTWorkerFactory(manager, max_workers=1, warm_workers=1)
    entered = asyncio.Event()

    async def work():
        async with factory.lease():
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stats = await factory.stats()
    assert stats.leased_workers == 0
    assert stats.idle_workers == 1
    await factory.close()
