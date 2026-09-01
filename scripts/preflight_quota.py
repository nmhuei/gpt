"""QUOTA-PREFLIGHT (ROADMAP row M): pre-batch quota gate CLI.

Reads ``GET {backend}/wham/usage`` with a bearer taken from an existing
TokenBundle source and prints one JSON summary line to stdout, then exits:

  0  OK        primary used_percent < 70 AND secondary < 50  -> batch may open
  2  DEFER     primary >= 70 OR secondary >= 50             -> dời batch,
               coordinator reschedules (thresholds: research §C3/§D)
  3  UNKNOWN   no bearer found, transport error, auth rejection (401/403),
               any other non-200 status, or an unparseable payload — quota
               could not be determined; the coordinator decides manually.

ENDPOINT NOT LIVE-VERIFIED (as of 2026-08-26): the URL and payload shape come
from OpenAI's own codex source (rate_limit_status_details.rs /
rate_limit_window_snapshot.rs) via
docs/reports/quota-pattern-research-2026-08-26.md §A1 — never against this
repo's own live account.  If a REAL run returns 404/401, report it back so
the path can be corrected deliberately; do NOT guess alternative paths in
this script.  The only sanctioned override is ``--url`` / ``WEBGPT_USAGE_URL``
after a coordinator decision (research §A2 names the candidate sibling:
``https://chatgpt.com/backend-api/codex/usage``).

Bearer sources (first match wins):

1. ``--token VALUE`` — explicit bearer, highest priority.
2. TokenBundle disk cache written by :class:`gpt.transport.token_manager.TokenManager`
   next to the browser profile (``<profile_dir>/webgpt-token-cache.json``,
   version-1 shape).  Read-only import of ``TokenBundle`` for typing; the
   cache-file reader is re-implemented here so token_manager.py stays
   untouched.  The cache is trusted only while younger than
   ``--max-token-age`` seconds (default 1800 = the manager's
   refresh_interval); stale/corrupt caches are ignored.

This script NEVER opens a browser, NEVER logs in, and performs exactly one
HTTP GET per run.  Known limitation (research §E): a web-session bearer may
be rejected by the codex backend (401) — that surfaces as exit 3, not as a
crash; the OAuth alternative lives in ``gpt.transport.codex_auth`` and is
deliberately out of scope here until the coordinator wires it.

FAILURES lesson applied: everything runs behind argparse + a main-guard, so
``--help`` (and plain import) has zero side effects and zero network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any

# Make the repo root importable whether run as ``python scripts/…`` or via an
# absolute path from cron/systemd (scripts/ is sys.path[0], not the repo).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gpt.transport.token_manager import TokenBundle  # noqa: E402

EXIT_OK = 0
EXIT_DEFER = 2
EXIT_UNKNOWN = 3

# Research §C3/§D: primary >=70% or secondary (weekly) >=50% => defer batch.
PRIMARY_BLOCK_PERCENT = 70.0
SECONDARY_BLOCK_PERCENT = 50.0

USAGE_URL_ENV = "WEBGPT_USAGE_URL"
PROFILE_DIR_ENV = "PROFILE_DIR"  # same key gpt.config.settings uses.
DEFAULT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def default_profile_dir() -> Path:
    """Anonymous CloakBrowser profile dir under the XDG runtime root.

    Mirrors ``gpt.config.settings.DEFAULT_PROFILE_DIR``: ``$WEBGPT_RUNTIME_ROOT``
    wins when exported, else ``~/.local/share/webgpt/cloak-profile``.  Resolved
    lazily (not at import) so a late env change still applies.
    """
    root = os.environ.get("WEBGPT_RUNTIME_ROOT", "").strip()
    base = Path(root) if root else Path.home() / ".local" / "share" / "webgpt"
    return base / "cloak-profile"

# Mirrors TokenManager's private disk-cache constant/version (read-only
# replication — token_manager.py must not be modified for this script).
TOKEN_CACHE_FILENAME = "webgpt-token-cache.json"
_TOKEN_CACHE_VERSION = 1
_DEFAULT_MAX_TOKEN_AGE_SECONDS = 1800.0  # == TokenManager.refresh_interval

_REQUEST_TIMEOUT_SECONDS = 15.0

try:  # Same guarded optional-dependency style as usage_poller.py.
    from curl_cffi.requests import Session as _CurlSyncSession
except ImportError:  # pragma: no cover - optional dependency
    _CurlSyncSession = None  # type: ignore[assignment,misc]

# Injectable blocking GET: (url, headers) -> (status, parsed-json-or-None).
# Tests inject fakes so nothing here ever touches the network.
HttpGetFn = Callable[[str, dict[str, str]], tuple[int, Any]]


def default_http_get(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    """Blocking usage GET via curl_cffi (single request, then done)."""
    if _CurlSyncSession is None:
        raise RuntimeError("curl_cffi is not installed; cannot read usage.")
    with _CurlSyncSession() as session:
        response = session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    return int(response.status_code), payload


def _optional_number(raw: Any) -> float | None:
    """Numeric-or-None for optional snapshot fields (bools excluded)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def summarize_payload(payload: Any) -> dict[str, Any] | None:
    """Defensive summary of both rate-limit windows; None when unusable.

    Mirrors the canonical codex shape (RateLimitStatusDetails): each window
    becomes ``{used_percent, limit_window_seconds, reset_at}`` with numeric
    fields None when absent/garbled.  A root without ``rate_limit`` at all is
    unparseable (None); ``primary_window``/``secondary_window`` being null is
    representable (window dict set to None) because some accounts genuinely
    lack a window tier (research §B8).
    """
    if not isinstance(payload, dict):
        return None
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None

    def window_snapshot(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        used_percent = _optional_number(raw.get("used_percent"))
        return {
            # Clamp so a garbled percent can never distort the gate decision.
            "used_percent": (
                None if used_percent is None else max(0.0, min(100.0, used_percent))
            ),
            "limit_window_seconds": _optional_number(raw.get("limit_window_seconds")),
            "reset_at": _optional_number(raw.get("reset_at")),
        }

    return {
        "primary": window_snapshot(rate_limit.get("primary_window")),
        "secondary": window_snapshot(rate_limit.get("secondary_window")),
    }


def decide_blocked(summary: dict[str, Any]) -> bool | None:
    """Gate decision from a summary; None when nothing measurable was seen.

    Blocking rule: ``primary.used_percent >= PRIMARY_BLOCK_PERCENT`` OR
    ``secondary.used_percent >= SECONDARY_BLOCK_PERCENT``.  Windows that are
    null/unmeasurable simply do not vote; when NEITHER window carries a
    usable percent the quota state is unknown (caller maps that to exit 3).
    """
    saw_any_percent = False
    blocked = False
    primary, secondary = summary["primary"], summary["secondary"]
    checks = (
        (primary, PRIMARY_BLOCK_PERCENT),
        (secondary, SECONDARY_BLOCK_PERCENT),
    )
    for window, threshold in checks:
        if window is None or window.get("used_percent") is None:
            continue
        saw_any_percent = True
        if float(window["used_percent"]) >= threshold:
            blocked = True
    return blocked if saw_any_percent else None


def load_cached_token_bundle(
    profile_dir: Path | str,
    *,
    max_age_seconds: float,
    wall_clock: Callable[[], float] = time.time,
) -> TokenBundle | None:
    """Read a still-fresh TokenBundle from the TokenManager disk cache.

    Replicates (read-only) the version-1 cache shape written by
    ``TokenManager._write_disk_cache``: JSON with ``version``, ``stored_at``
    (unix), ``access_token``, ``cookies`` (str->str), optional
    ``cf_clearance`` / ``oai_device_id``.  Missing/stale/corrupt/malformed
    caches return None silently — the caller then reports "unknown".
    """
    path = Path(profile_dir) / TOKEN_CACHE_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != _TOKEN_CACHE_VERSION:
        return None
    stored_at = raw.get("stored_at")
    if isinstance(stored_at, bool) or not isinstance(stored_at, (int, float)):
        return None
    age = wall_clock() - float(stored_at)
    if age < 0 or age >= max_age_seconds:
        return None
    access_token = raw.get("access_token")
    cookies_raw = raw.get("cookies")
    if not isinstance(access_token, str) or not access_token:
        return None
    if not isinstance(cookies_raw, dict) or not all(
        isinstance(name, str) and isinstance(value, str)
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


def run_preflight(
    *,
    url: str,
    token: str | None = None,
    profile_dir: Path | str | None = None,
    account_id: str | None = None,
    user_agent: str = "",
    max_token_age: float = _DEFAULT_MAX_TOKEN_AGE_SECONDS,
    http_get: HttpGetFn = default_http_get,
    wall_clock: Callable[[], float] = time.time,
) -> tuple[int, dict[str, Any]]:
    """One full check: resolve bearer, fetch once, decide, summarize.

    Returns ``(exit_code, summary_dict)``; the summary is always JSON-safe so
    callers (and ``main``) can print it verbatim.  Never raises for expected
    failure modes — those are exit code 3.  Uniform contract: ``blocked`` is
    present in every outcome — None until a decision is actually possible.
    """
    summary: dict[str, Any] = {"url": url, "blocked": None}
    bearer = token
    bearer_source = "cli"
    if not bearer and profile_dir is not None:
        bundle = load_cached_token_bundle(
            profile_dir, max_age_seconds=max_token_age, wall_clock=wall_clock
        )
        if bundle is not None:
            bearer = bundle.access_token
            bearer_source = "token-cache"
            if account_id is None and bundle.chatgpt_account_id:
                account_id = bundle.chatgpt_account_id
    if not bearer:
        summary.update({"error": "no_bearer", "bearer_source": None})
        return EXIT_UNKNOWN, summary
    summary["bearer_source"] = bearer_source

    headers = {
        "authorization": f"Bearer {bearer}",
        "accept": "application/json",
    }
    if account_id:
        # Header name per codex-auth docs (research §A1 sources 4/5).
        headers["chatgpt-account-id"] = account_id
    if user_agent:
        headers["user-agent"] = user_agent

    try:
        status, payload = http_get(url, headers)
    except Exception as error:  # transport/DNS/TLS — undetermined, not blocked
        summary.update({"error": "transport", "detail": str(error)})
        return EXIT_UNKNOWN, summary
    summary["http_status"] = int(status)

    if status in (401, 403):
        # Credential rejected by the usage backend.  NOT proof of low quota;
        # report back rather than guessing another endpoint (see docstring).
        summary.update({"error": "auth_rejected"})
        return EXIT_UNKNOWN, summary
    if status != 200:
        # Includes 404: wrong/unlive path — coordinator decides the fix.
        summary.update({"error": "http_status"})
        return EXIT_UNKNOWN, summary

    parsed = summarize_payload(payload)
    if parsed is None:
        summary.update({"error": "unparseable_payload"})
        return EXIT_UNKNOWN, summary
    summary.update(parsed)

    blocked = decide_blocked(parsed)
    if blocked is None:
        summary.update({"error": "no_measurable_window"})
        return EXIT_UNKNOWN, summary

    summary["blocked"] = blocked
    summary["thresholds"] = {
        "primary_block_percent": PRIMARY_BLOCK_PERCENT,
        "secondary_block_percent": SECONDARY_BLOCK_PERCENT,
    }
    return (EXIT_DEFER if blocked else EXIT_OK), summary


def build_parser() -> argparse.ArgumentParser:
    """CLI surface.  Parsing has no side effects (FAILURES lesson)."""
    parser = argparse.ArgumentParser(
        prog="preflight_quota",
        description=(
            "QUOTA-PREFLIGHT: gate a batch on ChatGPT Web quota "
            f"(exit {EXIT_OK}=ok, {EXIT_DEFER}=defer batch, "
            f"{EXIT_UNKNOWN}=unknown). Prints one JSON summary."
        ),
    )
    parser.add_argument(
        "--url",
        default=os.environ.get(USAGE_URL_ENV, "").strip() or DEFAULT_USAGE_URL,
        help=f"Usage endpoint (env {USAGE_URL_ENV}; default wham/usage).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Explicit bearer access token (wins over the disk cache).",
    )
    parser.add_argument(
        "--profile-dir",
        default=None,
        help=(
            "Browser profile dir holding webgpt-token-cache.json "
            f"(env {PROFILE_DIR_ENV}; default {default_profile_dir()})."
        ),
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help="Optional ChatGPT-Account-ID header value.",
    )
    parser.add_argument(
        "--user-agent",
        default="",
        help="Optional User-Agent header override (empty: omit).",
    )
    parser.add_argument(
        "--max-token-age",
        type=float,
        default=_DEFAULT_MAX_TOKEN_AGE_SECONDS,
        help="Max age (s) of the cached TokenBundle (default 1800).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code (see module docstring)."""
    args = build_parser().parse_args(argv)
    profile_dir = args.profile_dir
    if profile_dir is None:
        env_profile = os.environ.get(PROFILE_DIR_ENV, "").strip()
        profile_dir = env_profile or str(default_profile_dir())
    code, summary = run_preflight(
        url=args.url,
        token=args.token,
        profile_dir=profile_dir,
        account_id=args.account_id,
        user_agent=args.user_agent,
        max_token_age=args.max_token_age,
        http_get=default_http_get,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
