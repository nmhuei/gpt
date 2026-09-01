"""Browser-backed credentials for the hybrid HTTP transport.

The browser remains the authority for ChatGPT authentication.  This module only
reads the authenticated context and performs the browser-context calls that
cannot reliably be reproduced outside Chromium (notably sentinel requirements).
"""

from __future__ import annotations

import asyncio
import base64
import calendar
import hashlib
import json
import logging
import os
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from gpt.state import AuthRequired, ProtocolChanged

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenBundle:
    """Immutable snapshot of credentials needed by ``CurlCffiTransport``."""

    access_token: str
    cookies: Mapping[str, str]
    cf_clearance: str | None = None
    oai_device_id: str | None = None
    is_local_mock: bool = False
    # ChatGPT account uuid, when the extraction path can determine one.  Only
    # used to add the optional ChatGPT-Account-ID header on the authed
    # f/conversation prepare path; stays None for cookie-session bundles.
    chatgpt_account_id: str | None = None

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items())


@dataclass(frozen=True)
class SentinelTokens:
    """Per-turn browser-issued challenge tokens."""

    requirements_token: str | None = None
    proof_token: str | None = None
    turnstile_token: str | None = None
    # PORT-F-CONV-RECIPE: when the requirements token came from the prepare
    # stage (only ``prepare_token`` in the envelope), it must ride under the
    # ``...-Prepare-Token`` header name instead of the final one (spec §1).
    use_prepare_header: bool = False


# Anonymous (no-auth) endpoints live under this backend prefix.  Kept as a
# single constant so an authenticated variant is a one-line change later.
_ANON_BACKEND = "/backend-anon"

# Envelope keys that may carry the final requirements token across the legacy
# and prepare/finalize response shapes.
_REQUIREMENTS_TOKEN_KEYS = (
    "token",
    "requirements_token",
    "chat_requirements_token",
    "sentinel_token",
)

# Envelope keys that may carry the sentinel lifetime in seconds.  Verified
# captures expose ``expire_after`` ≈ 540 on the requirements envelope.
_SENTINEL_EXPIRY_KEYS = ("expire_after", "expires_after")

# Fallback lifetime when the envelope omits an explicit expiry.  Deliberately
# below the observed ~540s server-side validity.
_DEFAULT_SENTINEL_TTL = 480.0

# Safety margin subtracted from ``expire_after`` so a cached sentinel never
# collides with its server-side expiry mid-turn.  Overridable for tests.
_SENTINEL_TTL_MARGIN_ENV = "WEBGPT_SENTINEL_TTL_MARGIN"
_DEFAULT_SENTINEL_TTL_MARGIN = 60.0

# Env switch to disable sentinel caching entirely (per-turn mint as before).
_SENTINEL_CACHE_ENV = "WEBGPT_SENTINEL_CACHE"

# Env switch to disable the in-page SentinelSDK mint and roll back to the
# pure prepare/finalize/legacy requirements flow.
_SENTINEL_SDK_ENV = "WEBGPT_SENTINEL_SDK"

# Disk cache of the last successful TokenBundle (T4-lite).  Written next to
# the browser profile so a gateway restart within ``refresh_interval`` can
# serve requests without touching the browser at all.  The file holds the
# raw access token: it is always written with mode 0600 via an atomic
# tmp+rename, its contents are never logged, and it is only trusted while
# younger than ``refresh_interval``.
_TOKEN_CACHE_FILENAME = "webgpt-token-cache.json"
_TOKEN_CACHE_VERSION = 1

# In-page SentinelSDK mint script.  Evidence (sentinel-sdk-probe-2026-08-24):
# ChatGPT lazily injects ``/backend-api/sentinel/sdk.js`` which exposes
# ``globalThis.SentinelSDK``; ``token(flow)`` resolves a JSON string
# ``{p, t, c}`` (proof, turnstile, chat-requirements token) after solving the
# PoW + turnstile entirely inside the page context.  We replicate exactly what
# the page does — inject the same <script>, init with flow 'chatgpt' (wrapped:
# some deployments auto-init), then call token('chatgpt').
_SENTINEL_SDK_SCRIPT = """async () => {
    const FLOW = 'chatgpt';
    const SDK_URL = '/backend-api/sentinel/sdk.js';
    const LOAD_TIMEOUT_MS = 10000;
    if (!globalThis.SentinelSDK) {
        await new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = SDK_URL;
            script.async = true;
            const timer = setTimeout(
                () => reject(new Error('sentinel sdk.js load timed out')),
                LOAD_TIMEOUT_MS,
            );
            script.onload = () => { clearTimeout(timer); resolve(null); };
            script.onerror = () => {
                clearTimeout(timer);
                reject(new Error('sentinel sdk.js failed to load'));
            };
            document.head.appendChild(script);
        });
    }
    if (!globalThis.SentinelSDK || typeof globalThis.SentinelSDK.token !== 'function') {
        throw new Error('SentinelSDK did not expose token() after loading');
    }
    if (typeof globalThis.SentinelSDK.init === 'function') {
        try {
            await globalThis.SentinelSDK.init(FLOW);
        } catch (error) {
            // init() may be idempotent or already implicit; token() below is
            // the authority on whether the SDK path actually works.
        }
    }
    const raw = await globalThis.SentinelSDK.token(FLOW);
    return typeof raw === 'string' ? raw : JSON.stringify(raw ?? null);
}"""


