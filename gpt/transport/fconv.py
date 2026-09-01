from __future__ import annotations

import asyncio
import inspect
import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from gpt.reverse.stream_parser import SSEDecoder
from gpt.state import ProtocolChanged
from gpt.transport.credential_envelope import CF_CLEARANCE_COOKIE
from gpt.transport.token_manager import (
    PROOF_TOKEN_PREFIX,
    SentinelTokens,
    build_fconv_prepare_body,
    fconv_prepare_enabled,
    local_utc_offset_minutes,
    pow_challenge_from_prepare,
    requirements_token_from_prepare,
    resolve_local_timezone,
    solve_sentinel_pow,
)
from gpt.types import SendRequest

SENTINEL_PREPARE_URL = (
    "https://chatgpt.com/backend-api/sentinel/chat-requirements/prepare"
)
SENTINEL_CLASSIC_URL = "https://chatgpt.com/backend-api/sentinel/chat-requirements"
FCONV_PREPARE_URL = "https://chatgpt.com/backend-api/f/conversation/prepare"
FCONV_PREPARE_NOTOKEN = "no-token"
FCONV_RESUME_FLAG = "WEBGPT_FCONV_RESUME"
FCONV_RESUME_OFFSETS = (0, 1, 2)
FCONV_RESUME_MAX_FOLLOWS = 64

logger = logging.getLogger("gpt.transport.curl")


def prepare_enabled() -> bool:
    return fconv_prepare_enabled()


def resume_enabled() -> bool:
    return os.environ.get(FCONV_RESUME_FLAG, "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def integrity_headers(
    bundle: Any,
    user_agent: str,
    session_id: str,
    trace_id: str,
    *,
    sentinel: SentinelTokens | None = None,
) -> dict[str, str]:
    """Header envelope shared by sentinel/conduit prepare calls."""
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {bundle.access_token}",
        "Content-Type": "application/json",
        "OAI-Language": "en-US",
        "OAI-Device-Id": bundle.oai_device_id or "",
        "OAI-Session-Id": session_id,
        "X-OAI-Turn-Trace-Id": trace_id,
        "User-Agent": user_agent,
    }
    cookies = dict(bundle.cookies)
    if bundle.cf_clearance:
        cookies[CF_CLEARANCE_COOKIE] = bundle.cf_clearance
    if cookies:
        headers["Cookie"] = "; ".join(
            f"{name}={value}" for name, value in cookies.items()
        )
    account_id = getattr(bundle, "chatgpt_account_id", None)
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    if sentinel is not None:
        name = (
            "OpenAI-Sentinel-Chat-Requirements-Prepare-Token"
            if sentinel.use_prepare_header
            else "OpenAI-Sentinel-Chat-Requirements-Token"
        )
        headers[name] = sentinel.requirements_token or ""
        if sentinel.proof_token:
            headers["OpenAI-Sentinel-Proof-Token"] = sentinel.proof_token
    return headers


