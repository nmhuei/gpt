"""POOL-PER-ACCT-BREAKER (row S) — per-account rate-limit breaker scope.

Covers:
- WEBGPT_BREAKER_SCOPE resolution: default/global keeps byte-for-byte old
  wiring; auto gives a >=2-account pool one RateLimitBreaker per account;
  a single account stays on the global singleton.
- Pool selection: open breakers are skipped; acquisition-phase
  BackendCoolingDown retries the next account (max N-1 retries); explicit
  pins are never rerouted; consumer-body failures never retry.
- Advisory header aggregation across per-account snapshots.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from gpt.state import AuthRequired, RateLimited
from gpt.transport.breaker import (
    CROSS_BRAKE_ENV,
    BackendCoolingDown,
    CrossBrake,
    RateLimitBreaker,
    cross_brake_from_env,
    global_rate_limit_breaker,
    parse_cross_brake_spec,
)
from gpt.transport.multi_account import MultiAccountWorkerFactory
from gpt.transport.usage_poller import PoolPressureBoard


class LeafFactory:
    """Browser-path-style leaf: acquisition consults its own breaker."""

    def __init__(self, name: str, breaker: RateLimitBreaker | None = None):
        self.name = name
        self.rate_limit_breaker = breaker
        self.acquire_calls = 0

    @asynccontextmanager
    async def lease(self):
        self.acquire_calls += 1
        if self.rate_limit_breaker is not None:
            # Mirrors ChatGPTWorkerFactory/hybrid acquire gating.
            self.rate_limit_breaker.before_acquire()
        yield SimpleNamespace(_marker=self.name)


def _pool(breakers: dict[str, RateLimitBreaker] | None = None, **kwargs):
    leaves = {
        "alpha": LeafFactory("alpha", None if breakers is None else breakers.get("alpha")),
        "beta": LeafFactory("beta", None if breakers is None else breakers.get("beta")),
    }
    factory = MultiAccountWorkerFactory(leaves, breakers=breakers, **kwargs)
    return factory, leaves


def _trip(breaker: RateLimitBreaker) -> None:
    assert breaker.enabled
    breaker.trip("test")


# ---------------------------------------------------------------------------
# Scope resolution + wiring
# ---------------------------------------------------------------------------


class _NoDefaultAccountStore:
    def list(self):
        return []

    def get_default(self):
        return None


def _pool_server(monkeypatch, *, transport: str = "browser"):
    from gpt.gateway.server import create_api_app

    monkeypatch.setattr("gpt.gateway.server.AccountStore", _NoDefaultAccountStore)
    app = create_api_app(
        headless=True,
        transport=transport,
        account_profiles={"alpha": "/tmp/wg-alpha", "beta": "/tmp/wg-beta"},
    )
    return app.state.server


async def test_auto_scope_two_accounts_wire_independent_breakers(monkeypatch):
    monkeypatch.delenv("WEBGPT_BREAKER_SCOPE", raising=False)
    monkeypatch.setenv("WEBGPT_BREAKER_SCOPE", "auto")

    server = _pool_server(monkeypatch)

    pool_breakers = server.pool_rate_limit_breakers
    assert set(pool_breakers) == {"alpha", "beta"}
    assert pool_breakers["alpha"] is not pool_breakers["beta"]
    # Never the process-wide singleton.
    for breaker in pool_breakers.values():
        assert breaker is not global_rate_limit_breaker()

    factory = server._worker_factory
    assert isinstance(factory, MultiAccountWorkerFactory)
    assert factory.breakers == pool_breakers
    # Each leaf factory owns exactly its account's instance.
    for name, leaf in factory.factories.items():
        assert leaf.rate_limit_breaker is pool_breakers[name]


async def test_default_scope_keeps_global_singleton_wiring(monkeypatch):
    monkeypatch.delenv("WEBGPT_BREAKER_SCOPE", raising=False)
    server = _pool_server(monkeypatch)

    assert server.pool_rate_limit_breakers == {}
    factory = server._worker_factory
    assert isinstance(factory, MultiAccountWorkerFactory)
    assert factory.breakers == {}
    # browser-path leaves still fall back to the shared singleton internally.
    for leaf in factory.factories.values():
        assert leaf.rate_limit_breaker is global_rate_limit_breaker()


async def test_global_scope_explicit_keeps_singleton_wiring(monkeypatch):
    monkeypatch.setenv("WEBGPT_BREAKER_SCOPE", "global")
    server = _pool_server(monkeypatch)

    assert server.pool_rate_limit_breakers == {}
    for leaf in server._worker_factory.factories.values():
        assert leaf.rate_limit_breaker is global_rate_limit_breaker()


async def test_auto_scope_single_account_stays_global(monkeypatch):
    from gpt.gateway.server import create_api_app

    monkeypatch.setattr("gpt.gateway.server.AccountStore", _NoDefaultAccountStore)
    monkeypatch.setenv("WEBGPT_BREAKER_SCOPE", "auto")
    app = create_api_app(
        headless=True, account_profiles={"solo": "/tmp/wg-solo"}
    )
    server = app.state.server

    assert server.pool_rate_limit_breakers == {}
    for leaf in server._worker_factory.factories.values():
        assert leaf.rate_limit_breaker is global_rate_limit_breaker()


async def test_invalid_scope_falls_back_to_global(monkeypatch):
    monkeypatch.setenv("WEBGPT_BREAKER_SCOPE", "per_thing")
    server = _pool_server(monkeypatch)
    assert server.pool_rate_limit_breakers == {}


async def test_auto_scope_hybrid_leaves_receive_per_account_breaker(monkeypatch):
    monkeypatch.setenv("WEBGPT_BREAKER_SCOPE", "auto")
    server = _pool_server(monkeypatch, transport="hybrid")

    pool_breakers = server.pool_rate_limit_breakers
    assert set(pool_breakers) == {"alpha", "beta"}
    for name, leaf in server._worker_factory.factories.items():
        assert leaf.rate_limit_breaker is pool_breakers[name]


# ---------------------------------------------------------------------------
# Pool selection + retry semantics
# ---------------------------------------------------------------------------


async def test_trip_on_one_account_does_not_block_the_pool():
    breakers = {
        "alpha": RateLimitBreaker(90.0),
        "beta": RateLimitBreaker(90.0),
    }
    factory, leaves = _pool(breakers)
    _trip(breakers["alpha"])

    # Selection skips the open alpha outright and leases healthy beta.
    async with factory.lease() as session:
        assert session._webgpt_account_name == "beta"
    assert leaves["alpha"].acquire_calls == 0
    assert leaves["beta"].acquire_calls == 1


async def test_selection_skips_open_account():
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    factory, _leaves = _pool(breakers)
    _trip(breakers["alpha"])
    assert await factory._pick_name(None) == "beta"


async def test_open_sticky_default_rotates_to_sibling():
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    factory, _leaves = _pool(breakers, default_name="alpha")
    _trip(breakers["alpha"])
    assert await factory._pick_name(None) == "beta"


async def test_half_open_account_stays_selectable_for_probe():
    now = {"t": 1000.0}
    breaker = RateLimitBreaker(90.0, clock=lambda: now["t"])
    factory, _leaves = _pool({"alpha": breaker, "beta": RateLimitBreaker(90.0)})
    breaker.trip("test")
    now["t"] += 91.0  # window elapsed -> half-open probe allowed
    assert await factory._pick_name(None) == "alpha"


async def test_all_accounts_open_raises_cooling():
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    factory, leaves = _pool(breakers)
    _trip(breakers["alpha"])
    _trip(breakers["beta"])

    # Nothing selectable: fail fast WITHOUT burning a leaf acquisition.
    with pytest.raises(BackendCoolingDown):
        async with factory.lease():
            pass
    assert leaves["alpha"].acquire_calls == 0
    assert leaves["beta"].acquire_calls == 0


async def test_explicit_pin_is_never_rerouted_when_cooling():
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    factory, leaves = _pool(breakers)
    _trip(breakers["alpha"])

    with pytest.raises(BackendCoolingDown):
        async with factory.lease("alpha"):
            pass
    assert leaves["beta"].acquire_calls == 0


async def test_consumer_body_failure_never_retries_next_account():
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    factory, leaves = _pool(breakers)

    marker = BackendCoolingDown("mid-turn failure")
    with pytest.raises(BackendCoolingDown) as excinfo:
        async with factory.lease():
            raise marker
    assert excinfo.value is marker
    # alpha was leased once; beta untouched.
    assert leaves["alpha"].acquire_calls == 1
    assert leaves["beta"].acquire_calls == 0


async def test_no_breakers_keeps_single_shot_legacy_path():
    """Regression: without per-account breakers there is NO retry-next."""

    class AlwaysCoolingLeaf(LeafFactory):
        @asynccontextmanager
        async def lease(self):
            self.acquire_calls += 1
            raise BackendCoolingDown("global breaker open")
            yield  # pragma: no cover

    leaves = {"alpha": AlwaysCoolingLeaf("alpha"), "beta": LeafFactory("beta")}
    factory = MultiAccountWorkerFactory(leaves)  # no breakers wired

    with pytest.raises(BackendCoolingDown):
        async with factory.lease():
            pass
    # Exactly one attempt: legacy behaviour, no cross-account failover.
    assert leaves["alpha"].acquire_calls == 1
    assert leaves["beta"].acquire_calls == 0


async def test_retry_gives_up_after_n_minus_one_retries():
    class AlwaysCoolingLeaf(LeafFactory):
        @asynccontextmanager
        async def lease(self):
            self.acquire_calls += 1
            raise BackendCoolingDown(f"{self.name} cooling")
            yield  # pragma: no cover

    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    leaves = {
        "alpha": AlwaysCoolingLeaf("alpha", breakers["alpha"]),
        "beta": AlwaysCoolingLeaf("beta", breakers["beta"]),
    }
    factory = MultiAccountWorkerFactory(leaves, breakers=breakers)

    with pytest.raises(BackendCoolingDown) as excinfo:
        async with factory.lease():
            pass
    assert str(excinfo.value) == "beta cooling"  # last attempt surfaces
    assert leaves["alpha"].acquire_calls == 1
    assert leaves["beta"].acquire_calls == 1


# ---------------------------------------------------------------------------
# Hybrid gate (explicit breaker only)
# ---------------------------------------------------------------------------


class _FakePage:
    async def goto(self, *_args, **_kwargs) -> None:
        return None


class _FakeBrowserManager:
    profile_dir: str | None = None

    async def start(self) -> None:
        return None

    async def new_page(self) -> _FakePage:
        return _FakePage()

    async def stop(self) -> None:
        return None


def _hybrid_factory(breaker: RateLimitBreaker | None):
    from gpt.transport.hybrid import HybridWorkerFactory

    return HybridWorkerFactory(
        _FakeBrowserManager(),  # type: ignore[arg-type]
        max_workers=1,
        warm_workers=1,
        queue_timeout=5.0,
        rate_limit_breaker=breaker,
    )


def _fake_session():
    async def _close() -> None:
        return None

    return SimpleNamespace(session_id="s1", state="ready", close=_close)


async def test_hybrid_gate_blocks_while_breaker_open():
    now = {"t": 1000.0}
    breaker = RateLimitBreaker(90.0, clock=lambda: now["t"])
    factory = _hybrid_factory(breaker)
    factory._started = True
    factory._idle.append(_fake_session())
    breaker.trip("test")

    with pytest.raises(BackendCoolingDown):
        await factory.acquire()
    # The worker was NOT consumed by the failed acquisition.
    assert len(factory._idle) == 1


async def test_hybrid_gate_passes_probe_after_window_and_closes():
    now = {"t": 1000.0}
    breaker = RateLimitBreaker(90.0, clock=lambda: now["t"])
    factory = _hybrid_factory(breaker)
    factory._started = True
    factory._idle.append(_fake_session())
    breaker.trip("test")
    now["t"] += 91.0

    session_id, _session = await factory.acquire()
    assert session_id == "s1"
    # Successful handout counts as the closing half-open probe.
    assert breaker.snapshot().state == "closed"


async def test_hybrid_without_breaker_stays_ungated():
    factory = _hybrid_factory(None)
    assert factory.rate_limit_breaker is None
    factory._started = True
    factory._idle.append(_fake_session())

    session_id, _session = await factory.acquire()
    assert session_id == "s1"


# ---------------------------------------------------------------------------
# Advisory header aggregation
# ---------------------------------------------------------------------------


def _headers(**kwargs):
    from gpt.gateway.server import _advisory_ratelimit_headers

    return _advisory_ratelimit_headers(**kwargs)


def test_headers_all_closed_advertise_full_budget():
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    headers = _headers(breakers=breakers)
    assert headers["anthropic-ratelimit-requests-limit"] == "100"
    assert headers["anthropic-ratelimit-requests-remaining"] == "100"
    assert headers["anthropic-ratelimit-requests-reset"] == "0s"


def test_headers_partial_open_keep_closed_account_budget():
    now = {"t": 1000.0}
    open_b = RateLimitBreaker(90.0, clock=lambda: now["t"])
    open_b.trip("test")
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": open_b}
    headers = _headers(breakers=breakers)
    assert headers["anthropic-ratelimit-requests-remaining"] == "100"
    assert headers["anthropic-ratelimit-requests-reset"] == "0s"


def test_headers_all_open_exhaust_budget_with_longest_reset():
    now = {"t": 1000.0}
    short = RateLimitBreaker(90.0, clock=lambda: now["t"])
    long_b = RateLimitBreaker(600.0, clock=lambda: now["t"])
    short.trip("test")
    long_b.trip("test")
    headers = _headers(breakers={"alpha": short, "beta": long_b})
    assert headers["anthropic-ratelimit-requests-remaining"] == "0"
    reset = int(headers["anthropic-ratelimit-requests-reset"].removesuffix("s"))
    assert reset == 600  # ceil of the longest open window


def test_headers_without_pool_breakers_use_global_snapshot():
    headers = _headers()
    assert headers["anthropic-ratelimit-requests-limit"] == "100"
    assert headers["anthropic-ratelimit-requests-remaining"] in {"0", "100"}
    assert headers["anthropic-ratelimit-requests-reset"].endswith("s")


# ---------------------------------------------------------------------------
# POOL-POLLER-PERACCT: least-pressure selection (opt-in tie-break)
# ---------------------------------------------------------------------------


def _board(values: dict[str, float]) -> PoolPressureBoard:
    board = PoolPressureBoard()
    for name, percent in values.items():
        board.record(name, percent)
    return board


async def test_default_selection_ignores_pressure_board(monkeypatch):
    monkeypatch.delenv("WEBGPT_POOL_SELECTION", raising=False)
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    factory, _leaves = _pool(
        breakers, pressures=_board({"alpha": 1.0, "beta": 99.0})
    )

    assert factory.selection_mode == "round-robin"
    picks = [await factory._pick_name(None) for _ in range(4)]
    assert picks == ["alpha", "beta", "alpha", "beta"]  # byte-identical rotation


async def test_least_pressure_picks_lowest_reading(monkeypatch):
    monkeypatch.setenv("WEBGPT_POOL_SELECTION", "least-pressure")
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    factory, _leaves = _pool(
        breakers, pressures=_board({"alpha": 82.0, "beta": 41.0})
    )

    assert await factory._pick_name(None) == "beta"
    assert await factory._pick_name(None) == "beta"  # stays lowest until data moves


async def test_least_pressure_ties_still_rotate(monkeypatch):
    monkeypatch.setenv("WEBGPT_POOL_SELECTION", "least-pressure")
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    factory, _leaves = _pool(breakers, pressures=_board({"alpha": 50.0, "beta": 50.0}))

    picks = [await factory._pick_name(None) for _ in range(4)]
    assert picks == ["alpha", "beta", "alpha", "beta"]


async def test_least_pressure_partial_data_falls_back_to_rotation(monkeypatch):
    """Only some accounts have readings: ranking is skipped entirely."""
    monkeypatch.setenv("WEBGPT_POOL_SELECTION", "least-pressure")
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    factory, _leaves = _pool(breakers, pressures=_board({"alpha": 10.0}))

    picks = [await factory._pick_name(None) for _ in range(4)]
    assert picks == ["alpha", "beta", "alpha", "beta"]


async def test_least_pressure_runs_after_breaker_filter(monkeypatch):
    """Open breakers are excluded first; ranking only reorders survivors."""
    monkeypatch.setenv("WEBGPT_POOL_SELECTION", "least-pressure")
    now = {"t": 1000.0}
    open_alpha = RateLimitBreaker(90.0, clock=lambda: now["t"])
    open_alpha.trip("test")
    breakers = {"alpha": open_alpha, "beta": RateLimitBreaker(90.0, clock=lambda: now["t"])}
    factory, leaves = _pool(
        breakers, pressures=_board({"alpha": 0.0, "beta": 97.0})
    )

    async with factory.lease() as session:
        assert session._webgpt_account_name == "beta"
    assert leaves["alpha"].acquire_calls == 0


async def test_explicit_selection_kwarg_overrides_env(monkeypatch):
    monkeypatch.setenv("WEBGPT_POOL_SELECTION", "round-robin")
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    factory, _leaves = _pool(
        breakers,
        pressures=_board({"alpha": 1.0, "beta": 99.0}),
        selection="least-pressure",
    )

    assert factory.selection_mode == "least-pressure"
    # Explicit kwarg beats the env: alpha ranks first at 1% used.
    assert await factory._pick_name(None) == "alpha"


def test_factory_pressure_api_none_safe_without_board_or_readings():
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}
    bare, _leaves = _pool(breakers)
    assert bare.pressure("alpha") is None

    wired, _leaves2 = _pool(breakers, pressures=_board({"alpha": 41.0}))
    assert wired.pressure("alpha") == pytest.approx(41.0)
    assert wired.pressure("beta") is None  # unknown stays unknown


# ---------------------------------------------------------------------------
# POOL-POLLER-PERACCT: cross-account brake (opt-in)
# ---------------------------------------------------------------------------


def _cb_clock(start: float = 1000.0):
    state = {"t": start}
    return state, (lambda: state["t"])


def _three_breakers(state):
    return {
        name: RateLimitBreaker(90.0, clock=lambda: state["t"])
        for name in ("alpha", "beta", "gamma")
    }


def test_cross_brake_fires_at_k_distinct_within_window():
    state, clock = _cb_clock()
    breakers = _three_breakers(state)
    cb = CrossBrake(breakers, threshold=2, window_seconds=600.0, clock=clock)

    assert cb.record("alpha") is False
    assert all(b.snapshot().state == "closed" for b in breakers.values())

    assert cb.record("beta") is True  # K=2 distinct reached
    assert all(b.snapshot().state == "open" for b in breakers.values())
    assert breakers["gamma"].advisory_opens == 1  # advised via advise_pressure
    assert cb.fires == 1
    assert cb.distinct_tripped() == ("alpha", "beta")

    # Window slides past every hit: the counter resets.
    state["t"] += 601.0
    assert cb.distinct_tripped() == ()
    assert cb.record("gamma") is False  # needs K fresh distinct hits again


def test_cross_brake_repeated_hits_do_not_stack_windows():
    state, clock = _cb_clock()
    breakers = _three_breakers(state)
    cb = CrossBrake(breakers, threshold=2, window_seconds=600.0, clock=clock)
    cb.record("alpha")
    assert cb.record("beta") is True
    first_expiry = breakers["gamma"].snapshot().remaining_seconds

    # A third distinct hit inside the same window advises again, but every
    # breaker window is already open so advise_pressure no-ops (dedup).
    assert cb.record("gamma") is False
    assert cb.fires == 1
    assert breakers["gamma"].snapshot().remaining_seconds == pytest.approx(first_expiry)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", None),
        ("0", None),
        ("off", None),
        ("1", (2, 600.0)),
        ("true", (2, 600.0)),
        ("3", (3, 600.0)),
        ("2@300", (2, 300.0)),
        ("x", None),
        ("2@bad", None),
        ("0@300", None),
    ],
)
def test_parse_cross_brake_spec_table(raw, expected):
    assert parse_cross_brake_spec(raw) == expected


def test_cross_brake_from_env_gates(monkeypatch):
    breakers = {"alpha": RateLimitBreaker(90.0), "beta": RateLimitBreaker(90.0)}

    monkeypatch.delenv(CROSS_BRAKE_ENV, raising=False)  # default OFF
    assert cross_brake_from_env(breakers) is None

    monkeypatch.setenv(CROSS_BRAKE_ENV, "garbage!!")
    assert cross_brake_from_env(breakers) is None

    monkeypatch.setenv(CROSS_BRAKE_ENV, "1")
    cb = cross_brake_from_env(breakers)
    assert cb is not None
    assert cb.threshold == 2 and cb.window_seconds == 600.0

    assert cross_brake_from_env({}) is None  # nothing to brake


class _FailingLeaf:
    """Leaf whose acquisition raises the configured exception."""

    def __init__(self, name: str, exc: BaseException):
        self.name = name
        self.exc = exc
        self.acquire_calls = 0

    @asynccontextmanager
    async def lease(self):
        self.acquire_calls += 1
        raise self.exc
        yield  # pragma: no cover


async def _run_one_lease(factory):
    try:
        async with factory.lease():
            pass
    except (RateLimited, AuthRequired, BackendCoolingDown) as exc:
        return exc
    return None  # pragma: no cover


async def test_factory_real_rate_limited_feeds_cross_brake():
    state, clock = _cb_clock()
    breakers = {
        "alpha": RateLimitBreaker(90.0, clock=lambda: state["t"]),
        "beta": RateLimitBreaker(90.0, clock=lambda: state["t"]),
    }
    cb = CrossBrake(breakers, threshold=2, window_seconds=600.0, clock=clock)
    leaves = {
        "alpha": _FailingLeaf("alpha", RateLimited("429 alpha")),
        "beta": _FailingLeaf("beta", RateLimited("429 beta")),
    }
    factory = MultiAccountWorkerFactory(leaves, breakers=breakers, cross_brake=cb)

    first = await _run_one_lease(factory)
    assert isinstance(first, RateLimited)
    assert cb.distinct_tripped() == ("alpha",)
    assert all(b.snapshot().state == "closed" for b in breakers.values())

    second = await _run_one_lease(factory)
    assert isinstance(second, RateLimited)
    # K=2 real rate limits inside the window: the whole pool brakes together.
    assert all(b.snapshot().state == "open" for b in breakers.values())
    assert cb.fires == 1

    # Both breakers now open: selection finds nothing and fails fast without
    # burning another leaf acquisition.
    third = await _run_one_lease(factory)
    assert isinstance(third, BackendCoolingDown)
    assert leaves["alpha"].acquire_calls == 1
    assert leaves["beta"].acquire_calls == 1


async def test_factory_auth_failures_never_feed_cross_brake():
    state, clock = _cb_clock()
    names = ("alpha", "beta", "gamma")
    breakers = {name: RateLimitBreaker(90.0, clock=lambda: state["t"]) for name in names}
    cb = CrossBrake(breakers, threshold=3, window_seconds=600.0, clock=clock)
    leaves = {
        "alpha": _FailingLeaf("alpha", RateLimited("429")),
        "beta": _FailingLeaf("beta", RateLimited("429")),
        "gamma": _FailingLeaf("gamma", AuthRequired("auth trouble")),
    }
    factory = MultiAccountWorkerFactory(leaves, breakers=breakers, cross_brake=cb)

    for _ in names:
        await _run_one_lease(factory)

    # Two real rate limits + one auth failure < K=3: auth never counts.
    assert cb.distinct_tripped() == ("alpha", "beta")
    assert all(b.snapshot().state == "closed" for b in breakers.values())
