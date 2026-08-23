from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from gpt.state import SessionState
from gpt.transport.browser import BrowserManager
from gpt.transport.session import ChatGPTWebSession


class WorkerQueueTimeout(RuntimeError):
    """No worker capacity became available within the configured queue deadline."""


@dataclass
class WorkerFactoryStats:
    max_workers: int
    live_workers: int
    idle_workers: int
    leased_workers: int
    queue_waiters: int
    created_workers: int
    closed_workers: int


@dataclass
class _Worker:
    worker_id: str
    session: ChatGPTWebSession
    created_at: float
    last_used: float


class ChatGPTWorkerFactory:
    """Bounded elastic pool of short-lived ChatGPT Web page workers.

    Workers share one BrowserManager/context, so authentication/profile state is
    shared while each page/session remains isolated. Capacity is guarded by a
    semaphore; callers beyond the cap wait with an explicit queue timeout.
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        *,
        max_workers: int = 1,
        warm_workers: int = 1,
        queue_timeout: float = 180.0,
        target_url: str = "https://chatgpt.com",
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if warm_workers < 0 or warm_workers > max_workers:
            raise ValueError("warm_workers must be between 0 and max_workers")
        if queue_timeout <= 0:
            raise ValueError("queue_timeout must be positive")
        self.browser_manager = browser_manager
        self.max_workers = max_workers
        self.warm_workers = warm_workers
        self.queue_timeout = queue_timeout
        self.target_url = target_url
        self._capacity = asyncio.Semaphore(max_workers)
        self._lock = asyncio.Lock()
        self._idle: list[_Worker] = []
        self._leased: dict[str, _Worker] = {}
        self._all: dict[str, _Worker] = {}
        self._queue_waiters = 0
        self._created_workers = 0
        self._closed_workers = 0
        self._closed = False
        self._started = False

    async def start(self) -> None:
        """Start the shared browser and pre-warm the configured idle workers.

        Warm workers are real pages/sessions created before the first request so
        latency-sensitive clients do not pay the full bootstrap cost on their
        first turn.  They are still bounded by max_workers and are exposed in
        stats for manual verification.
        """
        if self._closed:
            raise RuntimeError("worker factory is closed")
        if self._started:
            return
        await self.browser_manager.start()
        self._started = True
        for _ in range(self.warm_workers):
            worker = await self._new_worker()
            async with self._lock:
                self._idle.append(worker)

    async def _new_worker(self) -> _Worker:
        session = await ChatGPTWebSession.create(
            browser_manager=self.browser_manager,
            target_url=self.target_url,
        )
        now = time.monotonic()
        worker = _Worker(
            worker_id=f"worker_{uuid.uuid4().hex[:12]}",
            session=session,
            created_at=now,
            last_used=now,
        )
        async with self._lock:
            self._all[worker.worker_id] = worker
            self._created_workers += 1
        return worker

    async def _acquire_capacity(self) -> None:
        async with self._lock:
            self._queue_waiters += 1
        try:
            await asyncio.wait_for(self._capacity.acquire(), timeout=self.queue_timeout)
        except TimeoutError as exc:
            raise WorkerQueueTimeout(
                f"No browser worker became available within {self.queue_timeout:.1f}s"
            ) from exc
        finally:
            async with self._lock:
                self._queue_waiters -= 1

    async def acquire(self) -> tuple[str, ChatGPTWebSession]:
        if self._closed:
            raise RuntimeError("worker factory is closed")
        await self._acquire_capacity()
        try:
            async with self._lock:
                worker = self._idle.pop() if self._idle else None
            if worker is None:
                worker = await self._new_worker()
            worker.last_used = time.monotonic()
            async with self._lock:
                self._leased[worker.worker_id] = worker
            return worker.worker_id, worker.session
        except Exception:
            self._capacity.release()
            raise

    async def release(self, worker_id: str, *, reusable: bool = True) -> None:
        async with self._lock:
            worker = self._leased.pop(worker_id, None)
        if worker is None:
            return
        worker.last_used = time.monotonic()
        healthy = reusable and worker.session.state in {
            SessionState.READY,
            SessionState.RETRYABLE_ERROR,
            SessionState.COMMIT_UNKNOWN,
        }
        keep_warm = False
        if healthy:
            async with self._lock:
                keep_warm = len(self._idle) < self.warm_workers
                if keep_warm:
                    self._idle.append(worker)
        if not keep_warm:
            await worker.session.close()
            async with self._lock:
                self._all.pop(worker.worker_id, None)
                self._closed_workers += 1
        self._capacity.release()

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[ChatGPTWebSession]:
        worker_id, session = await self.acquire()
        reusable = True
        try:
            yield session
        except Exception:
            reusable = session.state not in {
                SessionState.FATAL_ERROR,
                SessionState.BROWSER_DISCONNECTED,
                SessionState.PAGE_CRASHED,
                SessionState.RATE_LIMITED,
            }
            raise

        finally:
            await self.release(worker_id, reusable=reusable)

    async def reap_idle(self, idle_seconds: float) -> int:
        if idle_seconds < 0:
            raise ValueError("idle_seconds must be >= 0")
        cutoff = time.monotonic() - idle_seconds
        victims: list[_Worker] = []
        async with self._lock:
            survivors: list[_Worker] = []
            for worker in self._idle:
                if worker.last_used <= cutoff and len(self._idle) - len(victims) > self.warm_workers:
                    victims.append(worker)
                else:
                    survivors.append(worker)
            self._idle = survivors
        for worker in victims:
            await worker.session.close()
            async with self._lock:
                self._all.pop(worker.worker_id, None)
                self._closed_workers += 1
        return len(victims)

    async def stats(self) -> WorkerFactoryStats:
        async with self._lock:
            return WorkerFactoryStats(
                max_workers=self.max_workers,
                live_workers=len(self._all),
                idle_workers=len(self._idle),
                leased_workers=len(self._leased),
                queue_waiters=self._queue_waiters,
                created_workers=self._created_workers,
                closed_workers=self._closed_workers,
            )

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            workers = list(self._all.values())
            self._idle.clear()
            self._leased.clear()
            self._all.clear()
        for worker in workers:
            try:
                await worker.session.close()
            except Exception:
                pass
        await self.browser_manager.stop()


__all__ = [
    "ChatGPTWorkerFactory",
    "WorkerFactoryStats",
    "WorkerQueueTimeout",
]
