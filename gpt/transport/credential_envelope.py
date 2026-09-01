from __future__ import annotations

import os
from typing import Any

from gpt.state import AuthRequired
from gpt.transport.token_manager import SentinelTokens

CLOAKBROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
CF_UA_ENV = "WEBGPT_CF_USER_AGENT"
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_VERSION = "0.149.1"
CODEX_OPENAI_BETA = "responses=experimental"
CF_CLEARANCE_COOKIE = "cf_clearance"
OAI_CLIENT_VERSION_ENV = "WEBGPT_OAI_CLIENT_VERSION"
OAI_CLIENT_BUILD_ENV = "WEBGPT_OAI_CLIENT_BUILD_NUMBER"


def envelope_user_agent() -> str:
    """User-Agent shared by headers and the Sentinel/PoW fingerprint."""
    return os.environ.get(CF_UA_ENV, "").strip() or CLOAKBROWSER_USER_AGENT


def build_headers(
    bundle: Any,
    sentinel: SentinelTokens,
    *,
    codex: bool = False,
    session_id: str | None = None,
    fconv: bool = False,
    turn_session_id: str | None = None,
    turn_trace_id: str | None = None,
    conduit_token: str | None = None,
    bearer_token: str | None = None,
) -> dict[str, str]:
    """Build the browser credential envelope for direct SSE requests."""
    cookies = dict(bundle.cookies)
    if bundle.cf_clearance:
        cookies[CF_CLEARANCE_COOKIE] = bundle.cf_clearance
    access_token = bearer_token if bearer_token is not None else bundle.access_token

    missing: list[str] = []
    if not access_token:
        missing.append("Authorization")
    if not cookies.get(CF_CLEARANCE_COOKIE):
        missing.append("cf_clearance cookie")
    if not codex:
        if not bundle.oai_device_id:
            missing.append("oai-device-id")
        if not sentinel.requirements_token:
            missing.append("openai-sentinel-chat-requirements-token")
    if missing:
        raise AuthRequired(
            "Direct backend generation is missing required credentials: "
            + ", ".join(missing)
        )

    headers = {
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-language": "en-US",
        "User-Agent": envelope_user_agent(),
    }

    if codex:
        headers["originator"] = CODEX_ORIGINATOR
        headers["OpenAI-Beta"] = CODEX_OPENAI_BETA
        headers["version"] = CODEX_VERSION
        if session_id:
            headers["session_id"] = session_id
        return headers

    headers["oai-device-id"] = bundle.oai_device_id
    requirements_header = (
        "openai-sentinel-chat-requirements-prepare-token"
        if getattr(sentinel, "use_prepare_header", False)
        else "openai-sentinel-chat-requirements-token"
    )
    headers[requirements_header] = sentinel.requirements_token or ""
    if sentinel.proof_token:
        headers["openai-sentinel-proof-token"] = sentinel.proof_token
    if sentinel.turnstile_token:
        headers["openai-sentinel-turnstile-token"] = sentinel.turnstile_token

    if fconv:
        headers["Accept-Language"] = "en-US,en;q=0.9"
        headers["Cache-Control"] = "no-cache"
        headers["Pragma"] = "no-cache"
        if turn_session_id:
            headers["oai-session-id"] = turn_session_id
        if turn_trace_id:
            headers["x-oai-turn-trace-id"] = turn_trace_id
        if conduit_token:
            headers["X-Conduit-Token"] = conduit_token

        client_version = os.environ.get(OAI_CLIENT_VERSION_ENV, "").strip()
        if client_version:
            headers["OAI-Client-Version"] = client_version
        client_build = os.environ.get(OAI_CLIENT_BUILD_ENV, "").strip()
        if client_build:
            headers["OAI-Client-Build-Number"] = client_build

        account_id = getattr(bundle, "chatgpt_account_id", None)
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
    return headers


__all__ = [
    "CF_CLEARANCE_COOKIE",
    "CF_UA_ENV",
    "CLOAKBROWSER_USER_AGENT",
    "CODEX_OPENAI_BETA",
    "CODEX_ORIGINATOR",
    "CODEX_VERSION",
    "OAI_CLIENT_BUILD_ENV",
    "OAI_CLIENT_VERSION_ENV",
    "build_headers",
    "envelope_user_agent",
]
