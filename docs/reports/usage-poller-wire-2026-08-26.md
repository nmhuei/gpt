# USAGE-POLLER-WIRE — 2026-08-26

Closes the last open item of row USAGE-INTROSPECTION ("Cần coordinator quyết" điểm 4): `UsagePoller` existed but had no start/stop point in the gateway lifecycle.

## Changes

`gpt/gateway/server.py` (4 separate blocks; no poller/breaker logic touched):

1. Import: `from gpt.transport.usage_poller import POLL_SECONDS_ENV, UsagePoller`.
2. `WebChatAPIServer.__init__`: `self._usage_poller: UsagePoller | None = None` next to health-loop attrs.
3. New methods after `_health_check_interval`:
   - `_usage_poll_seconds()`: parses `WEBGPT_USAGE_POLL_SECONDS`; unset/blank/garbled → 0.0 (OFF), matching the poller's own `_env_float` fallback.
   - `start_usage_poller()`: no-op when a poller exists or flag ≤ 0 (OFF path constructs nothing — zero overhead). When on: builds exactly one `UsagePoller(global_rate_limit_breaker())` and calls `.start()`; idempotent.
4. `close()`: before factory/session teardown, `await self._usage_poller.stop(); self._usage_poller = None` — safe when never started.
5. `_lifespan`: `server.start_usage_poller()` immediately after `start_account_health_loop()`.

Breaker choice: the global singleton (`global_rate_limit_breaker()`), per the poller's one-way `advise_pressure` contract; per-account breakers (`WEBGPT_BREAKER_SCOPE=auto`) intentionally untouched.

## Tests

`tests/test_usage_poller.py` (+3, driving the real `_lifespan` on a `mock_backend=True` app):

- `test_gateway_lifespan_flag_off_creates_no_poller` — OFF: no instance at startup or teardown.
- `test_gateway_lifespan_flag_on_starts_and_stops_poller` — ON: instance enabled, advises the global singleton, task running; second `start_usage_poller()` returns same instance; teardown drops reference and task is cancelled.
- `test_gateway_lifespan_start_precedes_stop` — spy order through real lifespan == `["start", "stop"]`.

## Verification

- `.venv/bin/python -m pytest tests/test_gateway_agent_loop.py tests/test_usage_poller.py tests/test_api_server.py -q` → **107 passed in 1.42s** (44 in tests/test_usage_poller.py).
- `py_compile` OK on both files. Note: `ruff`/`mypy` modules are not installed in this venv ("No module named ruff"), so those gates could not run.

Not done (per constraints): no commit, no gateway restart, no changes outside the two files.