async def prepare_turn(
    bundle: Any,
    request: SendRequest,
    *,
    token_manager: Any,
    post_json: Callable[..., Awaitable[tuple[int | None, Any]]],
    user_agent: str,
    invalidate_access_credentials: Callable[[], None],
    solve_pow: Callable[..., str] = solve_sentinel_pow,
) -> tuple[SentinelTokens, str, str, str | None]:
    """Run sentinel prepare -> PoW -> f/conversation conduit prepare."""
    device_id = bundle.oai_device_id or ""
    turn_session_id = str(uuid.uuid4())
    turn_trace_id = str(uuid.uuid4())
    bootstrap = await token_manager.bootstrap_proof_token(user_agent, device_id)

    integrity = integrity_headers(
        bundle,
        user_agent,
        turn_session_id,
        turn_trace_id,
    )
    status, envelope = await post_json(
        SENTINEL_PREPARE_URL,
        integrity,
        {"p": bootstrap},
        timeout=request.timeout_seconds,
    )
    if status is None or status >= 400:
        status, envelope = await post_json(
            SENTINEL_CLASSIC_URL,
            integrity,
            {"p": bootstrap},
            timeout=request.timeout_seconds,
        )
    if status is None or status >= 400 or not isinstance(envelope, dict):
        if status in {401, 403}:
            invalidate_access_credentials()
        raise ProtocolChanged(
            f"Sentinel chat-requirements prepare failed ({status})."
        )

    token, is_prepare_stage = requirements_token_from_prepare(envelope)
    if not token:
        raise ProtocolChanged("Sentinel prepare returned no requirements token.")

    required, seed, difficulty = pow_challenge_from_prepare(envelope)
    proof_header: str | None = None
    if required:
        answer = await asyncio.to_thread(
            solve_pow,
            seed,
            difficulty,
            user_agent,
            device_id,
        )
        proof_header = PROOF_TOKEN_PREFIX + answer

    sentinel = SentinelTokens(
        requirements_token=token,
        proof_token=proof_header,
        use_prepare_header=is_prepare_stage,
    )

    conduit_token: str | None = None
    try:
        body = build_fconv_prepare_body(
            model=request.model.id if request.model and request.model.id else None,
            timezone_name=resolve_local_timezone(),
            timezone_offset_min=local_utc_offset_minutes(),
            conversation_id=request.conversation_id,
        )
        prepare_headers = integrity_headers(
            bundle,
            user_agent,
            turn_session_id,
            turn_trace_id,
            sentinel=sentinel,
        )
        prepare_headers["X-Conduit-Token"] = FCONV_PREPARE_NOTOKEN
        conduit_status, conduit_envelope = await post_json(
            FCONV_PREPARE_URL,
            prepare_headers,
            body,
            timeout=request.timeout_seconds,
        )
        if conduit_status == 200 and isinstance(conduit_envelope, dict):
            candidate = conduit_envelope.get("conduit_token")
            if isinstance(candidate, str) and candidate:
                conduit_token = candidate
        elif conduit_status in {401, 403}:
            logger.warning(
                "f/conversation/prepare rejected credentials (%s); "
                "invalidating the access-token cache while continuing "
                "without a conduit token.",
                conduit_status,
            )
            invalidate_access_credentials()
        else:
            logger.warning(
                "f/conversation/prepare failed (%s); continuing without "
                "a conduit token.",
                conduit_status,
            )
    except Exception:
        logger.warning(
            "f/conversation/prepare errored; continuing without a conduit token.",
            exc_info=True,
        )
    return sentinel, turn_session_id, turn_trace_id, conduit_token


async def follow_resume_segment(
    *,
    token: str,
    conversation_id: str | None,
    base_headers: dict[str, str] | None,
    request: SendRequest,
    decoder: SSEDecoder,
    absorb: Callable[[str], str],
    on_delta: Callable[[str, str], Awaitable[None] | None] | None,
    turn_id: str,
    conversation_url: str,
    post_conversation: Callable[..., Awaitable[Any]],
    response_chunks: Callable[[Any], AsyncIterator[bytes | str]],
    close_quietly: Callable[[Any], Awaitable[None]],
) -> str:
    """Follow one f/conversation resume handoff across known offset variants."""
    headers = dict(base_headers or {})
    headers["X-Conduit-Token"] = token
    url = conversation_url.rstrip("/") + "/resume"

    async def emit(records: list[str]) -> None:
        for record in records:
            delta = absorb(record)
            if delta and on_delta is not None:
                callback_result = on_delta(delta, turn_id)
                if inspect.isawaitable(callback_result):
                    await callback_result

    for offset in FCONV_RESUME_OFFSETS:
        response = await post_conversation(
            headers,
            {"conversation_id": conversation_id, "offset": offset},
            request,
            url=url,
        )
        status = getattr(response, "status_code", None)
        if status == 404:
            await close_quietly(response)
            continue
        if status is None or not 200 <= status < 300:
            logger.warning(
                "FCONV-RESUME handoff refused (HTTP %s, offset=%s); "
                "ending the resume chain with the text already streamed.",
                status,
                offset,
            )
            await close_quietly(response)
            return "stop"
        try:
            async for chunk in response_chunks(response):
                await emit(decoder.feed(chunk))
            await emit(decoder.finish())
        finally:
            await close_quietly(response)
        return "ok"

    logger.warning(
        "FCONV-RESUME handoff exhausted offsets %s (all 404); ending the "
        "resume chain.",
        FCONV_RESUME_OFFSETS,
    )
    return "exhausted"


__all__ = [
    "FCONV_PREPARE_NOTOKEN",
    "FCONV_PREPARE_URL",
    "FCONV_RESUME_FLAG",
    "FCONV_RESUME_MAX_FOLLOWS",
    "FCONV_RESUME_OFFSETS",
    "SENTINEL_CLASSIC_URL",
    "SENTINEL_PREPARE_URL",
    "follow_resume_segment",
    "integrity_headers",
    "prepare_enabled",
    "prepare_turn",
    "resume_enabled",
]
