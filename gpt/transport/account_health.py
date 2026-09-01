"""Per-account health tracking with injectable clock (roadmap track 3A/A1).

The tracker never talks to a browser itself: callers feed results in through
:meth:`AccountHealthTracker.mark_result` and query :meth:`~AccountHealthTracker.available_names`
before picking an account. Cooldowns expire purely based on the injected
clock, so tests drive time deterministically.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

HEALTHY = "healthy"
DEGRADED = "degraded"
COOLDOWN = "cooldown"
UNKNOWN = "unknown"


@dataclass
class AccountHealthState:
    """Health bookkeeping for one named account."""

    status: str = UNKNOWN
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    last_checked_at: float | None = None


@dataclass
class AccountHealthTracker:
    """Track per-account failures with expiring cooldowns."""

    clock: Callable[[], float] = time.monotonic
    states: dict[str, AccountHealthState] = field(default_factory=dict)

    def state(self, name: str) -> AccountHealthState:
        return self.states.setdefault(name, AccountHealthState())

    def snapshot(self, name: str) -> AccountHealthState:
        """Return a copy of the tracked state for one account."""
        current = self.state(name)
        return AccountHealthState(
            status=current.status,
            cooldown_until=current.cooldown_until,
            consecutive_failures=current.consecutive_failures,
            last_checked_at=current.last_checked_at,
        )

    def is_available(self, name: str) -> bool:
        return self.clock() >= self.state(name).cooldown_until

    def mark_result(
        self, name: str, ok: bool, cooldown_seconds: float = 0
    ) -> AccountHealthState:
        state = self.state(name)
        now = self.clock()
        state.last_checked_at = now
        if ok:
            state.status = HEALTHY
            state.consecutive_failures = 0
            state.cooldown_until = 0.0
        else:
            state.consecutive_failures += 1
            if cooldown_seconds > 0:
                state.status = COOLDOWN
                state.cooldown_until = now + max(0.0, float(cooldown_seconds))
            else:
                state.status = DEGRADED
                state.cooldown_until = 0.0
        return state

    def available_names(self, all_names: Iterable[str]) -> list[str]:
        """Names whose cooldown expired; falls back to all names when none are."""
        names = list(all_names)
        now = self.clock()
        available = [
            name for name in names if now >= self.state(name).cooldown_until
        ]
        if not available:
            logger.warning(
                "All %d account(s) are in cooldown; falling back to full list.",
                len(names),
            )
            return names
        return available


async def check_account_health(
    store: Any,
    name: str,
    *,
    page_context: Callable[[str], Any] | None = None,
) -> bool:
    """Verify one account's web session and persist the auth status.

    Reuses :func:`gpt.auth.accounts.browser_session_authenticated`. Tests
    inject ``page_context`` (an async context manager yielding a fake page)
    instead of launching a real browser.
    """
    from gpt.auth.accounts import browser_session_authenticated

    if page_context is None:
        page_context = _profile_page_context
    record = store.get(name)
    async with page_context(record.profile_dir) as page:
        ok = bool(await browser_session_authenticated(page))
    store.update_status(record.name, "authenticated" if ok else "login_required")
    return ok


@asynccontextmanager
async def _profile_page_context(profile_dir: str) -> AsyncIterator[Any]:
    """Open one headless persistent page for the given account profile."""
    from gpt.transport.browser import BrowserManager

    manager = BrowserManager(headless=True, persistent=True, profile_dir=profile_dir)
    try:
        yield await manager.new_page()
    finally:
        await manager.stop()


async def periodic_health_loop(
    tracker: AccountHealthTracker,
    store: Any,
    names: Sequence[str],
    interval: float,
    checker: Callable[[Any, str], Any] | None = None,
    *,
    failure_cooldown_seconds: float = 300.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Poll every account sequentially, forever, until ``stop_event`` is set."""
    if checker is None:
        checker = check_account_health
    while stop_event is None or not stop_event.is_set():
        for name in names:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                ok = bool(await checker(store, name))
            except Exception as exc:
                logger.warning("Health check for %s failed: %s", name, exc)
                ok = False
            tracker.mark_result(
                name, ok, cooldown_seconds=failure_cooldown_seconds
            )
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(max(0.0, interval))


__all__ = [
    "COOLDOWN",
    "DEGRADED",
    "HEALTHY",
    "UNKNOWN",
    "AccountHealthState",
    "AccountHealthTracker",
    "check_account_health",
    "periodic_health_loop",
]
