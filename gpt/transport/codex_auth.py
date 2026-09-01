"""Codex OAuth credential source for the CODEX-SSE transport branch.

Research basis: ``docs/reports/codex-oauth-research-2026-08-25.md`` — the
``codex-rs`` CLI stores its ChatGPT OAuth grant in ``$CODEX_HOME/auth.json``
(default ``~/.codex/auth.json``, mode 0600) and keeps the access token fresh
by posting a refresh grant to ``https://auth.openai.com/oauth/token`` with a
JSON body.  The refresh token rotates on every use (single-use rotation:
reusing the previous one is detected server-side and revokes the whole
chain), so any concurrent refresh must be serialized across processes.

This module is deliberately NOT wired anywhere yet (task CODEX-AUTH-TOKEN-
SOURCE): it is fully dormant unless ``WEBGPT_CODEX_AUTH_JSON=<path>`` is set
in the environment.  With the flag unset every entry point raises
:class:`CodexAuthDisabled` and nothing reads or writes the filesystem —
integration into ``TokenManager``/factory is a later task.

Design notes mapped to the research findings:

- auth.json schema mirrors ``codex-rs/login/src/auth/storage.rs::AuthDotJson``;
  unknown keys read from an existing file are preserved verbatim on rewrite so
  the real codex CLI can keep sharing the same credential file.
- Access-token expiry comes from the JWT ``exp`` claim (research §3); the
  refresh response's ``expires_in`` wins when present because it needs no
  parsing.
- Refresh window: this module refreshes when the token has less than 60s of
  life left (task spec) instead of codex-rs' 5-minute window.
- Single-use rotation: the whole read-refresh-write critical section runs
  under an exclusive ``flock`` on ``<auth.json>.lock`` and re-reads the file
  inside the lock, so two gateway processes can never both spend the same
  refresh token (the reuse would revoke the chain — research §5.1).  The
  in-process ``asyncio.Lock`` prevents N coroutines from spawning N threads
  for the same refresh.
- A rejected refresh (HTTP 400/401/403, e.g. ``invalid_grant``) marks the
  credential DEAD terminally in memory: every later call fails fast without
  touching disk or network again.  There is deliberately no retry loop — a
  rotated-away refresh token cannot come back, only a fresh ``codex login``
  can fix it.  Transport errors and 5xx are :class:`CodexAuthTransient`
  (retry later is meaningful) but are also never retried internally.
- Resource-server rejections of freshly rotated bearers (repeated 401 on the
  SSE call, review round 13) latch a separate non-terminal "untrusted" state
  via :meth:`CodexAuthManager.mark_untrusted`: while latched, every token
  fetch performs a real refresh grant; one accepted request clears it via
  ``mark_trusted()``.
- If ``last_refresh`` is older than 8 days (codex-rs ``TOKEN_REFRESH_INTERVAL``)
  the refresh chain is considered expired server-side: DEAD, no HTTP attempt.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # Unix-only; without fcntl we still work, just without cross-process locking.
    import fcntl
except ImportError:  # pragma: no cover - platform guard
    fcntl = None  # type: ignore[assignment]

try:  # Same guarded import style as curl_transport.py.
    from curl_cffi.requests import Session as _CurlSyncSession
except ImportError:  # pragma: no cover - optional dependency
    _CurlSyncSession = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Configuration surface
# ---------------------------------------------------------------------------

# Master flag AND path: setting this env to the codex auth.json path turns the
# whole module on.  Unset → everything disabled (default OFF, task requirement).
ENV_AUTH_JSON = "WEBGPT_CODEX_AUTH_JSON"
# Client id for the refresh grant.  Defaults to the public codex-rs client
# (research §2), overridable exactly like CODEX_APP_SERVER_LOGIN_CLIENT_ID.
ENV_CLIENT_ID = "WEBGPT_CODEX_CLIENT_ID"

DEFAULT_TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

DEFAULT_AUTH_PATH = Path.home() / ".codex" / "auth.json"

# Task spec: refresh when less than this much life remains on the access token.
REFRESH_SKEW_SECONDS = 60.0
# codex-rs TOKEN_REFRESH_INTERVAL: a refresh chain unused for 8 days is dead.
REFRESH_CHAIN_TTL_SECONDS = 8 * 86400.0

# Sync poster injected into CodexAuthManager: (url, json_body) -> (status,
# parsed-json-or-None).  Injectable so tests never touch the network.
HttpPostFn = Callable[[str, dict[str, str]], tuple[int, Any]]

# Statuses that mean the grant itself is gone (invalid_grant / revoked /
# account mismatch / entitlement) — terminal, research §3 error taxonomy.
_DEAD_REFRESH_STATUSES = frozenset({400, 401, 403})


def codex_auth_enabled() -> bool:
    """Whether ``WEBGPT_CODEX_AUTH_JSON`` points at a usable path."""
    return bool(os.environ.get(ENV_AUTH_JSON, "").strip())


def default_http_post(url: str, body: dict[str, str]) -> tuple[int, Any]:
    """Default blocking refresh POST via curl_cffi (runs in a worker thread).

    The token endpoint expects JSON (unlike the PKCE exchange which uses a
    form body — research §3).  Any transport-level failure raises so the
    manager can classify it as transient; a non-JSON 200 body yields None.
    """
    if _CurlSyncSession is None:
        raise RuntimeError("curl_cffi is not installed; cannot refresh codex token.")
    with _CurlSyncSession() as session:
        response = session.post(
            url,
            json=body,
            headers={"content-type": "application/json"},
            timeout=30.0,
        )
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    return int(response.status_code), payload


# ---------------------------------------------------------------------------
# Exceptions — one family so future callers can catch broadly or precisely
# ---------------------------------------------------------------------------


class CodexAuthError(RuntimeError):
    """Base class for all codex-auth failures."""


class CodexAuthDisabled(CodexAuthError):
    """``WEBGPT_CODEX_AUTH_JSON`` is not set — module is OFF by design."""


class CodexAuthInvalid(CodexAuthError):
    """auth.json missing/corrupt or lacks a refresh token (needs codex login)."""


class CodexAuthDead(CodexAuthError):
    """Terminal: refresh rejected or chain expired — only re-login fixes it."""


class CodexAuthTransient(CodexAuthError):
    """Retryable failure (network, 5xx, malformed success payload)."""


# ---------------------------------------------------------------------------
# Bundle + auth.json codec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodexAuthBundle:
    """Snapshot of one codex OAuth grant (schema of ``tokens`` + metadata).

    ``expires_at`` is wall-clock epoch seconds from the JWT ``exp`` claim
    (or the refresh response's ``expires_in``); ``None`` means unknown, which
    is treated as expired so a refresh happens on the next call.
    """

    access_token: str
    refresh_token: str
    id_token: str | None = None
    account_id: str | None = None
    last_refresh_epoch: float = 0.0  # 0.0 = unknown (missing/unparsable field)
    expires_at: float | None = None

    def is_fresh(self, now: float | None = None, skew: float = REFRESH_SKEW_SECONDS) -> bool:
        """True when the access token outlives ``now + skew``."""
        if self.expires_at is None:
            return False
        moment = time.time() if now is None else now
        return moment < self.expires_at - skew


def parse_jwt_exp(token: str) -> float | None:
    """Extract the ``exp`` epoch from a JWT's payload segment; None if absent.

    Purely local decoding — no signature verification (we are the consumer,
    the resource server verifies).  Any structural surprise returns None and
    callers treat the expiry as unknown rather than trusting garbage.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1]
    padded = segment + "=" * (-len(segment) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        exp = claims["exp"]
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return float(exp)


def parse_last_refresh(value: Any) -> float:
    """Parse the ``last_refresh`` ISO timestamp to epoch seconds (0.0 if bad).

    codex-rs writes RFC3339 (e.g. ``2026-08-25T00:00:00Z``); Python 3.10's
    ``fromisoformat`` does not accept ``Z``, so normalize it first.  A naive
    timestamp is interpreted as UTC.  Unparsable input yields 0.0 — the 8-day
    chain gate then simply cannot fire, which must never kill valid creds.
    """
    if not isinstance(value, str) or not value.strip():
        return 0.0
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def format_last_refresh(epoch: float) -> str:
    """Render ``last_refresh`` in the RFC3339 Z shape codex-rs writes."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def bundle_from_auth_json(raw: Any) -> CodexAuthBundle:
    """Validate one parsed auth.json document into a :class:`CodexAuthBundle`.

    Raises :class:`CodexAuthInvalid` when the shape cannot serve a refresh
    flow — most importantly when there is no refresh token (apikey-mode files
    carry none by design).
    """
    if not isinstance(raw, dict):
        raise CodexAuthInvalid("auth.json root is not an object.")
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        raise CodexAuthInvalid("auth.json has no tokens object (apikey mode?).")
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise CodexAuthInvalid("auth.json has no access_token.")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CodexAuthInvalid(
            "auth.json has no refresh_token; run codex login once to bootstrap."
        )
    id_token = tokens.get("id_token")
    account_id = tokens.get("account_id")
    return CodexAuthBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token if isinstance(id_token, str) and id_token else None,
        account_id=account_id if isinstance(account_id, str) and account_id else None,
        last_refresh_epoch=parse_last_refresh(raw.get("last_refresh")),
        expires_at=parse_jwt_exp(access_token),
    )


def bundle_from_token_payload(payload: Any, now: float | None = None) -> CodexAuthBundle:
    """Validate one OAuth token-endpoint success payload into a bundle.

    Used by the initial PKCE mint (``scripts/auth/codex_oauth_login.py``); shape is
    identical to a refresh response (research §2 step 4 / §3) but there is no
    previous bundle to fall back on, so both ``access_token`` and
    ``refresh_token`` are strictly required here.  ``expires_in`` wins over
    the JWT ``exp`` claim when present, mirroring
    :meth:`CodexAuthManager._bundle_from_refresh_response`.
    """
    if not isinstance(payload, dict):
        raise CodexAuthInvalid("token endpoint response was not a JSON object.")
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise CodexAuthInvalid("token endpoint response has no access_token.")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CodexAuthInvalid("token endpoint response has no refresh_token.")
    id_token = payload.get("id_token")
    account_id = payload.get("account_id") or payload.get("chatgpt_account_id")
    moment = time.time() if now is None else now
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expires_at: float | None = moment + float(expires_in)
    else:
        expires_at = parse_jwt_exp(access_token)
    return CodexAuthBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token if isinstance(id_token, str) and id_token else None,
        account_id=account_id if isinstance(account_id, str) and account_id else None,
        last_refresh_epoch=moment,
        expires_at=expires_at,
    )


def merge_bundle_into_raw(raw: dict[str, Any], bundle: CodexAuthBundle, now: float) -> dict[str, Any]:
    """Return a new auth.json document preserving unknown top-level keys.

    Only the fields this module owns are rewritten (``tokens.*``,
    ``last_refresh``, ``auth_mode`` fallback); anything else the codex CLI
    stored alongside (``agent_identity``, ``personal_access_token``, …)
    survives untouched so both programs can share the file.
    """
    merged = dict(raw)
    old_tokens = raw.get("tokens")
    tokens = dict(old_tokens) if isinstance(old_tokens, dict) else {}
    tokens["access_token"] = bundle.access_token
    tokens["refresh_token"] = bundle.refresh_token
    tokens["id_token"] = bundle.id_token
    tokens["account_id"] = bundle.account_id
    merged["tokens"] = tokens
    merged.setdefault("auth_mode", "chatgpt")
    merged["last_refresh"] = format_last_refresh(now)
    return merged


def load_auth_json(path: Path) -> tuple[CodexAuthBundle, dict[str, Any]]:
    """Read + validate auth.json; returns (bundle, raw-document)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CodexAuthInvalid(f"auth.json not found at {path}.") from exc
    except OSError as exc:
        raise CodexAuthInvalid(f"auth.json unreadable at {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise CodexAuthInvalid(f"auth.json at {path} is not valid JSON.") from exc
    return bundle_from_auth_json(raw), raw


def save_auth_json(path: Path, raw: dict[str, Any], bundle: CodexAuthBundle) -> dict[str, Any]:
    """Atomically rewrite auth.json with the rotated bundle, mode 0600.

    Mirrors ``TokenManager._write_disk_cache``: temp file created 0600,
    fsync'd, chmod'd against umask surprises, then swapped via ``os.replace``
    so concurrent readers never observe a partial document.  Returns the
    exact document written so callers can keep their in-memory copy
    consistent without re-merging (and thus re-timestamping).  Callers doing
    a rotation MUST hold the flock first — this function alone does not lock.
    """
    payload = merge_bundle_into_raw(raw, bundle, time.time())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
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
        raise
    return payload


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class CodexAuthManager:
    """Load, refresh (with serialized rotation), and serve the codex token.

    Enabled iff constructed with an explicit ``auth_path`` OR the env flag
    ``WEBGPT_CODEX_AUTH_JSON`` is set; otherwise every call raises
    :class:`CodexAuthDisabled`.  Nothing here is wired into the factory or
    ``TokenManager`` yet — that integration is intentionally deferred.
    """

    def __init__(
        self,
        *,
        auth_path: str | os.PathLike[str] | None = None,
        client_id: str | None = None,
        token_url: str = DEFAULT_TOKEN_URL,
        refresh_skew: float = REFRESH_SKEW_SECONDS,
        chain_ttl: float = REFRESH_CHAIN_TTL_SECONDS,
        http_post: HttpPostFn | None = None,
    ) -> None:
        configured = auth_path if auth_path is not None else os.environ.get(ENV_AUTH_JSON, "")
        text = str(configured).strip()
        # NB: Path("") normalizes to PosixPath("."), whose str() is truthy —
        # the enabled flag must key off the raw string, never off the Path.
        self.path = Path(text) if text else Path("")
        self.enabled = bool(text)
        self.client_id = client_id or os.environ.get(ENV_CLIENT_ID, "") or DEFAULT_CLIENT_ID
        self.token_url = token_url
        self.refresh_skew = refresh_skew
        self.chain_ttl = chain_ttl
        self._http_post: HttpPostFn = http_post or default_http_post
        self._state: CodexAuthBundle | None = None
        self._raw: dict[str, Any] = {}
        # Terminal marker: once set, every call fails fast with no I/O at all.
        self._dead_reason: str | None = None
        # Distrust latch (review round 13): set when the resource server kept
        # rejecting freshly rotated bearers.  Non-terminal — unlike DEAD the
        # grant itself may still be alive — but while latched every
        # ``get_access_token()`` performs a REAL refresh instead of serving
        # any cached snapshot, until ``mark_trusted()`` clears it.
        self._untrusted_reason: str | None = None
        self._async_lock = asyncio.Lock()

    # -- introspection -----------------------------------------------------

    @property
    def dead_reason(self) -> str | None:
        """Human-readable reason when the credential is terminally DEAD."""
        return self._dead_reason

    @property
    def untrusted_reason(self) -> str | None:
        """Reason string while the credential is in the distrust state."""
        return self._untrusted_reason

    @property
    def state(self) -> CodexAuthBundle | None:
        """Current in-memory snapshot, or None before the first load."""
        return self._state

    def invalidate(self) -> None:
        """Drop the cached snapshot so the next call reloads and re-refreshes.

        Intentionally does NOT clear the DEAD mark: a dead grant stays dead
        until the process restarts after a manual ``codex login``.  Note this
        alone never forces a refresh — a still-fresh disk bundle is served
        again unchanged (review round 13); callers that must rotate now use
        ``get_access_token(force_refresh=True)``.
        """
        self._state = None

    def mark_untrusted(self, reason: str) -> None:
        """Latch distrust after the resource server rejected rotated bearers.

        Every later ``get_access_token()`` bypasses all caches and performs
        one real refresh grant until :meth:`mark_trusted` clears the latch.
        A refresh that comes back invalid_grant still latches DEAD terminally
        exactly as before (the DEAD contract is untouched).
        """
        self._untrusted_reason = reason

    def mark_trusted(self) -> None:
        """Clear the distrust latch once a request succeeded end-to-end."""
        self._untrusted_reason = None

    # -- main entry point ----------------------------------------------------

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a currently-valid codex access token, refreshing if needed.

        Freshness margin is ``refresh_skew`` (60s).  Concurrency-safe: the
        asyncio lock collapses concurrent callers onto one refresh, and the
        cross-process flock plus in-lock re-read makes rotation safe even
        with several gateway processes sharing the same auth.json.

        ``force_refresh=True`` (review round 13) skips BOTH the in-memory
        snapshot and the on-disk freshness check for this single call: the
        locked critical section always posts a real refresh grant.  Used by
        the transport's 401 rotation so a server-refused bearer can never be
        replayed just because its JWT ``exp`` is still in the future.  The
        DEAD latch is honoured first regardless of the flag.
        """
        if self._dead_reason is not None:
            raise CodexAuthDead(self._dead_reason)
        if not self.enabled:
            raise CodexAuthDisabled(
                f"{ENV_AUTH_JSON} is not set; codex auth source is OFF."
            )
        force = force_refresh or self._untrusted_reason is not None
        async with self._async_lock:
            if self._dead_reason is not None:  # died while we waited
                raise CodexAuthDead(self._dead_reason)
            state = self._ensure_state()
            if state.is_fresh(skew=self.refresh_skew) and not force:
                return state.access_token
            # Unknown, near expiry, or forced: run the locked critical
            # section (disk flock + blocking HTTP + atomic persist) in a
            # worker thread so the event loop never blocks.
            return await asyncio.to_thread(self._locked_refresh, force)

    # -- internals -------------------------------------------------------------

    def _ensure_state(self) -> CodexAuthBundle:
        """Lazily load auth.json into memory (callers hold the asyncio lock)."""
        if self._state is None:
            self._state, self._raw = load_auth_json(self.path)
        return self._state

    @contextmanager
    def _disk_lock(self) -> Iterator[None]:
        """Exclusive cross-process lock on ``<auth.json>.lock`` (fcntl.flock).

        Without fcntl (non-unix) this degrades to no locking; the atomic
        rename still keeps the file itself consistent, but rotation then
        relies on single-process deployment.
        """
        if fcntl is None:  # pragma: no cover - platform guard
            yield
            return
        lock_path = self.path.with_name(self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _locked_refresh(self, force: bool = False) -> str:
        """Refresh critical section; runs on a worker thread under the flock.

        Re-reads auth.json INSIDE the lock: while we waited on the flock,
        another process may have already rotated the grant — adopting its
        fresher snapshot avoids spending a refresh token that was just
        rotated away (which the server counts as reuse and revokes).

        With ``force=True`` (review round 13) the adopt-fresh early return
        is skipped and a real refresh grant is always posted.  Caveat: in a
        multi-process deployment, forcing right after another process
        rotated can post the just-spent grant and trip reuse detection —
        which latches DEAD here.  That conservative outcome is acceptable:
        force is only used for credentials the resource server is already
        refusing.
        """
        with self._disk_lock():
            bundle, raw = load_auth_json(self.path)
            self._raw = raw
            if not force and bundle.is_fresh(skew=self.refresh_skew):
                self._state = bundle
                return bundle.access_token
            now = time.time()
            if bundle.last_refresh_epoch and now - bundle.last_refresh_epoch > self.chain_ttl:
                reason = (
                    "codex refresh chain idle for more than "
                    f"{self.chain_ttl / 86400:.0f} days; re-login required"
                )
                self._mark_dead(reason)
                raise CodexAuthDead(reason)

            status, payload = self._post_refresh(bundle.refresh_token)

            if status in _DEAD_REFRESH_STATUSES:
                code = payload.get("error") if isinstance(payload, dict) else None
                reason = (
                    f"codex refresh rejected with HTTP {status}"
                    f" ({code or 'no-error-field'}); credential is DEAD —"
                    " run codex login to obtain a new grant"
                )
                self._mark_dead(reason)
                raise CodexAuthDead(reason)
            if status != 200:
                # 429 / 5xx / odd codes: the grant may still be alive; retrying
                # later is meaningful.  Never retried inside this class.
                raise CodexAuthTransient(
                    f"codex refresh endpoint returned HTTP {status}."
                )
            new_bundle = self._bundle_from_refresh_response(bundle, payload, now)
            # Persist BEFORE releasing the flock so no other process can read
            # the pre-rotation refresh token after we spent it.  Adopting the
            # returned document keeps memory byte-identical to the file.
            self._raw = save_auth_json(self.path, self._raw, new_bundle)
            self._state = new_bundle
            return new_bundle.access_token

    def _post_refresh(self, refresh_token: str) -> tuple[int, Any]:
        """Blocking refresh POST (JSON body per research §3)."""
        try:
            return self._http_post(
                self.token_url,
                {
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
        except CodexAuthError:
            raise
        except Exception as exc:  # network/DNS/TLS — transient by definition
            raise CodexAuthTransient(f"codex refresh transport failed: {exc}") from exc

    def _bundle_from_refresh_response(
        self, current: CodexAuthBundle, payload: Any, now: float
    ) -> CodexAuthBundle:
        """Fold a 200 refresh response into the next bundle snapshot."""
        if not isinstance(payload, dict):
            raise CodexAuthTransient("codex refresh response was not a JSON object.")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise CodexAuthTransient("codex refresh response has no access_token.")
        new_refresh = payload.get("refresh_token")
        expires_at: float | None
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
            expires_at = now + float(expires_in)
        else:
            # JWT exp is authoritative in codex-rs; unknown expiry means the
            # next get_access_token simply refreshes again (safe, never fatal).
            expires_at = parse_jwt_exp(access_token)
        id_token = payload.get("id_token")
        account_id = payload.get("account_id") or payload.get("chatgpt_account_id")
        return replace(
            current,
            access_token=access_token,
            refresh_token=(
                new_refresh if isinstance(new_refresh, str) and new_refresh
                else current.refresh_token
            ),
            id_token=(
                id_token if isinstance(id_token, str) and id_token else current.id_token
            ),
            account_id=(
                account_id if isinstance(account_id, str) and account_id else current.account_id
            ),
            last_refresh_epoch=now,
            expires_at=expires_at,
        )

    def _mark_dead(self, reason: str) -> None:
        """Latch the terminal DEAD state (fast-fail for every later call)."""
        self._dead_reason = reason
