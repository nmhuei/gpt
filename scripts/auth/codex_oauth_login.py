#!/usr/bin/env python3
"""One-shot helper for the OWNER to mint the first codex OAuth grant.

Implements the manual PKCE flow researched in
``docs/reports/codex-oauth-research-2026-08-25.md`` §2:

1. The script generates a PKCE ``code_verifier`` / S256 ``code_challenge``
   and prints the full authorize URL.
2. The OWNER opens that URL in a normal browser already logged into ChatGPT
   (this script NEVER opens a browser, NEVER runs headless automation).
3. After approving, the browser is redirected to
   ``http://localhost:1455/auth/callback?code=...&state=...`` — because no
   local server is bound, the page shows a connection error: that is EXPECTED.
   The OWNER copies the whole URL from the address bar and pastes it back.
4. The script exchanges the authorization code (form POST to the token
   endpoint), validates the response shape via
   :func:`gpt.transport.codex_auth.bundle_from_token_payload`, and writes
   auth.json atomically (mode 0600) through
   :func:`gpt.transport.codex_auth.save_auth_json` — schema-compatible with
   both this repo's ``CodexAuthManager`` and the real codex CLI.

Safety properties:
- argparse + ``__main__`` guard; ``--help`` performs zero side effects
  (FAILURES 2026-08-25 lesson).
- An existing auth.json is never clobbered without an explicit ``--force``.
- Tokens are never printed — only file path, mode, and expiry metadata.
- All errors are clean one-line messages; raw tracebacks never reach the user.

Exit codes: 0 success · 1 clean error · 130 interrupted · 2 argparse usage.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

try:  # Normal when run via .venv/bin/python (editable install).
    from gpt.transport.codex_auth import (
        DEFAULT_CLIENT_ID,
        DEFAULT_TOKEN_URL,
        ENV_AUTH_JSON,
        ENV_CLIENT_ID,
        CodexAuthError,
        bundle_from_token_payload,
        save_auth_json,
    )
except ImportError:  # Direct `python scripts/...` from a checkout without install.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from gpt.transport.codex_auth import (
        DEFAULT_CLIENT_ID,
        DEFAULT_TOKEN_URL,
        ENV_AUTH_JSON,
        ENV_CLIENT_ID,
        CodexAuthError,
        bundle_from_token_payload,
        save_auth_json,
    )

DEFAULT_ISSUER = "https://auth.openai.com"
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
# Exact scope list of codex-rs (research §2 step 3) — offline_access is what
# yields the refresh token we persist.
DEFAULT_SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
EXCHANGE_TIMEOUT_S = 30.0

HttpPostFn = Callable[[str, dict[str, str]], tuple[int, Any]]
EchoFn = Callable[[str], None]
InputFn = Callable[[str], str]


class MintError(RuntimeError):
    """Clean, user-facing failure (never a raw traceback)."""


# ---------------------------------------------------------------------------
# PKCE + authorize URL
# ---------------------------------------------------------------------------


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` per research §2 step 1.

    verifier = 64 random bytes, base64url unpadded (86 chars ≥ RFC 7636's
    43-char minimum); challenge = base64url(SHA256(verifier)) — method S256.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def generate_state() -> str:
    """32 random bytes base64url, same entropy shape as codex-rs."""
    return secrets.token_urlsafe(32)


def build_authorize_url(
    *,
    issuer: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    code_challenge: str,
    state: str,
) -> str:
    """Assemble the authorize URL exactly as codex-rs does (research §2)."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": "codex_cli_rs",
    }
    return f"{issuer.rstrip('/')}/oauth/authorize?{urlencode(params, quote_via=quote)}"


