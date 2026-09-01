from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

from gpt.state import AuthRequired, RateLimited
from gpt.transport.breaker import BackendCoolingDown, RateLimitBreaker
from gpt.transport.factory import WorkerFactoryStats

if TYPE_CHECKING:  # pragma: no cover - typing only
    from gpt.transport.account_health import AccountHealthTracker
    from gpt.transport.breaker import CrossBrake
    from gpt.transport.usage_poller import PoolPressureBoard

DEFAULT_ACCOUNT_COOLDOWN_SECONDS = 900

# POOL-POLLER-PERACCT (row M): optional unpinned-selection ranking.
# ``round-robin`` (default) keeps the historical rotation byte-for-byte;
# ``least-pressure`` reorders the already-filtered candidates by each
# account's latest usage reading — but ONLY when every candidate has data
# (see :meth:`PoolPressureBoard.has_all`); any unknown reading falls back to
# plain rotation so accounts without credentials are never mis-ranked.
SELECTION_ENV = "WEBGPT_POOL_SELECTION"
SELECTION_ROUND_ROBIN = "round-robin"
SELECTION_LEAST_PRESSURE = "least-pressure"
_SELECTION_MODES = (SELECTION_ROUND_ROBIN, SELECTION_LEAST_PRESSURE)


def _selection_mode(raw: str | None = None) -> str:
    """Normalize the selection mode; unknown values stay round-robin."""
    text = (
        os.environ.get(SELECTION_ENV, "")
        if raw is None
        else raw
    ).strip().lower()
    return text if text in _SELECTION_MODES else SELECTION_ROUND_ROBIN


def _failure_cooldown_seconds() -> int:
    try:
        return int(os.environ.get("WEBGPT_ACCOUNT_COOLDOWN_SECONDS", "900"))
    except ValueError:
        return DEFAULT_ACCOUNT_COOLDOWN_SECONDS


