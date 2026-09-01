"""USAGE-INTROSPECTION: usage poller -> breaker one-way advice.

Covers (docs/automation/ROADMAP.md row USAGE-INTROSPECTION): a successful
usage scrape forwards ``rate_limit.primary_window.used_percent`` to the
breaker's new ``advise_pressure``; missing/mis-shaped fields skip the cycle;
401/403 mutes the poller silently; other failures just skip; and with
``WEBGPT_USAGE_POLL_SECONDS`` unset (default OFF) nothing ever polls.

RESET-AWARE-COOLDOWN (row S): the window's absolute ``reset_at`` is parsed
and forwarded as ``seconds_until_reset``; advisory cooldowns are capped just
past the reset and skipped entirely when the window resets imminently.
All HTTP and tokens are fakes -- no network, no credential files touched.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from gpt.transport.breaker import (
    ADVISORY_RESET_BUFFER_SECONDS,
    ADVISORY_RESET_IMMINENT_SECONDS,
    USAGE_PRESSURE_THRESHOLD,
    BackendCoolingDown,
    RateLimitBreaker,
)
from gpt.transport.usage_poller import (
    ACCOUNT_AUTH_JSON_ENV_PREFIX,
    POLL_SECONDS_ENV,
    POOL_AUTH_DIR_ENV,
    PoolPressureBoard,
    RateLimitWindow,
    UsagePoller,
    account_auth_json_path,
    create_account_pollers,
    default_pool_auth_dir,
    extract_rate_limit_window,
    extract_used_percent,
    make_web_token_cache_provider,
)


class FakeClock:
    """Deterministic monotonic clock so no test depends on real sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SpyBreaker:
    """Records advise_pressure calls; never opens anything."""

    def __init__(self) -> None:
        # Each entry: (used_percent, seconds_until_reset-or-None).
        self.calls: list[tuple[float, float | None]] = []
        self.opened = False

    def advise_pressure(
        self,
        used_percent: float,
        *,
        seconds_until_reset: float | None = None,
    ) -> bool:
        self.calls.append((used_percent, seconds_until_reset))
        return self.opened


class FakeHttp:
    """Injectable blocking GET returning canned responses per call."""

    def __init__(
        self,
        status: int = 200,
        payload: Any = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.on_call: asyncio.Event | None = None

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, Any]:
        self.calls.append((url, dict(headers)))
        if self.on_call is not None:
            self.on_call.set()
        if self.error is not None:
            raise self.error
        return self.status, self.payload


def usage_payload(percent: Any, reset_at: Any = None) -> dict[str, Any]:
    """Canonical-shaped payload; ``reset_at`` defaults to a far-future unix
    stamp so the default wall clock keeps the pre-reset-aware semantics
    (advisory opens normally instead of hitting the imminent-skip rule)."""
    if reset_at is None:
        reset_at = int(time.time()) + 86_400
    return {
        "rate_limit": {"primary_window": {"used_percent": percent, "reset_at": reset_at}}
    }


async def token() -> str | None:
    return "tok-test"


# ---------------------------------------------------------------------------
# Poller cycles (fake HTTP)
# ---------------------------------------------------------------------------


async def test_success_poll_advises_breaker_above_threshold(monkeypatch):
    monkeypatch.delenv(POLL_SECONDS_ENV, raising=False)
    breaker = RateLimitBreaker(cooldown_seconds=60.0, clock=FakeClock())
    http = FakeHttp(status=200, payload=usage_payload(91.4))
    poller = UsagePoller(
        breaker, poll_seconds=30.0, url="https://x/usage",
        token_provider=token, http_get=http,
    )

    reading = await poller.poll_once()

    assert reading is not None and reading.used_percent == pytest.approx(91.4)
    assert reading.advised is True
    assert len(http.calls) == 1
    url, headers = http.calls[0]
    assert url == "https://x/usage"
    assert headers["authorization"] == "Bearer tok-test"
    # Advice actually opened the protective window: acquire fails fast.
    with pytest.raises(BackendCoolingDown):
        breaker.before_acquire()
    assert breaker.snapshot().state == "open"
    assert breaker.advisory_opens == 1


