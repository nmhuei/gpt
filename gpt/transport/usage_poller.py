"""USAGE-INTROSPECTION (ROADMAP row M): proactive codex quota polling.

The global rate-limit breaker (``gpt.transport.breaker``) is REACTIVE: it only
opens a cooldown after a real 429 already burned quota and forced a failed
turn.  Codex exposes its remaining agentic-quota headroom via a usage endpoint
whose payload carries ``rate_limit.primary_window`` with ``used_percent`` /
``reset_at``.  This module polls that endpoint periodically and ADVISES the
existing breaker through :meth:`RateLimitBreaker.advise_pressure` so the
multi-account pool gets a short protective cooldown BEFORE the backend starts
rejecting.

Coupling is strictly ONE-WAY: the poller never touches breaker internals —
it can only call ``advise_pressure(used_percent)``; whether that opens a
window, at which threshold, and for how long is entirely breaker policy.

Dormancy contract (default OFF):

- ``WEBGPT_USAGE_POLL_SECONDS`` unset or <= 0  →  :meth:`UsagePoller.start`
  is a no-op, no task exists, nothing reads the network or the credential.
- The codex OAuth bearer comes from :mod:`gpt.transport.codex_auth`
  (lazy-imported here so that module stays untouched and dormant while
  ``WEBGPT_CODEX_AUTH_JSON`` is unset).  No usable credential → the poller
  idles silently.
- HTTP 401/403 mutes the poller for the rest of the process lifetime: every
  later cycle becomes an immediate quiet no-op so an unauthorized credential
  cannot spam logs or requests.
- Any other non-200 status or transport error simply skips that cycle.

Parsing is defensive: a missing/mis-shaped ``rate_limit.primary_window``
(or non-numeric ``used_percent``) skips the cycle without advising.  The
poller deliberately forwards EVERY valid reading to the breaker regardless of
level — thresholding is breaker policy, not poller policy.

RESET-AWARE-COOLDOWN (ROADMAP row S): the window's absolute ``reset_at``
unix timestamp used to be ignored; it is now parsed alongside
``used_percent`` and forwarded to the breaker as ``seconds_until_reset``
(wall-clock distance, None when absent/garbled) so advisory cooldowns can be
capped just past the natural reset — or skipped entirely when the window is
about to reset anyway (breaker policy, see ``gpt.transport.breaker``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gpt.transport.breaker import RateLimitBreaker

POLL_SECONDS_ENV = "WEBGPT_USAGE_POLL_SECONDS"
USAGE_URL_ENV = "WEBGPT_USAGE_URL"

DEFAULT_POLL_SECONDS = 0.0  # OFF unless explicitly enabled.
DEFAULT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# POOL-POLLER-PERACCT (row M): per-account credential resolution. Each pool
# account's codex OAuth bundle lives at ``<pool-dir>/<name>.auth.json`` (default
# ``~/.config/webgpt/codex``), overridable per account via
# ``WEBGPT_CODEX_AUTH_JSON_<NAME>`` or wholesale via ``WEBGPT_POOL_AUTH_DIR``.
POOL_AUTH_DIR_ENV = "WEBGPT_POOL_AUTH_DIR"
ACCOUNT_AUTH_JSON_ENV_PREFIX = "WEBGPT_CODEX_AUTH_JSON_"

# Statuses meaning "this credential will not be accepted": mute quietly.
_AUTH_MUTE_STATUSES = frozenset({401, 403})
_REQUEST_TIMEOUT_SECONDS = 15.0
_WEB_TOKEN_CACHE_FILENAME = "webgpt-token-cache.json"
_WEB_TOKEN_CACHE_VERSION = 1
_WEB_TOKEN_CACHE_MAX_AGE_SECONDS = 1800.0

try:  # Same guarded optional-dependency style as codex_auth.py.
    from curl_cffi.requests import Session as _CurlSyncSession
except ImportError:  # pragma: no cover - optional dependency
    _CurlSyncSession = None  # type: ignore[assignment,misc]

# Injectable blocking GET: (url, headers) -> (status, parsed-json-or-None).
# Injected by tests so nothing here ever touches the network.
HttpGetFn = Callable[[str, dict[str, str]], tuple[int, Any]]

# Injectable async bearer source: () -> token-or-None.  Defaults to the lazy
# codex_auth path below; tests inject fakes.
TokenProvider = Callable[[], Awaitable[str | None]]


def default_http_get(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    """Default blocking usage GET via curl_cffi (runs in a worker thread)."""
    if _CurlSyncSession is None:
        raise RuntimeError("curl_cffi is not installed; cannot poll codex usage.")
    with _CurlSyncSession() as session:
        response = session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    return int(response.status_code), payload


@dataclass(frozen=True)
class RateLimitWindow:
    """Defensively parsed ``primary_window`` snapshot (canonical codex shape).

    ``used_percent`` is required numeric and clamped to [0, 100]; ``reset_at``
    (absolute unix seconds) and ``limit_window_seconds`` are optional — a
    payload that garbles or omits them still yields a usable window so the
    percent-only advice path keeps working.
    """

    used_percent: float
    reset_at: float | None = None
    limit_window_seconds: float | None = None


def _optional_number(raw: Any) -> float | None:
    """Numeric-or-None for optional window fields (bools excluded)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def extract_rate_limit_window(payload: Any) -> RateLimitWindow | None:
    """Pull ``rate_limit.primary_window`` defensively as a full snapshot.

    Same skip rules as :func:`extract_used_percent` (non-object root, missing
    windows, boolean/str/None percent values all return None); valid numbers
    are clamped into [0, 100] so hostile payloads stay within sane bounds.
    """
    if not isinstance(payload, dict):
        return None
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    primary = rate_limit.get("primary_window")
    if not isinstance(primary, dict):
        return None
    value = primary.get("used_percent")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return RateLimitWindow(
        used_percent=max(0.0, min(100.0, float(value))),
        reset_at=_optional_number(primary.get("reset_at")),
        limit_window_seconds=_optional_number(primary.get("limit_window_seconds")),
    )