def extract_code_and_state(pasted: str) -> tuple[str, str | None]:
    """Pull ``(code, state)`` out of whatever the owner pasted back.

    Accepted shapes: a full callback URL, a bare query string
    (``code=...&state=...``), or just the raw authorization code.
    """
    text = pasted.strip().strip("'\"")
    if not text:
        raise MintError(
            "Nothing pasted — expected the callback URL or at least the "
            "'code' parameter."
        )
    state: str | None = None
    if "://" in text:
        query = urlparse(text).query
    elif "code=" in text or "state=" in text:
        query = text
    else:
        if any(ch.isspace() for ch in text):
            raise MintError(f"Pasted value {text!r} is neither a URL nor a bare code.")
        return text, None
    fields = parse_qs(query, keep_blank_values=True)
    codes = fields.get("code", [])
    if not codes or not codes[0].strip():
        raise MintError(
            "No 'code' parameter found in the pasted URL — copy the FULL "
            "address-bar URL after the redirect."
        )
    states = fields.get("state", [])
    if states and states[0].strip():
        state = states[0].strip()
    return codes[0].strip(), state


# ---------------------------------------------------------------------------
# Token exchange (form-urlencoded per research §2 step 4)
# ---------------------------------------------------------------------------


def default_exchange_post(url: str, form: dict[str, str]) -> tuple[int, Any]:
    """Blocking form POST to the token endpoint (stdlib only).

    Returns ``(status, parsed-json-or-raw-text)``; HTTP error statuses are
    returned rather than raised so the caller can render a clean message.
    """
    data = urlencode(form).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=EXCHANGE_TIMEOUT_S) as response:
            status = int(response.status)
            body = response.read().decode("utf-8", "replace")
    except HTTPError as exc:  # 4xx/5xx carry the OAuth error payload.
        status = int(exc.code)
        body = exc.read().decode("utf-8", "replace")
    try:
        payload: Any = json.loads(body)
    except ValueError:
        payload = body
    return status, payload


def exchange_token(
    *,
    token_url: str,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    post_fn: HttpPostFn | None = None,
) -> dict[str, Any]:
    """Run the authorization_code grant; returns the parsed success JSON."""
    post = post_fn or default_exchange_post
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    try:
        status, payload = post(token_url, form)
    except Exception as exc:  # DNS/TLS/timeout — keep it human-readable.
        raise MintError(f"Token endpoint unreachable ({token_url}): {exc}") from exc
    if status != 200:
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("error") or payload.get("error_description") or "")
        elif isinstance(payload, str):
            detail = payload[:200]
        raise MintError(
            f"Token exchange failed with HTTP {status}"
            + (f" ({detail})" if detail else "")
            + ". The code may be single-use/expired — restart the flow."
        )
    if not isinstance(payload, dict):
        raise MintError("Token endpoint returned a non-JSON 200 body.")
    return payload


# ---------------------------------------------------------------------------
# Mint driver
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex_oauth_login.py",
        description=(
            "Mint the first codex auth.json via a manual PKCE flow: this tool "
            "prints an authorize URL, you approve it in your own browser and "
            "paste the redirected callback URL back. It never opens a browser "
            "and never runs headless automation."
        ),
    )
    parser.add_argument(
        "--auth-json",
        default=None,
        metavar="PATH",
        help=(
            "Where to write auth.json "
            f"(default: ${ENV_AUTH_JSON} if set, otherwise ~/.codex/auth.json)."
        ),
    )
    parser.add_argument(
        "--client-id",
        default=None,
        metavar="ID",
        help=(
            "OAuth client id (default: ${ENV_CLIENT_ID} if set, otherwise "
            f"{DEFAULT_CLIENT_ID})."
        ),
    )
    parser.add_argument("--issuer", default=DEFAULT_ISSUER, help="OAuth issuer base URL.")
    parser.add_argument(
        "--token-url",
        default=DEFAULT_TOKEN_URL,
        help="Token endpoint posted for the exchange.",
    )
    parser.add_argument(
        "--redirect-uri",
        default=DEFAULT_REDIRECT_URI,
        help=(
            "Must match codex-rs' registered callback. No local port is bound: "
            "the browser will show a connection-refused page whose URL you paste back."
        ),
    )
    parser.add_argument("--scope", default=DEFAULT_SCOPE, help="Space-separated scopes.")
    parser.add_argument(
        "--code",
        default=None,
        metavar="CODE_OR_URL",
        help="Skip the interactive prompt: supply the callback URL / code directly.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing auth.json (a live grant is rotated away).",
    )
    return parser