async def test_below_threshold_reading_forwards_but_never_advises(monkeypatch):
    monkeypatch.delenv(POLL_SECONDS_ENV, raising=False)
    breaker = RateLimitBreaker(cooldown_seconds=60.0, clock=FakeClock())
    http = FakeHttp(status=200, payload=usage_payload(USAGE_PRESSURE_THRESHOLD - 1))
    poller = UsagePoller(breaker, poll_seconds=30.0, token_provider=token, http_get=http)

    reading = await poller.poll_once()

    assert reading is not None and reading.advised is False
    assert breaker.snapshot().state == "closed"
    assert breaker.before_acquire() is None  # acquisition unaffected.


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"rate_limit": None},
        {"rate_limit": {}},
        {"rate_limit": {"primary_window": None}},
        {"rate_limit": {"primary_window": {}}},
        usage_payload("77"),
        usage_payload(True),
        {"quota": {"used_percent": 99}},
    ],
)
async def test_missing_or_garbled_fields_skip_cycle_without_advising(payload):
    breaker = SpyBreaker()
    http = FakeHttp(status=200, payload=payload)
    poller = UsagePoller(
        breaker,  # type: ignore[arg-type]
        poll_seconds=30.0,
        token_provider=token,
        http_get=http,
    )

    assert await poller.poll_once() is None

    assert len(http.calls) == 1  # exactly one request was spent on the cycle
    assert breaker.calls == []  # and the breaker never heard about it


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_rejected_mutes_poller_silently(status):
    breaker = SpyBreaker()
    http = FakeHttp(status=status, payload={"error": "nope"})
    poller = UsagePoller(
        breaker,  # type: ignore[arg-type]
        poll_seconds=30.0,
        token_provider=token,
        http_get=http,
    )

    assert await poller.poll_once() is None
    assert poller.muted is True
    # Muted: later cycles are immediate quiet no-ops, zero extra requests.
    assert await poller.poll_once() is None
    assert await poller.poll_once() is None
    assert len(http.calls) == 1
    assert breaker.calls == []


async def test_server_error_skips_cycle_but_does_not_mute():
    breaker = SpyBreaker()
    http = FakeHttp(status=503, payload=None)
    poller = UsagePoller(
        breaker,  # type: ignore[arg-type]
        poll_seconds=30.0,
        token_provider=token,
        http_get=http,
    )

    assert await poller.poll_once() is None
    assert await poller.poll_once() is None

    assert poller.muted is False  # transient: next tick retries quietly
    assert len(http.calls) == 2


async def test_transport_error_skips_cycle_quietly():
    breaker = SpyBreaker()
    http = FakeHttp(error=RuntimeError("conn reset"))
    poller = UsagePoller(
        breaker,  # type: ignore[arg-type]
        poll_seconds=30.0,
        token_provider=token,
        http_get=http,
    )

    assert await poller.poll_once() is None
    assert breaker.calls == []
    assert poller.state()["muted"] is False


async def test_token_provider_failure_idles_the_cycle():
    breaker = SpyBreaker()
    http = FakeHttp(status=200, payload=usage_payload(95))

    async def broken_token() -> str | None:
        raise RuntimeError("refresh exploded")

    poller = UsagePoller(
        breaker,  # type: ignore[arg-type]
        poll_seconds=30.0,
        token_provider=broken_token,
        http_get=http,
    )

    assert await poller.poll_once() is None
    assert http.calls == []  # never reached the network without a bearer


async def test_clamps_out_of_range_used_percent(monkeypatch):
    monkeypatch.delenv(POLL_SECONDS_ENV, raising=False)
    breaker = RateLimitBreaker(cooldown_seconds=60.0, clock=FakeClock())

    http = FakeHttp(status=200, payload=usage_payload(250))
    poller = UsagePoller(breaker, poll_seconds=30.0, token_provider=token, http_get=http)
    reading = await poller.poll_once()
    assert reading is not None and reading.used_percent == 100.0

    http2 = FakeHttp(status=200, payload=usage_payload(-5))
    poller2 = UsagePoller(breaker, poll_seconds=30.0, token_provider=token, http_get=http2)
    reading2 = await poller2.poll_once()
    assert reading2 is not None and reading2.used_percent == 0.0


def test_extract_used_percent_shapes():
    assert extract_used_percent(usage_payload(12.5)) == 12.5
    assert extract_used_percent(None) is None
    assert extract_used_percent([1, 2]) is None
    assert extract_used_percent({"rate_limit": {"secondary_window": {}}}) is None