def extract_used_percent(payload: Any) -> float | None:
    """Pull ``rate_limit.primary_window.used_percent`` defensively.

    Returns None (skip this cycle) for anything unexpected: non-object root,
    missing windows, boolean/str/None percent values.  Valid numbers are
    clamped into [0, 100] so a hostile/garbled payload can never push the
    breaker policy outside sane bounds.
    """
    window = extract_rate_limit_window(payload)
    return None if window is None else window.used_percent


@dataclass(frozen=True)
class UsageReading:
    """One successful scrape forwarded to the breaker."""

    used_percent: float
    advised: bool  # True when the breaker opened a window because of it.
    # RESET-AWARE-COOLDOWN: wall-clock seconds until the window's reset_at,
    # when the payload carried one (None otherwise).
    seconds_until_reset: float | None = None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def make_web_token_cache_provider(
    profile_dir: Path | str,
    *,
    max_age_seconds: float = _WEB_TOKEN_CACHE_MAX_AGE_SECONDS,
    wall_clock: Callable[[], float] = time.time,
) -> TokenProvider:
    """Read-only bearer provider for TokenManager's per-profile disk cache.

    The provider never opens a browser and never logs the token. Missing,
    stale, corrupt, or malformed caches simply return ``None`` so the poller
    idles until a normal hybrid turn refreshes the profile cache.
    """
    path = Path(profile_dir) / _WEB_TOKEN_CACHE_FILENAME

    async def provider() -> str | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("version") != _WEB_TOKEN_CACHE_VERSION:
            return None
        stored_at = raw.get("stored_at")
        if isinstance(stored_at, bool) or not isinstance(stored_at, (int, float)):
            return None
        age = wall_clock() - float(stored_at)
        if age < 0 or age >= max_age_seconds:
            return None
        token = raw.get("access_token")
        return token if isinstance(token, str) and token else None

    return provider


