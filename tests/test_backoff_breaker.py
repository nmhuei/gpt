"""BACKOFF-BREAKER: global rate-limit circuit breaker at the factory layer.

Covers (trace-forensics 2026-08-25): a RATE_LIMITED turn opens a process-wide
cooldown that blocks subsequent acquire() calls, expiry admits exactly one
half-open probe, a failed probe doubles the window (capped), a successful probe
reopens fully, and WEBGPT_RATELIMIT_COOLDOWN_SECONDS=0 disables the breaker.
Non-rate-limit failures never change breaker state.
"""

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gpt.state import RateLimited, SessionState, WebChatError
from gpt.transport.breaker import (
    BackendCoolingDown,
    BreakerTicket,
    RateLimitBreaker,
    global_rate_limit_breaker,
    reset_global_rate_limit_breaker,
)
from gpt.transport.challenge import ChallengeDetectedError
from gpt.transport.factory import ChatGPTWorkerFactory


class FakeManager:
    def __init__(self):
        self.start = AsyncMock()
        self.stop = AsyncMock()


class FakeSession:
    def __init__(self, name: str):
        self.name = name
        self.state = SessionState.READY
        self.close = AsyncMock()


class FakeClock:
    """Deterministic monotonic clock so no test depends on real sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_factory(monkeypatch, breaker=None, **kwargs) -> ChatGPTWorkerFactory:
    counter = 0

    async def create(**_kwargs):
        nonlocal counter
        counter += 1
        return FakeSession(f"s{counter}")

    monkeypatch.setattr("gpt.transport.factory.ChatGPTWebSession.create", create)
    return ChatGPTWorkerFactory(
        cast(Any, FakeManager()),
        max_workers=2,
        warm_workers=0,
        rate_limit_breaker=breaker,
        **kwargs,
    )


async def rate_limited_turn(factory) -> None:
    """Run one lease whose turn ends classified as RATE_LIMITED."""
    with pytest.raises(RateLimited):
        async with factory.lease() as session:
            session.state = SessionState.RATE_LIMITED
            raise RateLimited("quota exhausted")


# (a) A rate-limited turn must block the next acquire for the whole window.
@pytest.mark.anyio
async def test_rate_limit_opens_cooldown_and_blocks_next_acquire(monkeypatch):
    clock = FakeClock()
    breaker = RateLimitBreaker(cooldown_seconds=90.0, clock=clock)
    factory = make_factory(monkeypatch, breaker=breaker)

    await rate_limited_turn(factory)
    assert breaker.snapshot().state == "open"

    with pytest.raises(BackendCoolingDown) as excinfo:
        await factory.acquire()
    assert "cooling down" in str(excinfo.value)
    # Blocked acquire fails fast without spinning up replacement workers.
    assert (await factory.stats()).created_workers == 1

    clock.advance(89.0)
    with pytest.raises(BackendCoolingDown):
        await factory.acquire()

    await factory.close()


# (b) After the window elapses exactly ONE half-open probe may pass.
@pytest.mark.anyio
async def test_expired_cooldown_admits_exactly_one_half_open_probe(monkeypatch):
    clock = FakeClock()
    breaker = RateLimitBreaker(cooldown_seconds=60.0, clock=clock)
    factory = make_factory(monkeypatch, breaker=breaker)

    await rate_limited_turn(factory)
    clock.advance(61.0)

    probe_id, _probe_session = await factory.acquire()
    with pytest.raises(BackendCoolingDown):
        await factory.acquire()  # everyone else still waits during the probe

    # Probe finishes healthy (READY release): breaker closes fully again...
    await factory.release(probe_id)
    assert breaker.snapshot().state == "closed"
    # ...and normal acquisition resumes immediately.
    worker_id, session = await factory.acquire()
    assert session.name.startswith("s")
    await factory.release(worker_id)

    await factory.close()


# (c) A probe that hits the rate limit again doubles the next window (capped).
@pytest.mark.anyio
async def test_failed_probe_doubles_cooldown_with_cap(monkeypatch):
    clock = FakeClock()
    breaker = RateLimitBreaker(
        cooldown_seconds=10.0,
        max_cooldown_seconds=25.0,
        backoff_factor=2.0,
        clock=clock,
    )
    factory = make_factory(monkeypatch, breaker=breaker)

    await rate_limited_turn(factory)  # window 1: 10s penalty
    clock.advance(11.0)
    await rate_limited_turn(factory)  # failed probe -> penalty doubles to 20
    assert breaker.snapshot().penalty_seconds == pytest.approx(20.0)
    with pytest.raises(BackendCoolingDown):
        await factory.acquire()

    clock.advance(19.0)  # 20s window not yet elapsed
    with pytest.raises(BackendCoolingDown):
        await factory.acquire()
    clock.advance(1.0)

    await rate_limited_turn(factory)  # second failed probe: min(40, cap)=25
    assert breaker.snapshot().penalty_seconds == pytest.approx(25.0)
    clock.advance(24.0)
    with pytest.raises(BackendCoolingDown):
        await factory.acquire()
    clock.advance(1.0)
    worker_id, _session = await factory.acquire()  # capped window elapsed
    await factory.release(worker_id)

    await factory.close()


def test_parallel_trips_inside_window_do_not_stack_penalty():
    clock = FakeClock()
    breaker = RateLimitBreaker(cooldown_seconds=90.0, clock=clock)
    breaker.trip("account A")
    clock.advance(30.0)
    breaker.trip("account B hit same backend limit")  # must NOT extend to 180s
    clock.advance(59.0)
    with pytest.raises(BackendCoolingDown):
        breaker.before_acquire()
    clock.advance(1.0)
    assert isinstance(breaker.before_acquire(), BreakerTicket)


# (d) A successful probe closes the breaker completely and resets backoff.
@pytest.mark.anyio
async def test_successful_probe_reopens_breaker_and_resets_backoff(monkeypatch):
    clock = FakeClock()
    breaker = RateLimitBreaker(cooldown_seconds=45.0, clock=clock)
    factory = make_factory(monkeypatch, breaker=breaker)

    await rate_limited_turn(factory)
    clock.advance(46.0)
    async with factory.lease() as session:  # clean probe turn
        assert session.state == SessionState.READY
    assert breaker.snapshot().state == "closed"
    assert breaker.snapshot().penalty_seconds == pytest.approx(45.0)

    # Next rate limit starts from the BASE window again, not a doubled one.
    await rate_limited_turn(factory)
    clock.advance(44.0)
    with pytest.raises(BackendCoolingDown):
        await factory.acquire()
    clock.advance(1.0)
    worker_id, _session = await factory.acquire()
    await factory.release(worker_id)

    await factory.close()


# (e) WEBGPT_RATELIMIT_COOLDOWN_SECONDS=0 disables the breaker entirely.
@pytest.mark.anyio
async def test_zero_cooldown_env_disables_breaker(monkeypatch):
    monkeypatch.setenv("WEBGPT_RATELIMIT_COOLDOWN_SECONDS", "0")
    reset_global_rate_limit_breaker()
    try:
        breaker = global_rate_limit_breaker()
        assert breaker.enabled is False
        factory = make_factory(monkeypatch)  # uses the global singleton

        await rate_limited_turn(factory)
        assert breaker.snapshot().trips == 0
        # No cooldown at all: the very next turn is admitted normally.
        async with factory.lease() as session:
            assert session.state == SessionState.READY
        await factory.close()
    finally:
        reset_global_rate_limit_breaker()


def test_env_parsing_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("WEBGPT_RATELIMIT_COOLDOWN_SECONDS", raising=False)
    monkeypatch.delenv("WEBGPT_RATELIMIT_MAX_COOLDOWN_SECONDS", raising=False)
    breaker = RateLimitBreaker.from_env()
    assert breaker.cooldown_seconds == pytest.approx(90.0)
    assert breaker.max_cooldown_seconds == pytest.approx(600.0)

    monkeypatch.setenv("WEBGPT_RATELIMIT_COOLDOWN_SECONDS", "120")
    monkeypatch.setenv("WEBGPT_RATELIMIT_MAX_COOLDOWN_SECONDS", "900")
    breaker = RateLimitBreaker.from_env()
    assert breaker.cooldown_seconds == pytest.approx(120.0)
    assert breaker.max_cooldown_seconds == pytest.approx(900.0)

    monkeypatch.setenv("WEBGPT_RATELIMIT_COOLDOWN_SECONDS", "not-a-number")
    assert RateLimitBreaker.from_env().cooldown_seconds == pytest.approx(90.0)


# Requirement 3: failures that are NOT rate limits never touch the breaker.
@pytest.mark.anyio
async def test_non_rate_limit_failures_leave_breaker_closed(monkeypatch):
    clock = FakeClock()
    breaker = RateLimitBreaker(cooldown_seconds=90.0, clock=clock)
    factory = make_factory(monkeypatch, breaker=breaker)

    # Retryable (reusable) failure.
    with pytest.raises(WebChatError):
        async with factory.lease() as session:
            session.state = SessionState.RETRYABLE_ERROR
            raise WebChatError("transient renderer hiccup")
    assert breaker.snapshot().state == "closed"
    worker_id, _session = await factory.acquire()
    await factory.release(worker_id)

    # Even a fatal, worker-killing failure must not open the cooldown.
    with pytest.raises(WebChatError):
        async with factory.lease() as session:
            session.state = SessionState.FATAL_ERROR
            raise WebChatError("hard failure")
    assert breaker.snapshot().state == "closed"
    worker_id, _session = await factory.acquire()
    await factory.release(worker_id)

    await factory.close()


# Bootstrap-time rate limit (login wall during session.create) also trips it.
@pytest.mark.anyio
async def test_bootstrap_rate_limit_trips_cooldown(monkeypatch):
    clock = FakeClock()
    breaker = RateLimitBreaker(cooldown_seconds=30.0, clock=clock)
    manager = FakeManager()

    async def create(**_kwargs):
        raise RateLimited("ChatGPT anonymous quota exhausted")

    monkeypatch.setattr("gpt.transport.factory.ChatGPTWebSession.create", create)
    factory = ChatGPTWorkerFactory(
        manager, max_workers=1, warm_workers=0, rate_limit_breaker=breaker
    )
    with pytest.raises(RateLimited):
        await factory.acquire()
    with pytest.raises(BackendCoolingDown):
        await factory.acquire()
    assert (await factory.stats()).queue_waiters == 0
    await factory.close()


# LIMIT-SIGNATURE-TAXONOMY (2026-08-26): a Cloudflare interstitial surfacing
# as ChallengeDetectedError (403/503 — even a 429 envelope carrying HTML) is
# NOT a quota verdict. The lease path must leave the breaker closed and the
# next acquisition must be admitted immediately.
@pytest.mark.anyio
@pytest.mark.parametrize("status", [403, 429, 503])
async def test_challenge_failure_never_trips_breaker(monkeypatch, status):
    clock = FakeClock()
    breaker = RateLimitBreaker(cooldown_seconds=90.0, clock=clock)
    factory = make_factory(monkeypatch, breaker=breaker)

    with pytest.raises(ChallengeDetectedError):
        async with factory.lease() as session:
            session.state = SessionState.FATAL_ERROR
            raise ChallengeDetectedError(
                "challenge page, not a quota verdict", status_code=status
            )

    snapshot = breaker.snapshot()
    assert snapshot.state == "closed"
    assert snapshot.trips == 0

    worker_id, _session = await factory.acquire()
    await factory.release(worker_id)
    await factory.close()