# ---------------------------------------------------------------------------
# Dormancy contract (default OFF)
# ---------------------------------------------------------------------------


async def test_flag_off_by_default_start_is_noop(monkeypatch):
    monkeypatch.delenv(POLL_SECONDS_ENV, raising=False)  # default OFF
    http = FakeHttp(status=200, payload=usage_payload(95))
    poller = UsagePoller(SpyBreaker(), token_provider=token, http_get=http)  # type: ignore[arg-type]

    assert poller.enabled is False
    assert poller.poll_seconds == 0.0
    assert poller.start() is None  # no task, no traffic
    assert http.calls == []
    await poller.stop()  # safe when never started


async def test_bad_env_value_falls_back_to_off(monkeypatch):
    monkeypatch.setenv(POLL_SECONDS_ENV, "not-a-number")
    poller = UsagePoller(RateLimitBreaker(), token_provider=token, http_get=FakeHttp())
    assert poller.enabled is False


async def test_env_enabled_loop_polls_until_stopped(monkeypatch):
    monkeypatch.setenv(POLL_SECONDS_ENV, "0.01")
    started = asyncio.Event()
    http = FakeHttp(status=200, payload=usage_payload(10))
    http.on_call = started
    breaker = SpyBreaker()
    poller = UsagePoller(
        breaker,  # type: ignore[arg-type]
        token_provider=token,
        http_get=http,
    )
    assert poller.enabled is True

    task = poller.start()
    try:
        assert task is not None
        await asyncio.wait_for(started.wait(), timeout=5.0)
        assert poller.start() is task  # idempotent while running
    finally:
        await poller.stop()
    assert task.cancelled()


async def test_default_codex_token_source_dormant_when_unset(monkeypatch):
    # Lazy-imported codex_auth manager sees WEBGPT_CODEX_AUTH_JSON unset ->
    # disabled -> the poller idles WITHOUT any HTTP attempt or file access.
    monkeypatch.delenv("WEBGPT_CODEX_AUTH_JSON", raising=False)
    monkeypatch.delenv(POLL_SECONDS_ENV, raising=False)
    breaker = SpyBreaker()
    http = FakeHttp(status=200, payload=usage_payload(95))
    poller = UsagePoller(breaker, poll_seconds=30.0, http_get=http)  # type: ignore[arg-type]

    assert await poller.poll_once() is None

    assert http.calls == []
    assert breaker.calls == []


# ---------------------------------------------------------------------------
# Breaker-side policy: advise_pressure (one-way, conservative)
# ---------------------------------------------------------------------------


def test_advise_pressure_opens_short_cooldown_when_closed():
    clock = FakeClock()
    breaker = RateLimitBreaker(cooldown_seconds=90.0, clock=clock)

    assert breaker.advise_pressure(86.0) is True

    snap = breaker.snapshot()
    assert snap.state == "open"
    assert snap.remaining_seconds == pytest.approx(90.0)
    assert snap.trips == 0  # advisory open is not an observed rate limit
    with pytest.raises(BackendCoolingDown):
        breaker.before_acquire()


def test_advise_at_exact_threshold_opens():
    breaker = RateLimitBreaker(cooldown_seconds=30.0, clock=FakeClock())
    assert breaker.advise_pressure(USAGE_PRESSURE_THRESHOLD) is True


def test_advise_below_threshold_is_noop():
    breaker = RateLimitBreaker(cooldown_seconds=30.0, clock=FakeClock())
    assert breaker.advise_pressure(84.999) is False
    assert breaker.snapshot().state == "closed"


def test_advise_never_extends_open_window():
    clock = FakeClock()
    breaker = RateLimitBreaker(cooldown_seconds=90.0, clock=clock)
    breaker.trip("real rate limit")
    clock.advance(10.0)  # 80s remain on the REAL window

    assert breaker.advise_pressure(99.0) is False

    remaining = breaker.snapshot().remaining_seconds
    assert remaining == pytest.approx(80.0)  # expiry untouched by advice