def _resolve_auth_path(cli_value: str | None) -> Path:
    text = cli_value or os.environ.get(ENV_AUTH_JSON, "").strip()
    return Path(text).expanduser() if text else Path.home() / ".codex" / "auth.json"


def run_mint(
    args: argparse.Namespace,
    *,
    post_fn: HttpPostFn | None = None,
    input_fn: InputFn = input,
    echo: EchoFn = print,
) -> Path:
    """Interactive mint flow; returns the written auth.json path."""
    auth_path = _resolve_auth_path(args.auth_json)
    if auth_path.exists() and not args.force:
        raise MintError(
            f"{auth_path} already exists — refusing to clobber a possibly live "
            "grant. Re-run with --force to overwrite."
        )

    client_id = (
        args.client_id
        or os.environ.get(ENV_CLIENT_ID, "").strip()
        or DEFAULT_CLIENT_ID
    )

    code_verifier, code_challenge = generate_pkce()
    state = generate_state()
    authorize_url = build_authorize_url(
        issuer=args.issuer,
        client_id=client_id,
        redirect_uri=args.redirect_uri,
        scope=args.scope,
        code_challenge=code_challenge,
        state=state,
    )

    echo("")
    echo("Step 1/3 — open this URL in your browser (logged into ChatGPT):")
    echo(authorize_url)
    echo("")
    echo("Step 2/3 — approve access. The browser then lands on")
    echo(f"    {args.redirect_uri}?code=...&state=...")
    echo("  which shows a connection error: EXPECTED (no local server). Copy the")
    echo("  FULL URL from the address bar and paste it below.")

    pasted = args.code
    if pasted is None:
        try:
            pasted = input_fn("Step 3/3 — paste callback URL (or code) > ")
        except EOFError as exc:
            raise MintError("No input available — use --code to run non-interactively.") from exc

    code, pasted_state = extract_code_and_state(pasted)
    if pasted_state is not None and pasted_state != state:
        raise MintError(
            "state mismatch between the printed authorize URL and the pasted "
            "callback — possible wrong window or tampering; restart the flow."
        )

    payload = exchange_token(
        token_url=args.token_url,
        client_id=client_id,
        code=code,
        redirect_uri=args.redirect_uri,
        code_verifier=code_verifier,
        post_fn=post_fn,
    )
    try:
        bundle = bundle_from_token_payload(payload)
    except CodexAuthError as exc:
        raise MintError(f"Token response unusable: {exc}") from exc

    # Seed the document with codex-rs AuthDotJson's nullable fields so the
    # file matches research §1 byte-shape; unknown keys survive later rotations.
    seed_raw: dict[str, Any] = {
        "OPENAI_API_KEY": None,
        "agent_identity": None,
        "personal_access_token": None,
    }
    # save_auth_json RETURNS the full document (tokens included!) — never
    # echo it; print only the path.
    save_auth_json(auth_path, seed_raw, bundle)

    echo("")
    echo(f"Wrote {auth_path} (mode 0600, atomic rename).")
    echo(f"auth_mode=chatgpt  account_id={bundle.account_id or '(none in response)'}")
    echo(
        "Access-token expiry: "
        + ("unknown" if bundle.expires_at is None else f"epoch {int(bundle.expires_at)}")
        + "; refresh rotates automatically under WEBGPT_CODEX_AUTH_JSON."
    )
    echo("Never print, commit, or share auth.json — it IS the account credential.")
    return auth_path


def main(argv: list[str] | None = None, **hooks: Any) -> int:
    """CLI entry point; returns a process exit code (0 ok, 1 error, 130 ^C)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run_mint(args, **hooks)
    except KeyboardInterrupt:
        print("\nAborted — nothing was written.", file=sys.stderr)
        return 130
    except (MintError, CodexAuthError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
