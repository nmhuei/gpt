"""Global rate-limit circuit breaker for backend worker acquisition.

Forensics (docs/reports/trace-forensics-2026-08-25.md): when the ChatGPT Web
backend rate-limits, the gateway used to fail over to another worker
immediately and resend the full prompt -- 228 wasted resends burning extra
quota in two days. This breaker makes the FIRST rate-limit hit open a
process-wide cooldown window: every ``acquire()`` inside the window fails fast
with :class:`BackendCoolingDown` instead of being handed a fresh worker. When
the window elapses, exactly ONE half-open probe request may pass; a successful
probe closes the breaker completely, while a rate-limited probe doubles the
cooldown (capped). Failures that are NOT rate limits never touch this state.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

DEFAULT_COOLDOWN_SECONDS = 90.0
DEFAULT_MAX_COOLDOWN_SECONDS = 600.0
DEFAULT_BACKOFF_FACTOR = 2.0

# USAGE-INTROSPECTION: quota pressure at/above this percent (as reported by
# the usage poller) is treated as worth one short protective cooldown while
# the breaker is fully closed.  The breaker owns this policy; the poller only
# forwards raw readings via :meth:`RateLimitBreaker.advise_pressure`.
USAGE_PRESSURE_THRESHOLD = 85.0

# RESET-AWARE-COOLDOWN (ROADMAP row S): the usage payload carries an absolute
# ``reset_at`` unix timestamp that used to be ignored.  When the poller also
# forwards how far the window is from resetting, advisory cooldowns adapt:
#
# - an advisory cooldown is capped at ``seconds_until_reset + buffer`` so it
#   never outlives the natural reset by more than the buffer -- waiting for
#   the reset is strictly cheaper than a long blind cooldown;
# - a window resetting within ADVISORY_RESET_IMMINENT_SECONDS is not worth
#   opening an advisory cooldown at all: the reset arrives sooner than any
#   useful cooldown and costs zero quota.
ADVISORY_RESET_BUFFER_SECONDS = 60.0
ADVISORY_RESET_IMMINENT_SECONDS = 90.0

COOLDOWN_ENV = "WEBGPT_RATELIMIT_COOLDOWN_SECONDS"
MAX_COOLDOWN_ENV = "WEBGPT_RATELIMIT_MAX_COOLDOWN_SECONDS"


class BackendCoolingDown(RuntimeError):
    """Raised by acquire()/lease() while the rate-limit breaker is open."""


@dataclass(frozen=True)
class BreakerTicket:
    """Opaque marker proving this caller holds the current half-open probe."""

    generation: int


@dataclass(frozen=True)
class RateLimitBreakerSnapshot:
    """Point-in-time observability for stats endpoints and tests."""

    state: str
    remaining_seconds: float
    penalty_seconds: float
    trips: int
    enabled: bool


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class RateLimitBreaker:
    """Closed -> Open -> Half-open breaker driven by backend rate limits.

    All state transitions are guarded by a ``threading.Lock`` that is never
    awaited while held, so one instance can safely be shared by every worker
    factory (and every account) across event loops -- that sharing is what
    makes the cooldown GLOBAL.
    """

    def __init__(
        self,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        *,
        max_cooldown_seconds: float = DEFAULT_MAX_COOLDOWN_SECONDS,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cooldown_seconds = float(cooldown_seconds)
        self.max_cooldown_seconds = float(max_cooldown_seconds)
        self.backoff_factor = float(backoff_factor)
        self._clock = clock
        self._lock = threading.Lock()
        self._deadline = 0.0  # 0 => fully closed
        self._penalty = self.cooldown_seconds
        self._probe_active = False
        self._generation = 0
        self._trips = 0
        # USAGE-INTROSPECTION: how many cooldowns were opened by advisory
        # pressure (not by an observed rate limit).  Purely observational.
        self._advisory_opens = 0

    @classmethod
    def from_env(cls) -> RateLimitBreaker:
        return cls(
            _env_seconds(COOLDOWN_ENV, DEFAULT_COOLDOWN_SECONDS),
            max_cooldown_seconds=_env_seconds(
                MAX_COOLDOWN_ENV, DEFAULT_MAX_COOLDOWN_SECONDS
            ),
        )

    @property
    def enabled(self) -> bool:
        return self.cooldown_seconds > 0

    def before_acquire(self) -> BreakerTicket | None:
        """Gate every worker acquisition.

        Returns ``None`` when the breaker is closed or disabled (proceed as
        usual). Returns a :class:`BreakerTicket` when this call becomes the
        single allowed half-open probe. Raises :class:`BackendCoolingDown`
        while the window is open or another probe is already in flight.
        """
        if not self.enabled:
            return None
        with self._lock:
            now = self._clock()
            if self._deadline > now:
                raise BackendCoolingDown(
                    "backend cooling down after rate limit "
                    f"(retry allowed in {self._deadline - now:.1f}s)"
                )
            if self._probe_active:
                raise BackendCoolingDown(
                    "backend cooling down: half-open probe already in flight"
                )
            if self._deadline <= 0.0:
                return None  # Fully closed: no change in behavior.
            self._generation += 1
            self._probe_active = True
            return BreakerTicket(self._generation)

    def trip(self, reason: str = "") -> None:
        """Open the breaker after observing a backend rate limit."""
        if not self.enabled:
            return
        with self._lock:
            now = self._clock()
            if self._deadline <= now:
                if self._probe_active and self._deadline > 0.0:
                    # The half-open probe hit another rate limit: widen the
                    # next window by the backoff factor, capped.
                    self._penalty = min(
                        self._penalty * self.backoff_factor,
                        self.max_cooldown_seconds,
                    )
                self._deadline = now + self._penalty
            # A trip while already inside the open window neither moves the
            # expiry nor compounds the penalty: parallel turns failing on the
            # same backend limit must not stack exponential windows.
            self._probe_active = False
            self._trips += 1

    def advise_pressure(
        self,
        used_percent: float,
        *,
        seconds_until_reset: float | None = None,
    ) -> bool:
        """Advisory-only quota-pressure signal from the usage poller.

        USAGE-INTROSPECTION (ROADMAP row M): instead of waiting for a real
        429, the usage poller forwards each codex ``rate_limit.primary_window``
        reading here.  The BREAKER -- never the poller -- owns the policy:
        while fully closed and ``used_percent`` is at/above
        :data:`USAGE_PRESSURE_THRESHOLD`, this opens one short cooldown window
        sized exactly like a first trip so acquisitions fail fast and the
        multi-account pool gets a breather BEFORE the backend starts
        rejecting.  Advisory opens are deliberately conservative:

        - they never compound the backoff penalty (only a real rate-limited
          half-open probe may widen it),
        - they never extend an already-open window,
        - and while a half-open probe is in flight (expired deadline with a
          live ticket) advice is ignored entirely so probe bookkeeping
          (``record_success``/``finish_probe``) stays exact.

        RESET-AWARE-COOLDOWN (row S): ``seconds_until_reset`` is the caller's
        wall-clock distance to the window's absolute ``reset_at`` (None when
        unknown).  When supplied, the advisory window length becomes
        ``min(cooldown_seconds, seconds_until_reset + buffer)`` -- capped just
        past the natural reset -- and a window resetting within
        :data:`ADVISORY_RESET_IMMINENT_SECONDS` skips the open entirely
        (waiting for the reset is free; any cooldown would be redundant).
        A negative/past reset counts as imminent.

        Returns True only when this call actually opened a new window.
        """
        if not self.enabled:
            return False
        if used_percent < USAGE_PRESSURE_THRESHOLD:
            return False
        if (
            seconds_until_reset is not None
            and seconds_until_reset < ADVISORY_RESET_IMMINENT_SECONDS
        ):
            # Imminent natural reset: skip the advisory open entirely.
            return False
        with self._lock:
            if self._deadline > 0.0:
                # Open or half-open: real traffic is already gated; advice
                # must not move expiry or wedge the single-probe slot.
                return False
            duration = self.cooldown_seconds
            if seconds_until_reset is not None:
                duration = min(
                    duration, seconds_until_reset + ADVISORY_RESET_BUFFER_SECONDS
                )
            now = self._clock()
            self._deadline = now + max(duration, 0.0)
            self._advisory_opens += 1
            return True

    @property
    def advisory_opens(self) -> int:
        """How many cooldown windows were opened by usage-pressure advice."""
        return self._advisory_opens

    def record_success(self, ticket: BreakerTicket | None) -> None:
        """Probe succeeded: close the breaker fully and reset backoff."""
        if ticket is None:
            return
        with self._lock:
            if self._probe_active and ticket.generation == self._generation:
                self._deadline = 0.0
                self._probe_active = False
                self._penalty = self.cooldown_seconds

    def finish_probe(self, ticket: BreakerTicket | None) -> None:
        """Probe ended without a clear verdict: free the slot, stay half-open.

        Also used to abandon a ticket when acquisition itself failed (queue
        timeout, cancellation, bootstrap error) so the single-probe slot can
        never wedge the breaker shut.
        """
        if ticket is None:
            return
        with self._lock:
            if self._probe_active and ticket.generation == self._generation:
                self._probe_active = False

    def snapshot(self) -> RateLimitBreakerSnapshot:
        with self._lock:
            now = self._clock()
            if self._deadline <= 0.0:
                state = "closed"
                remaining = 0.0
            elif now < self._deadline:
                state = "open"
                remaining = self._deadline - now
            else:
                state = "half_open"
                remaining = 0.0
            return RateLimitBreakerSnapshot(
                state=state,
                remaining_seconds=max(0.0, remaining),
                penalty_seconds=self._penalty,
                trips=self._trips,
                enabled=self.enabled,
            )


_GLOBAL_BREAKER: RateLimitBreaker | None = None
_GLOBAL_LOCK = threading.Lock()


def global_rate_limit_breaker() -> RateLimitBreaker:
    """Process-wide breaker shared by every worker factory by default."""
    global _GLOBAL_BREAKER
    with _GLOBAL_LOCK:
        if _GLOBAL_BREAKER is None:
            _GLOBAL_BREAKER = RateLimitBreaker.from_env()
        return _GLOBAL_BREAKER


def reset_global_rate_limit_breaker() -> None:
    """Drop the singleton so the next call re-reads env (tests, operators)."""
    global _GLOBAL_BREAKER
    with _GLOBAL_LOCK:
        _GLOBAL_BREAKER = None


# ---------------------------------------------------------------------------
# POOL-POLLER-PERACCT: opt-in cross-account emergency brake
# ---------------------------------------------------------------------------

# When K DISTINCT pool accounts eat REAL rate limits inside one sliding window,
# the throttle is probably IP/edge-level rather than per-account quota; the
# pool would otherwise burn through its remaining accounts one 429 at a time.
# The cross brake advises EVERY pool breaker open for one short window so the
# whole pool breathes together instead. Default OFF; opt in via
# ``WEBGPT_POOL_CROSS_BRAKE`` (see :func:`parse_cross_brake_spec`).
CROSS_BRAKE_ENV = "WEBGPT_POOL_CROSS_BRAKE"
CROSS_BRAKE_DEFAULT_THRESHOLD = 2
CROSS_BRAKE_DEFAULT_WINDOW_SECONDS = 600.0

_CROSS_BRAKE_TRUTHY = frozenset({"1", "true", "yes", "on"})


def parse_cross_brake_spec(raw: str) -> tuple[int, float] | None:
    """Parse the ``WEBGPT_POOL_CROSS_BRAKE`` value into ``(K, window_seconds)``.

    Accepted shapes: bare truthy flags (``1``/``true``/``yes``/``on``) use the
    defaults, a bare integer sets only K, and ``K@seconds`` sets both (e.g.
    ``3@300``). Unset/empty/disabled values AND anything unparseable return
    None — garbled input deliberately means OFF, because a typo must never be
    able to arm an emergency brake by accident.
    """
    text = raw.strip().lower()
    if not text:
        return None
    if text in _CROSS_BRAKE_TRUTHY:
        return (CROSS_BRAKE_DEFAULT_THRESHOLD, CROSS_BRAKE_DEFAULT_WINDOW_SECONDS)
    window = CROSS_BRAKE_DEFAULT_WINDOW_SECONDS
    if "@" in text:
        head, _, tail = text.partition("@")
        try:
            window = float(tail)
        except ValueError:
            return None
        text = head
    try:
        threshold = int(text)
    except ValueError:
        return None
    if threshold < 1 or window <= 0.0:
        return None
    return (threshold, window)


class CrossBrake:
    """Advise every pool breaker open once K distinct accounts hit real 429s.

    Purely advisory and one-way, exactly like the usage poller: it only calls
    :meth:`RateLimitBreaker.advise_pressure`, so already-open windows ignore
    it entirely (natural dedup — repeated hits cannot stack or extend the
    shared cooldown) and thresholding stays breaker policy. Hits are counted
    per DISTINCT account name inside a sliding window; a hit that is not a
    genuine backend rate limit (auth trouble, challenges) must never reach
    :meth:`record`.
    """

    def __init__(
        self,
        breakers: Mapping[str, RateLimitBreaker],
        *,
        threshold: int = CROSS_BRAKE_DEFAULT_THRESHOLD,
        window_seconds: float = CROSS_BRAKE_DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.breakers = dict(breakers)
        self.threshold = max(1, int(threshold))
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._hits: deque[tuple[str, float]] = deque()
        # How many advise rounds actually opened something (observability).
        self.fires = 0

    def _prune(self) -> set[str]:
        now = self._clock()
        cutoff = now - self.window_seconds
        while self._hits and self._hits[0][1] <= cutoff:
            self._hits.popleft()
        return {account for account, _ in self._hits}

    def record(self, name: str) -> bool:
        """Register one real rate-limit hit on ``name``.

        Returns True when this call reached the K/N threshold AND advised at
        least one still-closed breaker open. Every pool breaker is advised —
        including the accounts that just tripped, whose own windows are
        typically already open so the advice no-ops there; advising them too
        also covers hits surfaced before their leaf breaker could trip.
        """
        self._hits.append((name, self._clock()))
        distinct = self._prune()
        if len(distinct) < self.threshold:
            return False
        advised_any = False
        for breaker in self.breakers.values():
            if breaker.advise_pressure(100.0):
                advised_any = True
        if advised_any:
            self.fires += 1
        return advised_any

    def distinct_tripped(self) -> tuple[str, ...]:
        """Distinct account names with an unexpired hit (introspection/tests)."""
        return tuple(sorted(self._prune()))


def cross_brake_from_env(
    breakers: Mapping[str, RateLimitBreaker],
    environ: Mapping[str, str] | None = None,
) -> CrossBrake | None:
    """Build the opt-in cross brake from ``WEBGPT_POOL_CROSS_BRAKE``.

    Returns None (feature off) when the env is unset/garbled or when there is
    no per-account breaker pool to brake — the default path constructs nothing.
    """
    env = os.environ if environ is None else environ
    spec = parse_cross_brake_spec(env.get(CROSS_BRAKE_ENV, ""))
    if spec is None or not breakers:
        return None
    threshold, window = spec
    return CrossBrake(breakers, threshold=threshold, window_seconds=window)


__all__ = [
    "ADVISORY_RESET_BUFFER_SECONDS",
    "ADVISORY_RESET_IMMINENT_SECONDS",
    "COOLDOWN_ENV",
    "CROSS_BRAKE_DEFAULT_THRESHOLD",
    "CROSS_BRAKE_DEFAULT_WINDOW_SECONDS",
    "CROSS_BRAKE_ENV",
    "DEFAULT_BACKOFF_FACTOR",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_MAX_COOLDOWN_SECONDS",
    "MAX_COOLDOWN_ENV",
    "USAGE_PRESSURE_THRESHOLD",
    "BackendCoolingDown",
    "BreakerTicket",
    "CrossBrake",
    "RateLimitBreaker",
    "RateLimitBreakerSnapshot",
    "cross_brake_from_env",
    "global_rate_limit_breaker",
    "parse_cross_brake_spec",
    "reset_global_rate_limit_breaker",
]