def test_advise_ignored_while_half_open_probe_in_flight():
    clock = FakeClock()
    breaker = RateLimitBreaker(cooldown_seconds=60.0, clock=clock)
    breaker.trip("real rate limit")
    clock.advance(61.0)  # expired -> half-open
    ticket = breaker.before_acquire()
    assert ticket is not None

    assert breaker.advise_pressure(95.0) is False  # probe bookkeeping sacred

    with pytest.raises(BackendCoolingDown):
        breaker.before_acquire()  # single-probe slot still held
    breaker.record_success(ticket)
    assert breaker.snapshot().state == "closed"  # success closes fully
    assert breaker.before_acquire() is None


def test_advise_on_disabled_breaker_is_noop():
    breaker = RateLimitBreaker(0.0, clock=FakeClock())  # disabled
    assert breaker.advise_pressure(99.0) is False
    assert breaker.before_acquire() is None  # stays pass-through


def test_advisory_open_expires_to_probe_and_keeps_penalty_base():
    clock = FakeClock()
    breaker = RateLimitBreaker(cooldown_seconds=45.0, clock=clock)

    assert breaker.advise_pressure(90.0) is True
    clock.advance(46.0)
    ticket = breaker.before_acquire()
    assert ticket is not None
    breaker.record_success(ticket)

    snap = breaker.snapshot()
    assert snap.state == "closed"
    assert snap.penalty_seconds == pytest.approx(45.0)  # backoff NOT compounded
    assert snap.trips == 0  # advice is not an observed rate limit
    assert breaker.advisory_opens == 1


# ---------------------------------------------------------------------------
# RESET-AWARE-COOLDOWN (row S): advise_pressure consumes seconds_until_reset
# ---------------------------------------------------------------------------


def test_advise_capped_just_past_reset():
    """Cooldown never outlives the natural reset by more than the buffer."""
    breaker = RateLimitBreaker(cooldown_seconds=300.0, clock=FakeClock())

    assert breaker.advise_pressure(90.0, seconds_until_reset=120.0) is True

    remaining = breaker.snapshot().remaining_seconds
    assert remaining == pytest.approx(120.0 + ADVISORY_RESET_BUFFER_SECONDS)


def test_advise_far_reset_keeps_plain_cooldown():
    """A distant reset leaves the ordinary cooldown unchanged."""
    breaker = RateLimitBreaker(cooldown_seconds=90.0, clock=FakeClock())

    assert breaker.advise_pressure(90.0, seconds_until_reset=3600.0) is True

    assert breaker.snapshot().remaining_seconds == pytest.approx(90.0)


def test_advise_at_imminent_boundary_still_opens():
    # The "<90s skips" rule is exclusive: exactly 90s may open, capped at
    # min(cooldown, 90 + buffer) like any other known reset distance.
    breaker = RateLimitBreaker(
        cooldown_seconds=ADVISORY_RESET_IMMINENT_SECONDS, clock=FakeClock()
    )

    assert (
        breaker.advise_pressure(
            95.0, seconds_until_reset=ADVISORY_RESET_IMMINENT_SECONDS
        )
        is True
    )
    # min(cooldown=90s, 90s + buffer) -> the plain cooldown wins.
    assert breaker.snapshot().remaining_seconds == pytest.approx(
        ADVISORY_RESET_IMMINENT_SECONDS
    )


@pytest.mark.parametrize("seconds", [ADVISORY_RESET_IMMINENT_SECONDS - 0.001, 0.0, -30.0])
def test_advise_skipped_when_reset_imminent(seconds):
    """Waiting out an imminent reset is free -- no advisory open, no gating."""
    breaker = RateLimitBreaker(cooldown_seconds=600.0, clock=FakeClock())

    assert breaker.advise_pressure(99.0, seconds_until_reset=seconds) is False

    assert breaker.snapshot().state == "closed"
    assert breaker.before_acquire() is None  # acquisitions stay unaffected
    assert breaker.advisory_opens == 0


async def test_poller_forwards_reset_distance_to_breaker():
    wall = FakeClock()  # injected as the wall clock; unix-ness is irrelevant
    breaker = SpyBreaker()
    http = FakeHttp(
        status=200, payload=usage_payload(88.0, reset_at=wall.now + 1_200)
    )
    poller = UsagePoller(
        breaker,  # type: ignore[arg-type]
        poll_seconds=30.0,
        token_provider=token,
        http_get=http,
        wall_clock=wall,
    )

    reading = await poller.poll_once()

    assert reading is not None
    assert reading.seconds_until_reset == pytest.approx(1_200.0)
    assert breaker.calls == [(pytest.approx(88.0), pytest.approx(1_200.0))]