# ---------------------------------------------------------------------------
# PORT-F-CONV-RECIPE — authed /f/conversation prepare chain
#
# Local, browserless re-implementation of the sentinel bootstrap proof and the
# SHA3-512 proof-of-work solver, byte-matching gptweb2api's sentinel.go
# (spec: docs/reports/f-conversation-recipe-fields.md).  Everything here is
# pure computation; HTTP stays in the transport layer.
# ---------------------------------------------------------------------------

# Opt-in switch for the full authed prepare flow (default OFF: legacy in-page
# mint keeps running until live verification).
_FCONV_PREPARE_ENV = "WEBGPT_FCONV_PREPARE"

# Body ``p`` proof carries prefix C; the per-turn PoW header carries prefix B.
BOOTSTRAP_PROOF_PREFIX = "gAAAAAC"
PROOF_TOKEN_PREFIX = "gAAAAAB"

# The bootstrap answer embeds a timestamp (element [17]), so it must be
# re-solved after this long (sentinelBootstrapProofTTL).
_BOOTSTRAP_PROOF_TTL_SECONDS = 600.0

# Proof budget from sentinelProofMaxAttempts.
SENTINEL_PROOF_MAX_ATTEMPTS = 500_000

# Bootstrap difficulty is fixed at "0" (sentinel.go::bootstrapProof).
_BOOTSTRAP_DIFFICULTY = "0"

# Build-hash snapshot 8/2026.  TODO [CẦN VERIFY] #5: rotates with ChatGPT
# builds; if prepare suddenly starts failing, re-capture this constant first.
_SENTINEL_BUILD_HASH = "prod-a696433ddfe0489db6696cae8c5778c2128f26e8"

# Element [10]: separator between the two halves is U+2212 MINUS SIGN, not a
# plain hyphen (TODO [CẦN VERIFY] #6: copy exact byte when editing here).
_WEBKIT_GUM_STRING = (
    "webkitGetUserMedia−"  # noqa: RUF001 - U+2212 MINUS SIGN is load-bearing
    "function webkitGetUserMedia() { [native code] }"
)

# Fixed-point bootstrap fallback used only when the LOCAL bootstrap solve
# exhausts its budget (server-challenge exhaustion raises instead).
_BOOTSTRAP_POW_FALLBACK_MARKER = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

_DATE_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DATE_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

_TIMEZONE_ENV = "WEBGPT_TIMEZONE"


class SentinelPowExhausted(RuntimeError):
    """The SHA3-512 proof search ran out of its attempt budget."""