class UsagePoller:
    """Periodic codex usage scraper feeding one-way advice to the breaker.

    Constructing the poller does nothing; :meth:`start` spawns the loop only
    when ``poll_seconds > 0`` (env default 0 = permanently dormant).  All I/O
    is injectable (``token_provider``, ``http_get``) so tests are fake-HTTP.
    """

    def __init__(
        self,
        breaker: RateLimitBreaker,
        *,
        poll_seconds: float | None = None,
        url: str | None = None,
        token_provider: TokenProvider | None = None,
        http_get: HttpGetFn | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        start_delay: float = 0.0,
        reading_listener: Callable[[UsageReading], None] | None = None,
    ) -> None:
        self.breaker = breaker
        if poll_seconds is None:
            poll_seconds = _env_float(POLL_SECONDS_ENV, DEFAULT_POLL_SECONDS)
        self.poll_seconds = float(poll_seconds)
        configured_url = url if url is not None else os.environ.get(USAGE_URL_ENV, "")
        self.url = configured_url.strip() or DEFAULT_USAGE_URL
        self.enabled = self.poll_seconds > 0
        # POOL-POLLER-PERACCT: extra one-shot delay before the FIRST cycle so
        # N pool pollers can be staggered (offset poll_seconds/N each) instead
        # of bursting the usage endpoint in lockstep. Default 0 keeps the
        # single-poller behaviour byte-identical.
        self.start_delay = max(0.0, float(start_delay))
        self._reading_listener = reading_listener
        self._token_provider: TokenProvider = (
            token_provider if token_provider is not None else self._codex_token_provider
        )
        self._http_get: HttpGetFn = http_get or default_http_get
        self._clock = clock
        # Wall clock for absolute ``reset_at`` timestamps (injectable so the
        # reset-aware advice math is testable without real time).
        self._wall_clock = wall_clock
        # Latched after a 401/403: silent idle for the rest of the process.
        self._muted = False
        self._last_reading: UsageReading | None = None
        # Lazily built CodexAuthManager (kept untyped to avoid importing the
        # codex_auth module at construction time — dormancy contract).
        self._codex_manager: Any = None
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def muted(self) -> bool:
        """True once an auth-rejected response silenced further polling."""
        return self._muted

    @property
    def last_reading(self) -> UsageReading | None:
        """Most recent successful scrape, or None before the first one."""
        return self._last_reading

    def start(self) -> asyncio.Task[None] | None:
        """Spawn the polling loop; no-op (returns None) while disabled."""
        if not self.enabled:
            return None
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.get_running_loop().create_task(self._run())
        return self._task

    async def stop(self) -> None:
        """Cancel the loop and await its exit; safe when never started."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        """Sleep-first loop: one interval of gateway boot traffic stays clean.

        Polling errors are swallowed per-cycle by design — a flaky usage
        endpoint must never take the gateway down; the next tick retries.
        """
        if self.start_delay > 0.0:
            await asyncio.sleep(self.start_delay)
        while True:
            await asyncio.sleep(self.poll_seconds)
            with contextlib.suppress(Exception):
                await self.poll_once()

    # -- one poll cycle --------------------------------------------------------

    async def poll_once(self) -> UsageReading | None:
        """Run a single scrape+advise cycle; returns None when skipped.

        Skip paths (all silent): disabled-by-auth credential, transport error,
        401/403 (also latches :attr:`muted`), other non-200 statuses, and any
        payload without a parseable ``used_percent``.
        """
        if self._muted:
            return None
        token = await self._resolve_token()
        if not token:
            return None
        try:
            status, payload = await asyncio.to_thread(
                self._http_get,
                self.url,
                {"authorization": f"Bearer {token}", "accept": "application/json"},
            )
        except Exception:  # transport/DNS/TLS — skip quietly, retry next tick
            return None
        if status in _AUTH_MUTE_STATUSES:
            self._muted = True
            return None
        if status != 200:
            return None
        window = extract_rate_limit_window(payload)
        if window is None:
            return None
        seconds_until_reset = (
            None if window.reset_at is None else window.reset_at - self._wall_clock()
        )
        advised = self.breaker.advise_pressure(
            window.used_percent, seconds_until_reset=seconds_until_reset
        )
        reading = UsageReading(
            used_percent=window.used_percent,
            advised=advised,
            seconds_until_reset=seconds_until_reset,
        )
        self._last_reading = reading
        # POOL-POLLER-PERACCT: publish to the shared pressure board (pool
        # selection reads this). A listener bug must never break polling.
        if self._reading_listener is not None:
            with contextlib.suppress(Exception):
                self._reading_listener(reading)
        return reading

    async def _resolve_token(self) -> str | None:
        """Bearer from the injected provider, swallowing provider errors."""
        try:
            return await self._token_provider()
        except Exception:  # refresh/transient auth trouble — idle this cycle
            return None

    async def _codex_token_provider(self) -> str | None:
        """Lazy-imported codex OAuth bearer; None whenever unavailable.

        The import happens here (not at module load) so
        :mod:`gpt.transport.codex_auth` stays completely untouched/dormant
        until this poller actually runs with the feature enabled.  A manager
        constructed with ``WEBGPT_CODEX_AUTH_JSON`` unset reports
        ``enabled=False`` without touching disk or network.
        """
        from gpt.transport.codex_auth import CodexAuthError, CodexAuthManager

        if self._codex_manager is None:
            self._codex_manager = CodexAuthManager()
        manager = self._codex_manager
        if not manager.enabled:
            return None
        try:
            return await manager.get_access_token()
        except CodexAuthError:
            return None

    # -- observability ---------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Point-in-time introspection for stats endpoints / debugging."""
        return {
            "enabled": self.enabled,
            "poll_seconds": self.poll_seconds,
            "muted": self._muted,
            "last_used_percent": (
                None if self._last_reading is None else self._last_reading.used_percent
            ),
            "last_advised": (
                None if self._last_reading is None else self._last_reading.advised
            ),
            "last_seconds_until_reset": (
                None
                if self._last_reading is None
                else self._last_reading.seconds_until_reset
            ),
        }