async def test_poller_passes_none_when_reset_absent_or_garbled():
    for payload in (
        {"rate_limit": {"primary_window": {"used_percent": 91}}},
        {"rate_limit": {"primary_window": {"used_percent": 91, "reset_at": "soon"}}},
    ):
        breaker = SpyBreaker()
        http = FakeHttp(status=200, payload=payload)
        poller = UsagePoller(
            breaker,  # type: ignore[arg-type]
            poll_seconds=30.0,
            token_provider=token,
            http_get=http,
        )

        reading = await poller.poll_once()

        assert reading is not None and reading.seconds_until_reset is None
        assert breaker.calls == [(91.0, None)]
        assert poller.state()["last_seconds_until_reset"] is None


async def test_poller_imminent_reset_suppresses_advisory_open():
    wall = FakeClock()
    breaker = RateLimitBreaker(cooldown_seconds=600.0, clock=FakeClock())
    http = FakeHttp(
        status=200, payload=usage_payload(97.0, reset_at=wall.now + 10)
    )
    poller = UsagePoller(
        breaker,
        poll_seconds=30.0,
        token_provider=token,
        http_get=http,
        wall_clock=wall,
    )

    reading = await poller.poll_once()

    # High pressure BUT the window resets in 10s: skip the open entirely.
    assert reading is not None and reading.advised is False
    assert breaker.snapshot().state == "closed"
    assert breaker.before_acquire() is None
    assert breaker.advisory_opens == 0


def test_extract_rate_limit_window_full_snapshot():
    window = extract_rate_limit_window(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 42,
                    "limit_window_seconds": 18000,
                    "reset_at": 1_756_000_000,
                }
            }
        }
    )
    assert window == RateLimitWindow(
        used_percent=42.0,
        limit_window_seconds=18000.0,
        reset_at=1_756_000_000.0,
    )


def test_extract_rate_limit_window_optional_fields_are_defensive():
    window = extract_rate_limit_window(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 7.5,
                    "reset_at": "tomorrow",
                    "limit_window_seconds": True,
                }
            }
        }
    )
    assert window == RateLimitWindow(
        used_percent=7.5, reset_at=None, limit_window_seconds=None
    )


def test_extract_used_percent_delegates_to_window_extractor():
    payload = usage_payload(12.5)
    window = extract_rate_limit_window(payload)
    assert window is not None
    assert extract_used_percent(payload) == window.used_percent
    # Percent remains required: a window without it is skipped wholesale.
    assert extract_rate_limit_window({"rate_limit": {"primary_window": {}}}) is None


# ---------------------------------------------------------------------------
# USAGE-POLLER-WIRE: gateway lifespan start/stop
# ---------------------------------------------------------------------------


def _wire_app():
    """Minimal mock-backend gateway app whose lifespan we can drive directly."""
    from gpt.gateway.server import create_api_app

    return create_api_app(mock_backend=True)


async def test_gateway_lifespan_flag_off_creates_no_poller(monkeypatch):
    from gpt.gateway.server import _lifespan

    monkeypatch.delenv(POLL_SECONDS_ENV, raising=False)
    app = _wire_app()
    server = app.state.server

    async with _lifespan(app):
        # OFF: zero overhead — not even a dormant instance exists.
        assert server._usage_poller is None

    assert server._usage_poller is None  # teardown stays clean too


async def test_gateway_lifespan_flag_on_starts_and_stops_poller(monkeypatch):
    from gpt.gateway.server import _lifespan
    from gpt.transport.breaker import global_rate_limit_breaker

    monkeypatch.setenv(POLL_SECONDS_ENV, "30")
    app = _wire_app()
    server = app.state.server
    assert server._usage_poller is None  # nothing before startup

    seen: dict[str, Any] = {}
    async with _lifespan(app):
        poller = server._usage_poller
        assert isinstance(poller, UsagePoller)
        assert poller.enabled is True and poller.poll_seconds == 30.0
        # Advises exactly the global breaker singleton (one-way contract).
        assert poller.breaker is global_rate_limit_breaker()
        task = poller._task
        assert task is not None and not task.done()
        # Idempotent: a second start call never builds a second poller.
        server.start_usage_poller()
        assert server._usage_poller is poller
        seen["task"] = task

    assert server._usage_poller is None  # shutdown dropped the reference...
    assert seen["task"].cancelled() is True  # ...after cancelling the loop