def sentinel_date_str(timestamp: float) -> str:
    """JS-style local date string for fingerprint element [1].

    Format: ``Mon Aug 25 2026 14:03:09 GMT+0700``.  Day/month names are
    hardcoded English exactly like Go's ``Format("Mon")``/``Format("Jan")``
    — locale-aware strftime would leak the machine language into the
    fingerprint.
    """
    local = time.localtime(timestamp)
    # Seconds east of UTC: converting the local wall clock back with timegm
    # and diffing against the source epoch avoids DST table guessing.
    offset_seconds = calendar.timegm(local) - int(timestamp)
    sign = "+" if offset_seconds >= 0 else "-"
    magnitude = abs(offset_seconds)
    hours, remainder = divmod(magnitude // 60, 60)
    minutes = remainder % 60
    return (
        f"{_DATE_DAYS[local.tm_wday]} {_DATE_MONTHS[local.tm_mon - 1]} "
        f"{local.tm_mday:02d} {local.tm_year} "
        f"{local.tm_hour:02d}:{local.tm_min:02d}:{local.tm_sec:02d} "
        f"GMT{sign}{hours:02d}{minutes:02d}"
    )


def sentinel_fingerprint(
    user_agent: str,
    device_id: str,
    timestamp: float,
    attempt: int = 0,
    elapsed_ms: float = 0.0,
) -> list[Any]:
    """Build the fixed-order 18-element sentinel config array (spec §2)."""
    return [
        4000,                                   # [0]
        sentinel_date_str(timestamp),           # [1] local date string
        4294705152,                             # [2]
        attempt,                                # [3] counter, mutated per try
        user_agent,                             # [4] MUST match UA header
        None,                                   # [5]
        _SENTINEL_BUILD_HASH,                   # [6]
        "en-US",                                # [7]
        "en-US",                                # [8]
        elapsed_ms,                             # [9] ms since solve start
        _WEBKIT_GUM_STRING,                     # [10] U+2212 inside!
        "location",                             # [11]
        "ontransitionend",                      # [12]
        123.456,                                # [13]
        device_id,                              # [14] MUST match OAI-Device-Id
        "",                                     # [15]
        8,                                      # [16]
        float(int(timestamp * 1000)),           # [17] epoch millis
    ]


def encode_sentinel_config(config: list[Any]) -> str:
    """Compact-JSON + base64 encode one config array, Go byte-shape.

    Go ``json.Marshal`` prints integer-valued float64 in shortest form
    (``0``, ``123``, not ``0.0``), so integral floats are narrowed to ints
    before dumping (TODO [CẦN VERIFY] #4: confirm against a live probe).
    ``ensure_ascii=False`` keeps U+2212 as raw UTF-8 like Go does; any JSON
    parser decodes both spellings identically, so the choice only has to be
    self-consistent between what we hash and what we submit.
    """
    normalized = [
        int(value) if isinstance(value, float) and value.is_integer() else value
        for value in config
    ]
    text = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def solve_sentinel_pow(
    seed: str,
    difficulty: str,
    user_agent: str,
    device_id: str,
    *,
    max_attempts: int = SENTINEL_PROOF_MAX_ATTEMPTS,
) -> str:
    """Solve the SHA3-512 prefix proof; returns bare base64 (NO prefix).

    digest = sha3_512(seed + base64(config)); the hex prefix must compare
    lexicographically ≤ difficulty.  An empty difficulty can never match —
    bootstrap callers fall back, server challenges raise.
    """
    target = difficulty or ""
    if not target:
        raise SentinelPowExhausted("Empty proof difficulty never matches.")
    started = time.monotonic()
    timestamp = time.time()
    config = sentinel_fingerprint(user_agent, device_id, timestamp)
    for attempt in range(max_attempts):
        config[3] = attempt
        config[9] = (time.monotonic() - started) * 1000.0
        encoded = encode_sentinel_config(config)
        digest = hashlib.sha3_512((seed + encoded).encode()).hexdigest()
        if digest[: len(target)] <= target:
            return encoded
    raise SentinelPowExhausted(
        f"No proof found within {max_attempts} attempts."
    )


def bootstrap_pow_fallback(seed: str) -> str:
    """Fixed-marker fallback for an exhausted LOCAL bootstrap solve."""
    return _BOOTSTRAP_POW_FALLBACK_MARKER + base64.b64encode(
        json.dumps(seed).encode("utf-8")
    ).decode("ascii")


def requirements_token_from_prepare(envelope: Any) -> tuple[str | None, bool]:
    """Extract (token, is_prepare_stage) from a chat-requirements envelope.

    ``token`` wins over ``prepare_token``; only the latter means the token
    must ride under the ``...-Prepare-Token`` header name (spec §1 mapping).
    """
    token = _find_string(envelope, "token")
    if token:
        return token, False
    prepare_token = _find_string(envelope, "prepare_token", "prepareToken")
    if prepare_token:
        return prepare_token, True
    return None, False


def pow_challenge_from_prepare(envelope: Any) -> tuple[bool, str, str]:
    """Read (required, seed, difficulty) preferring proofofwork over
    proof_challenge (both generations share seed/difficulty names)."""
    if not isinstance(envelope, dict):
        return False, "", ""
    challenge = envelope.get("proofofwork")
    if not isinstance(challenge, dict):
        challenge = envelope.get("proof_challenge")
    if not isinstance(challenge, dict):
        return False, "", ""
    seed = _find_string(challenge, "seed") or ""
    difficulty = _find_string(challenge, "difficulty") or ""
    required = challenge.get("required")
    if not isinstance(required, bool):
        # A challenge object without an explicit flag but carrying a seed is
        # de-facto required.
        required = bool(seed)
    return bool(required and seed), seed, difficulty


def build_fconv_prepare_body(
    *,
    timezone_name: str,
    timezone_offset_min: int,
    model: str | None = None,
    conversation_id: str | None = None,
    parent_message_id: str = "client-created-root",
    prepare_state: str = "none",
) -> dict[str, Any]:
    """Exact 14+1-field body for POST /backend-api/f/conversation/prepare.

    ``conversation_id`` is appended ONLY when continuing an existing
    conversation (spec §3: absent on new conversations).
    """
    body: dict[str, Any] = {
        "action": "next",
        "client_contextual_info": {
            "app_name": "chatgpt.com",
            "has_web_push_capabilities": False,
            "web_push_notification_permission": "default",
        },
        "client_prepare_dispatch": "conversation",
        "client_prepare_source": "chatgpt_web_client",
        "client_prepare_state": prepare_state,
        "conversation_mode": {"kind": "primary_assistant"},
        "local_function_names": [],
        "model": model or "auto",
        "parent_message_id": parent_message_id,
        "supported_encodings": ["v1"],
        "supports_buffering": True,
        "system_hints": [],
        "timezone": timezone_name,
        "timezone_offset_min": timezone_offset_min,
    }
    if conversation_id:
        body["conversation_id"] = conversation_id
    return body


def local_utc_offset_minutes(timestamp: float | None = None) -> int:
    """Local UTC offset in minutes east of UTC (VN = +420)."""
    ts = time.time() if timestamp is None else timestamp
    local = time.localtime(ts)
    return round((calendar.timegm(local) - int(ts)) / 60.0)


def resolve_local_timezone() -> str:
    """Best-effort IANA timezone name from the tz database (never hardcoded).

    Order: WEBGPT_TIMEZONE env → tzlocal (if installed) → validated
    ``time.tzname`` → Etc/GMT±h derived from the measured offset (Etc/GMT
    names are POSIX-inverted: UTC+7 is ``Etc/GMT-7``) → ``UTC``.
    """
    configured = os.environ.get(_TIMEZONE_ENV, "").strip()
    if configured:
        return configured
    try:
        from tzlocal import get_localzone

        return str(get_localzone())
    except Exception as exc:
        logger.debug("tzlocal unavailable or failed: %s", exc)
    try:
        from zoneinfo import ZoneInfo

        for candidate in time.tzname:
            if candidate:
                try:
                    ZoneInfo(candidate)
                    return candidate
                except Exception:
                    continue
    except Exception as exc:
        logger.debug("zoneinfo timezone-name probe failed: %s", exc)
    offset = local_utc_offset_minutes()
    if offset == 0:
        return "UTC"
    hours = abs(offset) // 60
    sign = "-" if offset > 0 else "+"
    return f"Etc/GMT{sign}{hours}"


def fconv_prepare_enabled() -> bool:
    """Whether the authed f/conversation prepare flow is opted in.

    Deliberately default-OFF: OFF must reproduce the pre-existing send()
    behavior byte-for-byte until a live verification passes.
    """
    flag = os.environ.get(_FCONV_PREPARE_ENV, "").strip().casefold()
    return flag in {"1", "true", "yes", "on"}


class TokenManager:
    """Extract and periodically refresh authenticated browser credentials."""

    def __init__(
        self,
        page: Any,
        *,
        refresh_interval: float = 1_800,
        origin: str = "https://chatgpt.com",
        auto_login: Callable[[], Awaitable[bool]] | None = None,
        allow_local_mock: bool | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        if refresh_interval <= 0:
            raise ValueError("refresh_interval must be positive")
        self.page = page
        self.refresh_interval = refresh_interval
        self.origin = origin.rstrip("/")
        self._auto_login = auto_login
        self.allow_local_mock = (
            True if allow_local_mock is None else allow_local_mock
        )
        # ``None`` disables the disk cache entirely; behaviour is then exactly
        # the pre-cache one (browser always consulted on first extract).
        self._cache_path: Path | None = (
            Path(cache_dir) / _TOKEN_CACHE_FILENAME if cache_dir else None
        )
        self._bundle: TokenBundle | None = self._load_disk_cache()
        if self._bundle is not None:
            # The wall-clock ``stored_at`` timestamp already proved freshness
            # in _load_disk_cache; anchor the monotonic interval clock here so
            # refresh_if_needed serves the cached bundle without a browser hit.
            self._last_refresh = time.monotonic()
        else:
            self._last_refresh = 0.0
        self._lock = asyncio.Lock()
        self._auto_login_attempted = False
        # Sentinel cache: (tokens, expire_at_monotonic).  See get_sentinel_tokens.
        self._sentinel_cache: tuple[SentinelTokens, float] | None = None
        self._sentinel_lock = asyncio.Lock()
        # Bootstrap proof cache: (proof, user_agent, device_id, expire_at).
        # See bootstrap_proof_token.
        self._bootstrap_cache: tuple[str, str, str, float] | None = None

    async def extract_all(self) -> TokenBundle:
        """Read cookies, access token, and device id from the browser context."""
        async with self._lock:
            return await self._extract_all_unlocked()

    async def _extract_all_unlocked(self) -> TokenBundle:
        context = getattr(self.page, "context", None)
        if context is None or not hasattr(context, "cookies"):
            raise ProtocolChanged("The browser page does not expose a cookie context.")
        browser_cookies = await context.cookies()
        cookies = {
            item["name"]: item["value"]
            for item in browser_cookies
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str)
        }
        session = await self.page.evaluate(
            """async () => {
                const response = await fetch('/api/auth/session', {
                    credentials: 'include'
                });
                if (!response.ok) return {};
                return response.json();
            }"""
        )
        access_token = _find_string(session, "accessToken", "access_token")
        if not access_token and await self._attempt_auto_login():
            # A successful login updates cookies/storage asynchronously.  Read
            # the session endpoint again instead of trusting the login helper.
            session = await self.page.evaluate(
                """async () => {
                    const response = await fetch('/api/auth/session', {
                        credentials: 'include'
                    });
                    if (!response.ok) return {};
                    return response.json();
                }"""
            )
            access_token = _find_string(session, "accessToken", "access_token")
            browser_cookies = await context.cookies()
            cookies = {
                item["name"]: item["value"]
                for item in browser_cookies
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and isinstance(item.get("value"), str)
            }
        if not access_token:
            if self.allow_local_mock:
                # Never persist the placeholder mock credentials to disk.
                self._bundle = _local_mock_bundle()
                self._last_refresh = time.monotonic()
                return self._bundle
            raise AuthRequired("ChatGPT browser session has no access token.")
        # Device id sourcing priority: live ChatGPT profiles really carry the
        # ``oai-did`` cookie (.chatgpt.com / .openai.com); the other cookie
        # names are legacy fallbacks.  Browser storage is consulted last and
        # only when no cookie matched, because hot-copied profiles can lose
        # leveldb state entirely.
        device_id = (
            cookies.get("oai-did")
            or cookies.get("oai-device-id")
            or cookies.get("oai_device_id")
            or await self.page.evaluate(
                """() => localStorage.getItem('oai-device-id')
                    || localStorage.getItem('oai_device_id')"""
            )
        )
        if not isinstance(device_id, str) or not device_id:
            previous = self._bundle.oai_device_id if self._bundle else None
            if isinstance(previous, str) and previous:
                # Re-extraction found nothing in the browser: keep the id
                # already minted/persisted for this profile instead of
                # rotating identities between turns.
                device_id = previous
            else:
                # Fresh login / profile that never rendered the app shell.
                # ChatGPT accepts any lowercase UUID4 device id (see
                # scripts/cert/fconv_replay.py); the disk-cache write below
                # persists it so later turns stay stable.
                device_id = str(uuid.uuid4())
        self._bundle = TokenBundle(
            access_token=access_token,
            cookies=MappingProxyType(cookies),
            cf_clearance=cookies.get("cf_clearance"),
            oai_device_id=device_id,
        )
        self._last_refresh = time.monotonic()
        self._write_disk_cache(self._bundle)
        return self._bundle

    def _load_disk_cache(self) -> TokenBundle | None:
        """Load a still-fresh persisted bundle, or None on any problem.

        The cache is trusted only when its ``stored_at`` wall-clock age is
        below ``refresh_interval`` and the payload shape is fully valid.
        Missing, stale, corrupt, or malformed caches are ignored silently —
        a cold start then simply falls back to the browser as before.
        """
        path = self._cache_path
        if path is None:
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("version") != _TOKEN_CACHE_VERSION:
            return None
        stored_at = raw.get("stored_at")
        if isinstance(stored_at, bool) or not isinstance(stored_at, (int, float)):
            return None
        age = time.time() - float(stored_at)
        if age < 0 or age >= self.refresh_interval:
            return None
        access_token = raw.get("access_token")
        cookies_raw = raw.get("cookies")
        if not isinstance(access_token, str) or not access_token:
            return None
        if not isinstance(cookies_raw, dict) or not all(
            isinstance(name, str)
            and isinstance(value, str)
            for name, value in cookies_raw.items()
        ):
            return None
        cf_clearance = raw.get("cf_clearance")
        oai_device_id = raw.get("oai_device_id")
        for candidate in (cf_clearance, oai_device_id):
            if candidate is not None and not isinstance(candidate, str):
                return None
        return TokenBundle(
            access_token=access_token,
            cookies=MappingProxyType(dict(cookies_raw)),
            cf_clearance=cf_clearance,
            oai_device_id=oai_device_id,
        )

    def _write_disk_cache(self, bundle: TokenBundle) -> None:
        """Persist a successful extract atomically with mode 0600.

        Best-effort: any I/O failure is swallowed because the disk cache is
        an optimisation, never part of the auth correctness path.  The file
        contains the live access token, so it is created 0600 explicitly
        (chmod after write guards against umask surprises) and swapped in via
        ``os.replace`` so readers never observe a partial file.  Contents are
        never logged.
        """
        path = self._cache_path
        if path is None or bundle.is_local_mock:
            return
        payload = {
            "version": _TOKEN_CACHE_VERSION,
            "stored_at": time.time(),
            "access_token": bundle.access_token,
            "cookies": dict(bundle.cookies),
            "cf_clearance": bundle.cf_clearance,
            "oai_device_id": bundle.oai_device_id,
        }
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except OSError:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _attempt_auto_login(self) -> bool:
        """Try the configured account login once, without exposing credentials."""
        if self._auto_login_attempted:
            return False
        self._auto_login_attempted = True
        callback = self._auto_login or _configured_auto_login
        try:
            return bool(await callback())
        except Exception:
            # The subsequent auth error is intentionally generic: exceptions
            # from browser identity providers can contain sensitive details.
            return False

    async def refresh_if_needed(self) -> TokenBundle:
        """Return a current snapshot, refreshing it at most once per interval."""
        if self._bundle is not None and time.monotonic() - self._last_refresh < self.refresh_interval:
            return self._bundle
        async with self._lock:
            if (
                self._bundle is not None
                and time.monotonic() - self._last_refresh < self.refresh_interval
            ):
                return self._bundle
            return await self._extract_all_unlocked()

    async def get_sentinel_tokens(self, conversation_id: str | None = None) -> SentinelTokens:
        """Return sentinel tokens for one conversation turn, cached per TTL.

        Minting hits the browser page twice (prepare/finalize) or once (legacy)
        and costs 100-300ms of network time, so the resulting tokens are cached
        until ``expire_after - margin`` seconds have elapsed (margin from
        ``WEBGPT_SENTINEL_TTL_MARGIN``, default 60s).  Envelopes without an
        explicit ``expire_after`` fall back to a conservative 480s TTL.

        Set ``WEBGPT_SENTINEL_CACHE=0`` to disable caching and mint every turn.
        The local-mock path is unaffected and never touches the cache.
        """
        if self._bundle is not None and self._bundle.is_local_mock:
            return SentinelTokens(requirements_token="local-mock-sentinel")
        if not _sentinel_cache_enabled():
            tokens, _ttl = await self._mint_sentinel(conversation_id)
            return tokens
        now = time.monotonic()
        if self._sentinel_cache is not None:
            tokens, expire_at = self._sentinel_cache
            if now < expire_at:
                return tokens
        async with self._sentinel_lock:
            now = time.monotonic()
            if self._sentinel_cache is not None:
                tokens, expire_at = self._sentinel_cache
                if now < expire_at:
                    return tokens
            tokens, ttl = await self._mint_sentinel(conversation_id)
            self._sentinel_cache = (tokens, now + ttl)
            return tokens

    def invalidate_sentinel(self) -> None:
        """Drop the cached sentinel tokens so the next call mints afresh.

        Transport layers should call this when a request carrying the cached
        sentinel is rejected with 401/403 — that means the server-side token
        expired or was invalidated earlier than our TTL assumed, and retrying
        with the same cache would keep failing until it naturally expires.
        """
        self._sentinel_cache = None

    def invalidate_access_token(self) -> None:
        """Drop the cached bundle so the next refresh re-extracts everything.

        The transport should call this when the server rejects the Bearer
        access token / cookie jar itself (401/403 on endpoints that
        authenticate purely with them, e.g. the codex/responses branch) —
        unlike ``invalidate_sentinel`` this is about the credential snapshot,
        not the per-turn challenge tokens.  The disk cache is removed too:
        a rejected access token must never be re-served after a restart
        within ``refresh_interval``.
        """
        self._bundle = None
        self._last_refresh = 0.0
        path = self._cache_path
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    async def _mint_sentinel(self, conversation_id: str | None) -> tuple[SentinelTokens, float]:
        """Fetch a fresh sentinel envelope and compute its effective TTL.

        The cheap requirements fetch runs first; when the envelope already
        carries proof + turnstile artifacts (some deployments do) the SDK is
        never loaded.  Otherwise the full set is minted through ChatGPT's own
        in-page SentinelSDK (verified live 2026-08-24, see
        ``docs/reports/sentinel-sdk-probe-2026-08-24.md``), and only if that
        fails too do we fall back to the bare requirements-only tokens.
        """
        result = await self._sentinel_requirements(conversation_id)
        if not isinstance(result, dict):
            raise ProtocolChanged("Sentinel requirements response was not an object.")
        if isinstance(result.get("status"), int) and result["status"] >= 400:
            raise AuthRequired("ChatGPT rejected sentinel requirements.")
        tokens = SentinelTokens(
            requirements_token=_find_string(result, *_REQUIREMENTS_TOKEN_KEYS),
            proof_token=_find_string(result, "proof_token", "proofToken"),
            turnstile_token=_find_string(result, "turnstile_token", "turnstileToken"),
        )
        ttl = _sentinel_ttl_seconds(result)
        if tokens.proof_token and tokens.turnstile_token:
            return tokens, ttl
        sdk_tokens = await self._mint_via_sdk()
        if sdk_tokens is not None:
            return sdk_tokens
        return tokens, ttl

    async def _mint_via_sdk(self) -> tuple[SentinelTokens, float] | None:
        """Mint the full sentinel set via ``globalThis.SentinelSDK`` in-page.

        Injects the same ``sdk.js`` script tag the page itself uses, calls
        ``token('chatgpt')``, and splits the returned JSON string into
        ``{p, t, c}`` → proof / turnstile / chat-requirements token.  The
        requirements token (``c``) feeds the existing TTL logic so caching
        behaves identically to the legacy flow.

        Returns None — never raises — when the switch is off, the script is
        unavailable, or the payload lacks any of the three required values,
        letting the caller keep its prepare/finalize/legacy result.
        """
        if not _sentinel_sdk_enabled():
            return None
        try:
            raw = await self.page.evaluate(_SENTINEL_SDK_SCRIPT)
            # Tolerate evaluate shims that hand back a parsed object.
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        proof = payload.get("p")
        turnstile = payload.get("t")
        requirements = payload.get("c")
        if not all(
            isinstance(value, str) and value
            for value in (proof, turnstile, requirements)
        ):
            return None
        envelope: dict[str, Any] = {"token": requirements}
        for key in _SENTINEL_EXPIRY_KEYS:
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                envelope[key] = value
        return (
            SentinelTokens(
                requirements_token=requirements,
                proof_token=proof,
                turnstile_token=turnstile,
            ),
            _sentinel_ttl_seconds(envelope),
        )

    async def bootstrap_proof_token(self, user_agent: str, device_id: str) -> str:
        """Self-solve the sentinel bootstrap proof locally (no browser).

        seed = random float printed with exactly 6 decimals; the answer embeds
        a timestamp, so results are cached for 10 minutes keyed on the
        (user_agent, device_id) identity pair that also rides in the
        fingerprint.  Prefix ``gAAAAAC`` distinguishes it from per-turn PoW
        proofs (``gAAAAAB``).  A budget-exhausted local solve falls back to
        the fixed bootstrap marker — only server-challenge exhaustion raises.
        """
        now = time.monotonic()
        cached = self._bootstrap_cache
        if cached is not None:
            proof, cached_ua, cached_device, expire_at = cached
            if now < expire_at and cached_ua == user_agent and cached_device == device_id:
                return proof
        seed = format(random.random(), ".6f")
        try:
            answer = solve_sentinel_pow(
                seed, _BOOTSTRAP_DIFFICULTY, user_agent, device_id
            )
        except SentinelPowExhausted:
            answer = bootstrap_pow_fallback(seed)
        proof = BOOTSTRAP_PROOF_PREFIX + answer
        self._bootstrap_cache = (
            proof,
            user_agent,
            device_id,
            now + _BOOTSTRAP_PROOF_TTL_SECONDS,
        )
        return proof

    async def prepare_conduit(self) -> str | None:
        """Fetch a conduit token from the conversation prepare endpoint.

        Evidence (Burp capture 2026-08-24): the browser issues
        ``POST /backend-anon/f/conversation/prepare`` before opening a
        conversation and receives an envelope carrying ``conduit_token``.
        Not yet wired into the transport; returns None on any failure so
        callers can degrade gracefully.
        """
        try:
            result = await self._post_in_page(
                f"{_ANON_BACKEND}/f/conversation/prepare", {}
            )
        except Exception:
            # A failed conduit fetch must never break the conversation flow.
            return None
        if not isinstance(result, dict):
            return None
        if isinstance(result.get("status"), int) and result["status"] >= 400:
            return None
        return _find_string(result, "conduit_token", "conduitToken")

    async def _sentinel_requirements(self, conversation_id: str | None) -> Any:
        """Run the two-step prepare/finalize sentinel flow with legacy fallback.

        Evidence (live probe 2026-08-24): finalize accepts
        ``{"prepare_token": <prepare_token>}`` → 200, while the ``{"p": …}``
        shape returns 500.  Both shapes are attempted (new one first); any
        failure in either step falls back to the single-step legacy endpoint,
        which still works.
        """
        try:
            prepared = await self._post_in_page(
                f"{_ANON_BACKEND}/sentinel/chat-requirements/prepare",
                {"conversation_id": conversation_id} if conversation_id else {},
            )
            prepare_token = (
                _find_string(prepared, "prepare_token", "prepareToken")
                if isinstance(prepared, dict)
                else None
            )
            if prepare_token is None and _has_requirements_token(prepared):
                # Defensive: a deployment that answers prepare directly.
                return prepared
            if prepare_token is not None:
                for body in ({"prepare_token": prepare_token}, {"p": prepare_token}):
                    finalized = await self._post_in_page(
                        f"{_ANON_BACKEND}/sentinel/chat-requirements/finalize",
                        body,
                    )
                    if _has_requirements_token(finalized):
                        return finalized
                # Missing token in the finalize envelope: fall through.
        except Exception:
            # JS/network errors must fall back, never propagate past legacy.
            pass
        return await self._legacy_sentinel(conversation_id)

    async def _legacy_sentinel(self, conversation_id: str | None) -> Any:
        """Call the original single-endpoint chat-requirements route."""
        return await self.page.evaluate(
            """async (conversationId) => {
                const response = await fetch('/backend-anon/sentinel/chat-requirements', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'content-type': 'application/json'},
                    body: JSON.stringify({conversation_id: conversationId || undefined})
                });
                if (!response.ok) return {status: response.status};
                return response.json();
            }""",
            conversation_id,
        )

    async def _post_in_page(self, path: str, payload: dict[str, Any]) -> Any:
        """POST JSON from the page context so cookies/CF state match the browser."""
        return await self.page.evaluate(
            """async ({path, payload}) => {
                const response = await fetch(path, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'content-type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                if (!response.ok) return {status: response.status};
                return response.json();
            }""",
            {"path": path, "payload": payload},
        )


def _has_requirements_token(value: Any) -> bool:
    """Whether an envelope already carries a final requirements token."""
    return bool(_find_string(value, *_REQUIREMENTS_TOKEN_KEYS))


def _find_number(value: Any, *keys: str) -> float | None:
    """Find a numeric value in nested response envelopes (bools excluded).

    Numeric counterpart of ``_find_string`` for envelope fields such as
    ``expire_after``.
    """
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                return float(candidate)
        for candidate in value.values():
            found = _find_number(candidate, *keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_number(candidate, *keys)
            if found is not None:
                return found
    return None


def _sentinel_ttl_seconds(envelope: Any) -> float:
    """Effective cache TTL for a sentinel requirements envelope.

    ``expire_after`` (seconds, observed ≈ 540) minus the configured safety
    margin; 480s when the envelope omits the field entirely.
    """
    expire_after = _find_number(envelope, *_SENTINEL_EXPIRY_KEYS)
    if expire_after is None or expire_after <= 0:
        return _DEFAULT_SENTINEL_TTL
    ttl = expire_after - _sentinel_ttl_margin()
    # A margin that swallows the whole lifetime must not produce a negative
    # (always-expired) or zero TTL; fall back to the conservative default.
    return ttl if ttl > 0 else _DEFAULT_SENTINEL_TTL


def _sentinel_ttl_margin() -> float:
    """Safety margin subtracted from the envelope lifetime."""
    raw = os.environ.get(_SENTINEL_TTL_MARGIN_ENV, "").strip()
    try:
        margin = float(raw) if raw else _DEFAULT_SENTINEL_TTL_MARGIN
    except ValueError:
        return _DEFAULT_SENTINEL_TTL_MARGIN
    return margin if margin >= 0 else _DEFAULT_SENTINEL_TTL_MARGIN


def _sentinel_cache_enabled() -> bool:
    """Whether sentinel caching is on (default) or forced off via env."""
    flag = os.environ.get(_SENTINEL_CACHE_ENV, "").strip().casefold()
    return flag not in {"0", "false", "no", "off"}


def _sentinel_sdk_enabled() -> bool:
    """Whether the in-page SentinelSDK mint is on (default) or rolled back.

    ``WEBGPT_SENTINEL_SDK=0`` restores the pre-SDK behaviour exactly: only
    the prepare/finalize/legacy requirements flow runs and no ``sdk.js``
    script is ever injected.
    """
    flag = os.environ.get(_SENTINEL_SDK_ENV, "").strip().casefold()
    return flag not in {"0", "false", "no", "off"}


def _find_string(value: Any, *keys: str) -> str | None:
    """Find a non-empty string in the small nested response envelopes ChatGPT uses."""
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for candidate in value.values():
            found = _find_string(candidate, *keys)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_string(candidate, *keys)
            if found:
                return found
    return None


def local_mock_mode_enabled() -> bool:
    """Return whether this process may use the browser-free local transport.

    This is deliberately opt-in.  A placeholder credential must never be sent
    to ChatGPT in a normal gateway process.
    """
    mode = os.environ.get("WEBGPT_MODE", "").strip().casefold()
    flag = os.environ.get("WEBGPT_LOCAL_MOCK", "").strip().casefold()
    return mode in {"dev", "development", "test", "testing"} or flag in {
        "1",
        "true",
        "yes",
        "on",
    }


def _local_mock_bundle() -> TokenBundle:
    """Create a clearly marked credential snapshot for local-only responses."""
    cookies = MappingProxyType(
        {
            "cf_clearance": "local-mock-clearance",
            "oai-device-id": "local-mock-device",
        }
    )
    return TokenBundle(
        access_token="local-mock-token",
        cookies=cookies,
        cf_clearance=cookies["cf_clearance"],
        oai_device_id=cookies["oai-device-id"],
        is_local_mock=True,
    )


async def _configured_auto_login() -> bool:
    """Run the existing login workflow when all .env credentials are present."""
    from gpt.auth import AutoLoginManager, LoginCredentials
    from gpt.config.settings import load_config

    config = load_config()
    if not (config.email and config.password and config.totp_key):
        return False
    manager = AutoLoginManager(
        profile_dir=config.profile_dir,
        headless=config.headless,
    )
    return await manager.login(
        LoginCredentials(config.email, config.password, config.totp_key)
    )
