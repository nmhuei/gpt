"""Central, read-only Cloudflare / Turnstile challenge detection.

Strategy (owner-confirmed): prevention via CloakBrowser fingerprint consistency
plus early detection and clean stop.  This module NEVER clicks, solves, or
otherwise bypasses a challenge — it only classifies what a page or an HTTP
response is showing so callers can stop cleanly and report instead of crashing
blindly or retrying into a wall.
"""

from __future__ import annotations

import enum
import json
import logging
from typing import Any

from gpt.state import ChatGPTWebError

logger = logging.getLogger(__name__)


class ChallengeKind(str, enum.Enum):
    """Classification of an anti-bot interstitial."""

    NONE = "none"
    CLOUDFLARE_CHALLENGE = "cloudflare_challenge"
    TURNSTILE = "turnstile"


class ChallengeDetectedError(ChatGPTWebError):
    """A security challenge is blocking the current path.

    Raised instead of a blind crash/retry once a challenge page is detected.
    Carries structured context so upper layers can stop cleanly and report
    (the only valid recovery is minting fresh clearance in the real browser).
    """

    def __init__(
        self,
        message: str,
        *,
        kind: ChallengeKind = ChallengeKind.CLOUDFLARE_CHALLENGE,
        url: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.url = url
        self.status_code = status_code


# Turnstile widget markers (checked first: a Turnstile widget may also appear
# inside a full Cloudflare challenge page).
_TURNSTILE_SELECTORS = (
    "#cf-turnstile",
    "[data-turnstile-wrapper]",
    "div.cf-turnstile",
)

# Full-page Cloudflare challenge markers.
_CHALLENGE_SELECTORS = (
    'iframe[src*="challenges.cloudflare.com"]',
    "#challenge-stage",
    "#cf-challenge-running",
    '[id^="cf-chl-"]',
)

# Title markers are matched case-insensitively against page.title().
_TITLE_MARKERS = (
    "just a moment",
    "verify you are human",
    "attention required",
)

# Visible body-text markers of an interstitial (casefolded before matching).
_BODY_MARKERS = (
    "verify you are human",
    "performing security verification",
    "checking your browser",
    "needs to review the security of your connection",
)

# Raw-HTML markers used for HTTP response bodies where there is no DOM.
_HTML_MARKERS = (
    "just a moment",
    "challenge-platform",
    "cf-browser-verification",
    "cf_chl_",
    "verify you are human",
    "attention required",
)

_TURNSTILE_HTML_MARKER = "cf-turnstile"


async def detect_challenge(page: Any) -> ChallengeKind:
    """Classify whether ``page`` currently shows a security challenge.

    Accepts any Playwright-like page object (real Playwright page or test
    double exposing ``locator``/``title``/``inner_text``).  Detection is
    strictly read-only; every probe failure degrades to "not detected" so a
    transient DOM error can never be mistaken for a challenge.
    """
    try:
        if await page.locator(", ".join(_TURNSTILE_SELECTORS)).count() > 0:
            return ChallengeKind.TURNSTILE
        if await page.locator(", ".join(_CHALLENGE_SELECTORS)).count() > 0:
            return ChallengeKind.CLOUDFLARE_CHALLENGE
    except Exception as exc:
        logger.debug("challenge selector probe failed: %s", exc)

    try:
        title = str(await page.title()).casefold()
        if any(marker in title for marker in _TITLE_MARKERS):
            return ChallengeKind.CLOUDFLARE_CHALLENGE
    except Exception as exc:
        logger.debug("challenge title probe failed: %s", exc)

    try:
        body_text = str(
            await page.locator("body").inner_text(timeout=1000)
        ).casefold()
    except Exception:
        return ChallengeKind.NONE
    if any(marker in body_text for marker in _BODY_MARKERS):
        return ChallengeKind.CLOUDFLARE_CHALLENGE
    return ChallengeKind.NONE


class LimitSignal(str, enum.Enum):
    """Pre-breaker taxonomy of a failed HTTP response (LIMIT-SIGNATURE-TAXONOMY).

    The global rate-limit breaker must only ever be fed a genuine quota
    verdict.  A Cloudflare interstitial (403/503 — occasionally 429) wrapped
    in HTML looks superficially similar to a 429 but means something entirely
    different, and an ambiguous failure proves nothing at all.
    """

    NONE = "none"  # status < 400: nothing to classify
    PURE_RATE_LIMIT = "pure_rate_limit"  # bare 429 or JSON with a rate-limit signature
    CHALLENGE = "challenge"  # HTML/challenge markers -> challenge recovery, never the breaker
    UNDETERMINED = "undetermined"  # anything else -> must NOT trip the breaker


# Structured JSON signatures accepted as an explicit quota verdict (checked in
# addition to the status leg).  Keys are matched case-insensitively; value
# markers require one of these substrings inside a JSON string so free-text
# error prose can never accidentally qualify unless it names the limit itself.
_RATE_LIMIT_JSON_KEYS = frozenset({"rate_limit", "ratelimit", "rate-limit"})
_RATE_LIMIT_JSON_VALUE_MARKERS = (
    "rate_limit",
    "rate limit",
    "too_many_requests",
    "usage_limit",
)
_JSON_SCAN_DEPTH = 4


def has_rate_limit_json_signature(snippet: str | None) -> bool:
    """Return True when ``snippet`` parses as JSON carrying a quota signature.

    Defensive by design: non-JSON input, malformed JSON, or deeply nested
    payloads all degrade to False — only an unmistakable rate-limit payload
    qualifies.
    """
    stripped = (snippet or "").strip()
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        payload = json.loads(stripped)
    except Exception:
        return False

    def scan(node: Any, depth: int) -> bool:
        if depth > _JSON_SCAN_DEPTH:
            return False
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.strip().casefold() in _RATE_LIMIT_JSON_KEYS:
                    return True
                if scan(value, depth + 1):
                    return True
            return False
        if isinstance(node, list):
            return any(scan(item, depth + 1) for item in node)
        if isinstance(node, str):
            folded = node.casefold()
            return any(marker in folded for marker in _RATE_LIMIT_JSON_VALUE_MARKERS)
        return False

    return scan(payload, 0)


def classify_limit_signal(status_code: int | None, body_snippet: str | None) -> LimitSignal:
    """Decide what a failed HTTP response actually is, BEFORE breaker feeding.

    Precedence:
    1. Challenge/HTML markers on any >=400 status -> ``CHALLENGE`` (an
       interstitial is never a quota verdict, even on a 429 envelope).
    2. A parseable JSON body with an explicit rate-limit signature ->
       ``PURE_RATE_LIMIT``.
    3. A bare status-429 without challenge markers -> ``PURE_RATE_LIMIT``
       ("429 thuần").
    4. Everything else (plain 403/503, empty/unreadable body, unknown shape)
       -> ``UNDETERMINED``: callers keep their legacy paths and the breaker
       stays untouched.
    """
    if status_code is None or status_code < 400:
        return LimitSignal.NONE
    snippet = (body_snippet or "").casefold()
    if classify_http_challenge(status_code, snippet) is not ChallengeKind.NONE:
        return LimitSignal.CHALLENGE
    if has_rate_limit_json_signature(body_snippet):
        return LimitSignal.PURE_RATE_LIMIT
    if status_code == 429:
        return LimitSignal.PURE_RATE_LIMIT
    return LimitSignal.UNDETERMINED


def classify_http_challenge(status_code: int, body_snippet: str | None) -> ChallengeKind:
    """Classify an HTTP response as a Cloudflare challenge interstitial.

    Challenge pages arrive with 403/503 (occasionally 429) and an HTML body
    containing Cloudflare's markers ("Just a moment...", challenge-platform
    scripts).  A bare error status without those markers is NOT classified as
    a challenge — that keeps normal API errors on their existing paths.
    """
    if status_code < 400:
        return ChallengeKind.NONE
    snippet = (body_snippet or "").casefold()
    if not snippet:
        return ChallengeKind.NONE
    if _TURNSTILE_HTML_MARKER in snippet:
        return ChallengeKind.TURNSTILE
    if any(marker in snippet for marker in _HTML_MARKERS):
        return ChallengeKind.CLOUDFLARE_CHALLENGE
    return ChallengeKind.NONE


__all__ = [
    "ChallengeDetectedError",
    "ChallengeKind",
    "LimitSignal",
    "classify_http_challenge",
    "classify_limit_signal",
    "detect_challenge",
    "has_rate_limit_json_signature",
]