async def test_gateway_lifespan_start_precedes_stop(monkeypatch):
    from gpt.gateway.server import _lifespan

    monkeypatch.setenv(POLL_SECONDS_ENV, "30")
    order: list[str] = []
    real_start, real_stop = UsagePoller.start, UsagePoller.stop

    def spy_start(self: UsagePoller):
        order.append("start")
        return real_start(self)

    async def spy_stop(self: UsagePoller):
        order.append("stop")
        await real_stop(self)

    monkeypatch.setattr(UsagePoller, "start", spy_start)
    monkeypatch.setattr(UsagePoller, "stop", spy_stop)

    async with _lifespan(_wire_app()):
        pass

    assert order == ["start", "stop"]


# ---------------------------------------------------------------------------
# POOL-POLLER-PERACCT wire-up: per-account pollers driven by the lifespan
# ---------------------------------------------------------------------------


async def test_gateway_lifespan_pool_scope_starts_per_account_pollers(monkeypatch):
    from gpt.gateway.server import _lifespan

    monkeypatch.setenv(POLL_SECONDS_ENV, "30")
    app = _wire_app()
    server = app.state.server
    # Mock backend never builds factories; setting the resolved map directly
    # reproduces the exact __init__ state under WEBGPT_BREAKER_SCOPE=auto.
    pool = {"beta": RateLimitBreaker(90.0), "alpha": RateLimitBreaker(90.0)}
    server.pool_rate_limit_breakers = dict(pool)
    assert server._account_usage_pollers == {}  # nothing before startup

    async with _lifespan(app):
        # N accounts -> N pollers; the global singleton stays untouched.
        assert server._usage_poller is None
        account_pollers = server._account_usage_pollers
        assert set(account_pollers) == {"alpha", "beta"}
        assert all(isinstance(p, UsagePoller) for p in account_pollers.values())
        # Each poller advises ONLY its own account's breaker.
        assert account_pollers["alpha"].breaker is pool["alpha"]
        assert account_pollers["beta"].breaker is pool["beta"]
        assert account_pollers["alpha"].enabled is True
        assert account_pollers["alpha"].poll_seconds == 30.0
        # Staggered starts in sorted-name order (30s / 2 accounts).
        assert account_pollers["alpha"].start_delay == 0.0
        assert account_pollers["beta"].start_delay == pytest.approx(15.0)
        # Shared pressure board wired alongside the pollers.
        assert isinstance(server.pool_pressure_board, PoolPressureBoard)
        tasks = {name: p._task for name, p in account_pollers.items()}
        assert all(t is not None and not t.done() for t in tasks.values())
        # Idempotent: re-starting never rebuilds or double-starts the pool.
        server.start_usage_poller()
        assert server._account_usage_pollers is account_pollers

    assert server._account_usage_pollers == {}  # shutdown dropped every ref...
    assert server.pool_pressure_board is None  # ...and the shared board too
    assert server._usage_poller is None
    assert all(task.cancelled() is True for task in tasks.values())


async def test_gateway_lifespan_subpool_scope_falls_back_to_singleton(monkeypatch):
    """Global scope (or <2 accounts) keeps the historical single poller."""
    from gpt.gateway.server import _lifespan
    from gpt.transport.breaker import global_rate_limit_breaker

    monkeypatch.setenv(POLL_SECONDS_ENV, "30")
    app = _wire_app()
    server = app.state.server
    server.pool_rate_limit_breakers = {"solo": RateLimitBreaker(90.0)}  # <2 acct

    async with _lifespan(app):
        assert server._usage_poller is not None
        assert server._usage_poller.breaker is global_rate_limit_breaker()
        assert server._account_usage_pollers == {}
        assert server.pool_pressure_board is None

    assert server._usage_poller is None  # singleton teardown unchanged