# ---------------------------------------------------------------------------
# POOL-POLLER-PERACCT (row M): one poller per account, shared pressure board
# ---------------------------------------------------------------------------


class PoolPressureBoard:
    """Latest known ``used_percent`` per pool account (tiny + lock-guarded).

    Each per-account poller publishes every successful scrape here via its
    ``reading_listener``; pool selection reads it for the opt-in
    least-pressure ranking. A missing entry means "no fresh reading" — the
    ranking treats unknown as unrankable rather than pretending 0%.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._percents: dict[str, float] = {}

    def record(self, name: str, used_percent: float) -> None:
        with self._lock:
            self._percents[name] = float(used_percent)

    def pressure(self, name: str) -> float | None:
        """Most recent used_percent for ``name``, or None without a reading."""
        with self._lock:
            value = self._percents.get(name)
        return value

    def has_all(self, names: Collection[str]) -> bool:
        """True only when EVERY given name has at least one reading."""
        with self._lock:
            return all(name in self._percents for name in names)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._percents)


def default_pool_auth_dir() -> Path:
    """Default per-account codex bundle directory (computed lazily so tests
    can retarget ``HOME``)."""
    return Path.home() / ".config" / "webgpt" / "codex"


def _env_name_suffix(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name.upper())


def account_auth_json_path(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve one pool account's codex ``auth.json`` path.

    Precedence: explicit ``WEBGPT_CODEX_AUTH_JSON_<NAME>`` (name upper-cased,
    non-alphanumerics folded to ``_``) wins; otherwise
    ``<pool-dir>/<name>.auth.json`` where the pool dir comes from
    ``WEBGPT_POOL_AUTH_DIR`` or :func:`default_pool_auth_dir`.

    The file is NOT required to exist here — a missing file simply means
    "this account has no usage credential" and its poller idles silently.
    """
    env = os.environ if environ is None else environ
    override = env.get(ACCOUNT_AUTH_JSON_ENV_PREFIX + _env_name_suffix(name), "")
    override = override.strip()
    if override:
        return Path(override)
    pool_dir_raw = env.get(POOL_AUTH_DIR_ENV, "").strip()
    pool_dir = Path(pool_dir_raw) if pool_dir_raw else default_pool_auth_dir()
    return pool_dir / f"{name}.auth.json"


