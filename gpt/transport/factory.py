from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from gpt.state import RateLimited, SessionState
from gpt.transport.breaker import (
    BackendCoolingDown,
    BreakerTicket,
    RateLimitBreaker,
    global_rate_limit_breaker,
)
from gpt.transport.browser import BrowserManager
from gpt.transport.session import ChatGPTWebSession


class WorkerQueueTimeout(RuntimeError):
    """No worker capacity became available within the configured queue deadline."""


# States in which a released worker may return to the idle pool. This is a
# whitelist on purpose: mid-send transients (SENDING/WAITING_RESPONSE/
# GENERATING, ...) mean a turn was interrupted and the worker must be closed --
# re-pooling one let every later turn fail with "Cannot send while session is
# GENERATING" (verify-fromscratch 2026-08-25, ĐỨT #1 worker poisoning).
_REUSABLE_RELEASE_STATES = frozenset(
    {
        SessionState.READY,
        SessionState.RETRYABLE_ERROR,
        SessionState.COMMIT_UNKNOWN,
    }
)


def worker_affinity_enabled() -> bool:
    """Whether conversation-to-worker affinity is active.

    Rollback switch: ``WEBGPT_WORKER_AFFINITY=0`` restores the plain LIFO
    acquire behavior completely.  Read dynamically so tests and operators can
    toggle it without recreating the factory.
    """
    return os.environ.get("WEBGPT_WORKER_AFFINITY", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


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
    # BACKOFF-BREAKER: the ticket proves this acquisition passed the global
    # rate-limit gate (non-None only when it is the half-open probe); the flag
    # is set by lease() when a turn ends in RATE_LIMITED so release() can trip
    # the breaker exactly once per turn outcome.
    breaker_ticket: BreakerTicket | None = None
    breaker_rate_limited: bool = False


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
        rate_limit_breaker: RateLimitBreaker | None = None,
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
        # BACKOFF-BREAKER (trace-forensics 2026-08-25): one process-wide
        # breaker is shared by every factory by default, so a rate limit on
        # any account blocks ALL acquisitions for the cooldown window instead
        # of failing over into another full-prompt resend.
        self.rate_limit_breaker = rate_limit_breaker or global_rate_limit_breaker()
        self._capacity = asyncio.Semaphore(max_workers)
        self._lock = asyncio.Lock()
        self._idle: list[_Worker] = []
        self._leased: dict[str, _Worker] = {}
        self._all: dict[str, _Worker] = {}
        # P2 worker affinity: affinity_key -> worker_id, kept in LRU order with
        # a bounded cap so long-lived gateways cannot grow this map forever.
        self._affinity_map: OrderedDict[str, str] = OrderedDict()
        self._queue_waiters = 0
        self._created_workers = 0
        self._closed_workers = 0
        self._closed = False
        self._started = False

    @property
    def browsers_connected(self) -> bool:
        """Aggregated browser connectivity used by health/readiness endpoints.

        Mirrors ``MultiAccountWorkerFactory.browsers_connected`` so the API
        server can ask any factory kind the same question without reaching
        into factory internals.
        """
        return bool(getattr(self.browser_manager, "connected", False))

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

    async def acquire(
        self, affinity_key: str | None = None
    ) -> tuple[str, ChatGPTWebSession]:
        if self._closed:
            raise RuntimeError("worker factory is closed")
        # BACKOFF-BREAKER: while the global cooldown window is open, fail fast
        # with BackendCoolingDown instead of handing out another worker whose
        # full-prompt resend would only burn more rate-limited quota. The gate
        # runs BEFORE the capacity semaphore so blocked callers never occupy
        # queue slots.
        ticket = self.rate_limit_breaker.before_acquire()
        try:
            await self._acquire_capacity()
        except BaseException:
            # Never strand the half-open probe slot on a failed queue wait.
            self.rate_limit_breaker.finish_probe(ticket)
            raise
        try:
            pinned_worker_id = await self._pinned_worker(affinity_key)
            async with self._lock:
                worker: _Worker | None = None
                if pinned_worker_id is not None:
                    # Prefer the worker this conversation used last turn when it
                    # is idle; if it is leased (or gone) fall through to the
                    # normal LIFO path instead of blocking on it.
                    for index, candidate in enumerate(self._idle):
                        if candidate.worker_id == pinned_worker_id:
                            worker = self._idle.pop(index)
                            break
                if worker is None:
                    worker = self._idle.pop() if self._idle else None
            if worker is None:
                try:
                    worker = await self._new_worker()
                except RateLimited:
                    # Bootstrap hit the anonymous-quota login wall
                    # (ChatGPTWebSession.create): same global signal as a
                    # send-time RATE_LIMITED classification.
                    self.rate_limit_breaker.trip("worker bootstrap rate limited")
                    raise
            worker.last_used = time.monotonic()
            worker.breaker_ticket = ticket
            async with self._lock:
                self._leased[worker.worker_id] = worker
            return worker.worker_id, worker.session
        except BaseException:
            self.rate_limit_breaker.finish_probe(ticket)
            self._capacity.release()
            raise

    async def _pinned_worker(self, affinity_key: str | None) -> str | None:
        """Resolve the preferred worker id for a key without blocking."""
        if not affinity_key or not worker_affinity_enabled():
            return None
        async with self._lock:
            worker_id = self._affinity_map.get(affinity_key)
            if worker_id is not None:
                self._affinity_map.move_to_end(affinity_key)
            return worker_id

    def _record_affinity(self, affinity_key: str | None, worker_id: str) -> None:
        if not affinity_key or not worker_affinity_enabled():
            return
        cap = max(1, self.max_workers * 4)
        self._affinity_map[affinity_key] = worker_id
        self._affinity_map.move_to_end(affinity_key)
        while len(self._affinity_map) > cap:
            self._affinity_map.popitem(last=False)

    def _purge_affinity(self, worker_ids: set[str]) -> None:
        stale = [
            key for key, bound in self._affinity_map.items() if bound in worker_ids
        ]
        for key in stale:
            del self._affinity_map[key]

    def _settle_rate_limit(self, worker: _Worker, *, healthy: bool) -> None:
        """Report a finished turn's outcome to the global breaker (once).

        Rate-limited turns open/widen the cooldown; healthy releases count as
        a successful probe; any OTHER failure only frees the half-open slot so
        non-rate-limit behavior stays exactly as before.
        """
        rl_hit = worker.breaker_rate_limited
        ticket = worker.breaker_ticket
        worker.breaker_ticket = None
        worker.breaker_rate_limited = False
        if rl_hit:
            self.rate_limit_breaker.trip("turn ended rate limited")
        elif ticket is None:
            return
        elif healthy:
            self.rate_limit_breaker.record_success(ticket)
        else:
            self.rate_limit_breaker.finish_probe(ticket)

    async def release(
        self, worker_id: str, *, reusable: bool = True, affinity_key: str | None = None
    ) -> None:
        async with self._lock:
            worker = self._leased.pop(worker_id, None)
        if worker is None:
            return
        worker.last_used = time.monotonic()
        healthy = reusable and worker.session.state in _REUSABLE_RELEASE_STATES
        self._settle_rate_limit(worker, healthy=healthy)
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
                self._purge_affinity({worker.worker_id})
        elif affinity_key:
            # Worker survived; remember it for the next turn of this conversation.
            async with self._lock:
                self._record_affinity(affinity_key, worker.worker_id)
        self._capacity.release()

    @asynccontextmanager
    async def lease(
        self, affinity_key: str | None = None
    ) -> AsyncIterator[ChatGPTWebSession]:
        worker_id, session = await self.acquire(affinity_key)
        reusable = True  # Success exits defer to release()'s health check.
        rate_limited = False
        try:
            yield session
        except BaseException as exc:
            # Classify on the session state machine, not on the exception type.
            # The previous blacklist ({FATAL_ERROR, ...}) treated every other
            # state -- including mid-send GENERATING/WAITING_RESPONSE -- as
            # reusable, and because asyncio.CancelledError is a BaseException it
            # bypassed ``except Exception`` entirely and left ``reusable=True``
            # for an aborted turn. Only known-recoverable states survive; a
            # cancelled or poisoned worker is closed by release().
            reusable = session.state in _REUSABLE_RELEASE_STATES
            # BACKOFF-BREAKER: classify the rate-limit signal the same way, so
            # release() can trip the global cooldown for this outcome.
            rate_limited = isinstance(exc, RateLimited) or (
                session.state is SessionState.RATE_LIMITED
            )
            raise

        finally:
            if rate_limited:
                async with self._lock:
                    leased_worker = self._leased.get(worker_id)
                    if leased_worker is not None:
                        leased_worker.breaker_rate_limited = True
            await self.release(worker_id, reusable=reusable, affinity_key=affinity_key)

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
                self._purge_affinity({worker.worker_id})
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
            self._affinity_map.clear()
        for worker in workers:
            try:
                await worker.session.close()
            except Exception:
                pass
        await self.browser_manager.stop()


__all__ = [
    "BackendCoolingDown",
    "ChatGPTWorkerFactory",
    "RateLimitBreaker",
    "WorkerFactoryStats",
    "WorkerQueueTimeout",
    "worker_affinity_enabled",
]