async def test_gateway_lifespan_flag_off_with_pool_creates_nothing(monkeypatch):
    from gpt.gateway.server import _lifespan

    monkeypatch.delenv(POLL_SECONDS_ENV, raising=False)  # default OFF
    app = _wire_app()
    server = app.state.server
    server.pool_rate_limit_breakers = {
        "alpha": RateLimitBreaker(90.0),
        "beta": RateLimitBreaker(90.0),
    }

    async with _lifespan(app):
        # OFF wins over everything: zero overhead, not even dormant instances.
        assert server._usage_poller is None
        assert server._account_usage_pollers == {}
        assert server.pool_pressure_board is None

    assert server._usage_poller is None
    assert server._account_usage_pollers == {}


# ---------------------------------------------------------------------------
# POOL-POLLER-PERACCT (row M): per-account pollers + pressure board
# ---------------------------------------------------------------------------


def _pool_breakers() -> dict[str, RateLimitBreaker]:
    return {
        "alpha": RateLimitBreaker(90.0, clock=FakeClock()),
        "beta": RateLimitBreaker(90.0, clock=FakeClock()),
    }


def _provider_factory():
    """Fake per-account bearer source: tok-<name>."""

    def factory(name: str):
        async def provider() -> str | None:
            return f"tok-{name}"

        return provider

    return factory


def test_pool_pressure_board_records_reports_and_snapshot():
    board = PoolPressureBoard()
    assert board.pressure("alpha") is None
    assert board.has_all(["alpha"]) is False

    board.record("alpha", 12.5)
    board.record("alpha", 30.0)  # latest wins
    board.record("beta", 99.0)

    assert board.pressure("alpha") == 30.0
    assert board.has_all(["alpha", "beta"]) is True
    assert board.snapshot() == {"alpha": 30.0, "beta": 99.0}


async def test_account_pollers_advise_only_their_own_breaker(monkeypatch):
    monkeypatch.setenv(POLL_SECONDS_ENV, "30")
    breakers = _pool_breakers()
    http = FakeHttp(status=200, payload=usage_payload(95))
    pollers, board = create_account_pollers(
        breakers,
        token_provider_factory=_provider_factory(),
        http_get=http,
    )

    assert set(pollers) == {"alpha", "beta"}
    assert pollers["alpha"].breaker is breakers["alpha"]
    assert pollers["alpha"].breaker is not breakers["beta"]

    # Poll ONLY alpha: exactly its own breaker opens, beta stays closed.
    reading = await pollers["alpha"].poll_once()
    assert reading is not None and reading.advised is True
    assert breakers["alpha"].snapshot().state == "open"
    assert breakers["beta"].snapshot().state == "closed"
    assert breakers["beta"].advisory_opens == 0
    headers = http.calls[0][1]
    assert headers["authorization"] == "Bearer tok-alpha"
    # The scrape reached the shared pressure board under alpha's name.
    assert board.pressure("alpha") == 95.0
    assert board.has_all(["alpha", "beta"]) is False


@pytest.mark.parametrize(
    "breakers_count, flag",
    [(0, "30"), (1, "30"), (2, "")],
)
async def test_create_account_pollers_noop_cases(breakers_count, flag, monkeypatch):
    if flag:
        monkeypatch.setenv(POLL_SECONDS_ENV, flag)
    else:
        monkeypatch.delenv(POLL_SECONDS_ENV, raising=False)  # default OFF
    candidates = {
        "alpha": RateLimitBreaker(90.0),
        "beta": RateLimitBreaker(90.0),
    }
    subset = dict(list(candidates.items())[:breakers_count])

    pollers, board = create_account_pollers(subset)

    assert pollers == {}
    assert board.snapshot() == {}


async def test_create_account_pollers_stagger_offsets_and_disable(monkeypatch):
    monkeypatch.setenv(POLL_SECONDS_ENV, "30")

    pollers, _board = create_account_pollers(_pool_breakers())
    assert pollers["alpha"].start_delay == 0.0  # sorted-name order
    assert pollers["beta"].start_delay == pytest.approx(15.0)

    flat, _board2 = create_account_pollers(_pool_breakers(), stagger=False)
    assert flat["alpha"].start_delay == 0.0
    assert flat["beta"].start_delay == 0.0