class MultiAccountWorkerFactory:
    """Route leases across isolated per-account worker factories.

    A caller may request a specific account name. New conversations without a
    pin are assigned round-robin; the yielded session is tagged so the caller
    can persist that assignment for all later turns.

    With an optional ``AccountHealthTracker`` and a ``default_name``, unpinned
    leases become sticky on the default account while it is healthy and only
    round-robin across cooldown-free accounts once it is not.

    POOL-PER-ACCT-BREAKER (row S): with a ``breakers`` mapping (account name
    -> :class:`RateLimitBreaker`), selection skips accounts whose breaker is
    inside an open cooldown window, and when the chosen account's factory
    still raises :class:`BackendCoolingDown` during acquisition the pool
    transparently retries the next account -- up to N-1 attempts before the
    cooling-down error surfaces. An explicitly requested account is never
    silently rerouted: pins keep their strict semantics. Without ``breakers``
    every code path behaves exactly as before.

    POOL-POLLER-PERACCT (row M): three opt-in additions, all default-off /
    no-op so the pre-M wiring is untouched. ``pressures`` (a shared
    :class:`~gpt.transport.usage_poller.PoolPressureBoard` fed by the
    per-account usage pollers) enables ``WEBGPT_POOL_SELECTION=least-pressure``
    ranking; ``cross_brake`` (opt-in via ``WEBGPT_POOL_CROSS_BRAKE``, built by
    :func:`~gpt.transport.breaker.cross_brake_from_env`) counts REAL
    RateLimited failures per account and, at K distinct accounts inside its
    window, advises every pool breaker open for one short window.
    """

    def __init__(
        self,
        factories: Mapping[str, Any],
        health: AccountHealthTracker | None = None,
        default_name: str | None = None,
        breakers: Mapping[str, RateLimitBreaker] | None = None,
        *,
        pressures: PoolPressureBoard | None = None,
        cross_brake: CrossBrake | None = None,
        selection: str | None = None,
    ) -> None:
        if not factories:
            raise ValueError("MultiAccountWorkerFactory requires at least one account factory")
        self.factories = dict(factories)
        self.health = health
        self.default_name = default_name
        self.breakers: dict[str, RateLimitBreaker] = dict(breakers or {})
        self.pressures = pressures
        self.cross_brake = cross_brake
        # Explicit ``selection`` (tests/wiring) wins over the env default;
        # unknown values degrade to round-robin rather than erroring.
        self.selection_mode = _selection_mode(selection)
        self._names = tuple(self.factories)
        self._cursor = 0
        self._lock = asyncio.Lock()

    def _breaker_open(self, name: str) -> bool:
        """True while ``name``'s breaker window blocks new acquisitions.

        Only a genuinely open window skips an account; ``half_open`` stays
        selectable so the single-probe recovery path keeps working per
        account instead of stranding a recovered account behind selection.
        """
        breaker = self.breakers.get(name)
        if breaker is None or not breaker.enabled:
            return False
        return breaker.snapshot().state == "open"

    async def _pick_name(
        self,
        requested: str | None,
        exclude: Collection[str] = (),
    ) -> str | None:
        if requested is not None:
            if requested not in self.factories:
                raise KeyError(f"Unknown runtime account: {requested}")
            return requested
        all_names = [name for name in self._names if name not in exclude]
        pool = all_names
        if self.default_name is not None and self.default_name in self.factories:
            sticky = [self.default_name]
            if self.health is None:
                # No health data: trust the configured default unconditionally.
                pool = sticky
            else:
                available = self.health.available_names(all_names)
                # Sticky default: never rotate away from a healthy default.
                pool = sticky if self.default_name in available else available
        elif self.health is not None:
            pool = self.health.available_names(all_names)
        if not pool:
            pool = all_names
        usable = [name for name in pool if not self._breaker_open(name)]
        if not usable:
            # Everything the scheduler would pick is inside a cooldown window;
            # fall back to any non-open account outside this attempt so a
            # tripped sticky default still rotates to a healthy sibling.
            usable = [name for name in all_names if not self._breaker_open(name)]
        if not usable:
            return None
        async with self._lock:
            board = self.pressures
            if (
                board is not None
                and self.selection_mode == SELECTION_LEAST_PRESSURE
                and board.has_all(usable)
            ):
                # Least-pressure ranking over exactly the selectable names.
                # Rotating by the cursor BEFORE sorting keeps equal-pressure
                # ties fair; the cursor still advances so the next pick moves
                # through rotation order among ties.
                rotation = self._cursor % len(usable)
                ranked = sorted(
                    usable[rotation:] + usable[:rotation],
                    key=lambda candidate: board.pressure(candidate) or 0.0,
                )
                self._cursor += 1
                return ranked[0]
            name = usable[self._cursor % len(usable)]
            self._cursor += 1
            return name

    def pressure(self, name: str) -> float | None:
        """Latest known usage percent for ``name`` (row M observability API).

        None whenever no pressure board is wired or that account has no fresh
        reading — callers must treat unknown as "no data", never as 0.
        """
        return None if self.pressures is None else self.pressures.pressure(name)

    @staticmethod
    def _is_real_rate_limit(exc: BaseException) -> bool:
        """True only for genuine backend rate limits (never auth trouble)."""
        cls_name = type(exc).__name__
        return isinstance(exc, RateLimited) or "RateLimited" in cls_name

    def _mark_failure(self, name: str, exc: BaseException) -> None:
        # POOL-POLLER-PERACCT: the cross brake counts REAL RateLimited hits
        # only — auth problems say nothing about shared-IP pressure — and runs
        # even when no health tracker is wired.
        if self.cross_brake is not None and self._is_real_rate_limit(exc):
            self.cross_brake.record(name)
        if self.health is None:
            return
        cls_name = type(exc).__name__
        if isinstance(exc, (RateLimited, AuthRequired)) or (
            "RateLimited" in cls_name or "AuthRequired" in cls_name
        ):
            self.health.mark_result(
                name, ok=False, cooldown_seconds=_failure_cooldown_seconds()
            )

    @asynccontextmanager
    async def lease(self, account_name: str | None = None) -> AsyncIterator[Any]:
        if not self.breakers:
            # No per-account breakers wired: the historical single-shot path,
            # kept verbatim so scope=global behaviour cannot drift.
            name = await self._pick_name(account_name)
            if name is None:
                # Unreachable with an empty breaker map (nothing is excluded
                # and no account can be skipped), kept for type honesty.
                raise BackendCoolingDown(
                    "all pool accounts are cooling down after rate limit"
                )
            try:
                async with self.factories[name].lease() as session:
                    session._webgpt_account_name = name
                    if self.health is not None:
                        self.health.mark_result(name, ok=True)
                    yield session
            except Exception as exc:
                self._mark_failure(name, exc)
                raise
            return
        last_cooling: BackendCoolingDown | None = None
        tried: list[str] = []
        # Explicit pins are never silently rerouted: one attempt, exactly the
        # pre-row-S outcome when that account refuses acquisition.
        max_attempts = 1 if account_name is not None else len(self.factories)
        while len(tried) < max_attempts:
            name = await self._pick_name(account_name, exclude=tuple(tried))
            if name is None:
                break
            tried.append(name)
            entered = False
            try:
                async with self.factories[name].lease() as session:
                    session._webgpt_account_name = name
                    if self.health is not None:
                        self.health.mark_result(name, ok=True)
                    entered = True
                    yield session
                    return
            except BackendCoolingDown as exc:
                if entered:
                    # Raised by the consumer mid-turn, not by acquisition:
                    # never retry, surface it unchanged.
                    raise
                last_cooling = exc
                continue
            except Exception as exc:
                self._mark_failure(name, exc)
                raise
        if last_cooling is not None:
            raise last_cooling
        raise BackendCoolingDown("all pool accounts are cooling down after rate limit")

    async def start(self) -> None:
        started: list[Any] = []
        for factory in self.factories.values():
            start = getattr(factory, "start", None)
            if start is None:
                continue
            # Track the factory before awaiting start(): a start() that opens
            # resources and *then* raises must still be rolled back below. On
            # success the entry simply stays; close() only ever runs on this
            # rollback path, so keeping it is harmless.
            started.append(factory)
            try:
                await start()
            except BaseException:
                # Roll back every account factory touched so far (including
                # one that failed midway); otherwise a failing second account
                # leaks the first one's browser/process.
                for started_factory in reversed(started):
                    with suppress(Exception):
                        await started_factory.close()
                raise

    async def stats(self) -> WorkerFactoryStats:
        values = [await factory.stats() for factory in self.factories.values()]
        return WorkerFactoryStats(
            max_workers=sum(item.max_workers for item in values),
            live_workers=sum(item.live_workers for item in values),
            idle_workers=sum(item.idle_workers for item in values),
            leased_workers=sum(item.leased_workers for item in values),
            queue_waiters=sum(item.queue_waiters for item in values),
            created_workers=sum(item.created_workers for item in values),
            closed_workers=sum(item.closed_workers for item in values),
        )

    @property
    def browsers_connected(self) -> bool:
        return all(
            bool(getattr(factory.browser_manager, "connected", False))
            for factory in self.factories.values()
        )

    async def close(self) -> None:
        for factory in self.factories.values():
            await factory.close()


__all__ = ["MultiAccountWorkerFactory"]