def make_account_token_provider(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> TokenProvider:
    """Filesystem-backed bearer source bound to one account's auth.json.

    A missing file idles that account's poller WITHOUT importing
    :mod:`gpt.transport.codex_auth` or touching the network (checked per
    cycle, so a credential added later starts working without a restart);
    a present file goes through the same lazy ``CodexAuthManager`` dormancy
    contract as the historical global path. Provider errors are swallowed
    upstream by :meth:`UsagePoller._resolve_token` like any other source.
    """
    path = account_auth_json_path(name, environ=environ)
    manager: Any = None

    async def provider() -> str | None:
        nonlocal manager
        if not path.is_file():
            return None
        if manager is None:
            from gpt.transport.codex_auth import CodexAuthError, CodexAuthManager

            manager = CodexAuthManager(auth_path=path)
        try:
            return await manager.get_access_token()
        except CodexAuthError:
            return None

    return provider


def _board_recorder(
    board: PoolPressureBoard, name: str
) -> Callable[[UsageReading], None]:
    def listener(reading: UsageReading) -> None:
        board.record(name, reading.used_percent)

    return listener


def create_account_pollers(
    breakers: Mapping[str, RateLimitBreaker],
    *,
    token_provider_factory: Callable[[str], TokenProvider] | None = None,
    http_get: HttpGetFn | None = None,
    wall_clock: Callable[[], float] = time.time,
    stagger: bool = True,
) -> tuple[dict[str, UsagePoller], PoolPressureBoard]:
    """Build one usage poller per pool account plus its shared pressure board.

    The non-empty ``name -> breaker`` map is exactly what row S produced under
    ``WEBGPT_BREAKER_SCOPE=auto`` with >= 2 accounts, so its shape encodes both
    preconditions: anything else (global scope, single account, no breakers)
    returns an empty result and callers keep the historical single
    global-breaker poller. The ``WEBGPT_USAGE_POLL_SECONDS`` gate keeps its
    meaning unchanged — when unset/<=0 nothing is constructed at all (default
    OFF dormancy), when enabled each returned poller starts its own loop
    advising ONLY its own account's breaker (one-way ``advise_pressure``).

    Start offsets are staggered by ``poll_seconds / N`` (in sorted-name order)
    so N pollers never burst the usage endpoint in lockstep. Tests inject
    ``token_provider_factory``/``http_get`` exactly like the single poller.
    """
    empty_board = PoolPressureBoard()
    poll_seconds = _env_float(POLL_SECONDS_ENV, DEFAULT_POLL_SECONDS)
    if len(breakers) < 2 or poll_seconds <= 0.0:
        return {}, empty_board
    board = PoolPressureBoard()
    providers = token_provider_factory or make_account_token_provider
    names = sorted(breakers)
    total = len(names)
    pollers: dict[str, UsagePoller] = {}
    for index, name in enumerate(names):
        pollers[name] = UsagePoller(
            breakers[name],
            poll_seconds=poll_seconds,
            token_provider=providers(name),
            http_get=http_get,
            wall_clock=wall_clock,
            start_delay=poll_seconds * index / total if stagger else 0.0,
            reading_listener=_board_recorder(board, name),
        )
    return pollers, board


__all__ = [
    "ACCOUNT_AUTH_JSON_ENV_PREFIX",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_USAGE_URL",
    "POLL_SECONDS_ENV",
    "POOL_AUTH_DIR_ENV",
    "PoolPressureBoard",
    "RateLimitWindow",
    "UsagePoller",
    "UsageReading",
    "account_auth_json_path",
    "create_account_pollers",
    "default_http_get",
    "default_pool_auth_dir",
    "extract_rate_limit_window",
    "extract_used_percent",
    "make_account_token_provider",
    "make_web_token_cache_provider",
]