def test_account_auth_json_path_resolution(tmp_path, monkeypatch):
    # Default: ~/.config/webgpt/codex/<name>.auth.json (HOME retargeted).
    monkeypatch.setenv("HOME", str(tmp_path))
    assert account_auth_json_path("alpha") == (
        tmp_path / ".config" / "webgpt" / "codex" / "alpha.auth.json"
    )
    assert default_pool_auth_dir() == (
        tmp_path / ".config" / "webgpt" / "codex"
    )

    # Pool-wide directory override.
    custom_dir = tmp_path / "pools"
    assert account_auth_json_path(
        "alpha", environ={POOL_AUTH_DIR_ENV: str(custom_dir)}
    ) == (custom_dir / "alpha.auth.json")

    # Per-account env override wins over everything; non-alphanumerics fold.
    assert account_auth_json_path(
        "acct-1",
        environ={
            POOL_AUTH_DIR_ENV: str(custom_dir),
            ACCOUNT_AUTH_JSON_ENV_PREFIX + "ACCT_1": "/x/y.json",
        },
    ) == Path("/x/y.json")


async def test_missing_account_credential_idles_quietly_without_http(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(POLL_SECONDS_ENV, "30")
    monkeypatch.delenv("WEBGPT_CODEX_AUTH_JSON", raising=False)
    empty_dir = tmp_path / "no-bundles"
    monkeypatch.setenv(POOL_AUTH_DIR_ENV, str(empty_dir))

    breakers = _pool_breakers()
    http = FakeHttp(status=200, payload=usage_payload(95))
    pollers, board = create_account_pollers(breakers, http_get=http)

    # Real fs-backed provider: no auth.json -> silent idle, zero requests,
    # no mute latch (a credential appearing later just starts working).
    assert await pollers["alpha"].poll_once() is None
    assert http.calls == []
    assert board.snapshot() == {}
    assert pollers["alpha"].muted is False
    assert breakers["alpha"].advisory_opens == 0


async def test_reading_listener_exception_never_breaks_the_cycle():
    breaker = SpyBreaker()
    http = FakeHttp(status=200, payload=usage_payload(42))

    def exploding_listener(_reading):
        raise RuntimeError("listener bug")

    poller = UsagePoller(
        breaker,  # type: ignore[arg-type]
        poll_seconds=30.0,
        token_provider=token,
        http_get=http,
        reading_listener=exploding_listener,
    )

    reading = await poller.poll_once()

    assert reading is not None and reading.used_percent == 42.0
    assert poller.last_reading is reading


async def test_web_token_cache_provider_reads_fresh_token(tmp_path):
    cache = tmp_path / "webgpt-token-cache.json"
    cache.write_text(
        json.dumps(
            {
                "version": 1,
                "stored_at": 100.0,
                "access_token": "web-token",
                "cookies": {},
            }
        ),
        encoding="utf-8",
    )
    provider = make_web_token_cache_provider(
        tmp_path, max_age_seconds=30.0, wall_clock=lambda: 120.0
    )
    assert await provider() == "web-token"


async def test_web_token_cache_provider_rejects_stale_or_malformed(tmp_path):
    cache = tmp_path / "webgpt-token-cache.json"
    cache.write_text(
        json.dumps({"version": 1, "stored_at": 100.0, "access_token": "old"}),
        encoding="utf-8",
    )
    stale = make_web_token_cache_provider(
        tmp_path, max_age_seconds=30.0, wall_clock=lambda: 131.0
    )
    assert await stale() is None

    cache.write_text("not-json", encoding="utf-8")
    malformed = make_web_token_cache_provider(tmp_path, wall_clock=lambda: 100.0)
    assert await malformed() is None


async def test_usage_poller_can_use_web_token_cache_provider(tmp_path):
    (tmp_path / "webgpt-token-cache.json").write_text(
        json.dumps(
            {
                "version": 1,
                "stored_at": 100.0,
                "access_token": "web-token",
                "cookies": {},
            }
        ),
        encoding="utf-8",
    )
    provider = make_web_token_cache_provider(
        tmp_path, max_age_seconds=30.0, wall_clock=lambda: 110.0
    )
    http = FakeHttp(status=200, payload=usage_payload(12.0))
    poller = UsagePoller(
        SpyBreaker(),  # type: ignore[arg-type]
        poll_seconds=30.0,
        token_provider=provider,
        http_get=http,
    )
    reading = await poller.poll_once()
    assert reading is not None and reading.used_percent == 12.0
    assert http.calls[0][1]["authorization"] == "Bearer web-token"
