"""curl_cffi implementation of the ChatGPT conversation transport."""

from __future__ import annotations

import base64
import binascii
import inspect
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from typing import Any

try:  # Keep browser-only installations importable and easy to unit-test.
    from curl_cffi.requests import AsyncSession
except ImportError:  # pragma: no cover - exercised in minimal installations.
    AsyncSession = None  # type: ignore[assignment,misc]

from gpt.reverse.stream_parser import SSEDecoder
from gpt.state import AuthRequired, ProtocolChanged, RateLimited
from gpt.transport.challenge import (
    ChallengeDetectedError,
    ChallengeKind,
    LimitSignal,
    classify_http_challenge,
    classify_limit_signal,
)
from gpt.transport.credential_envelope import (
    CF_CLEARANCE_COOKIE as _CF_CLEARANCE_COOKIE,
)
from gpt.transport.credential_envelope import (
    CLOAKBROWSER_USER_AGENT,
)
from gpt.transport.credential_envelope import (
    CODEX_VERSION as _CODEX_VERSION,
)
from gpt.transport.credential_envelope import (
    build_headers as _credential_build_headers,
)
from gpt.transport.credential_envelope import (
    envelope_user_agent as _credential_user_agent,
)
from gpt.transport.fconv import (
    FCONV_PREPARE_NOTOKEN as _FCONV_PREPARE_NOTOKEN,
)
from gpt.transport.fconv import (
    FCONV_PREPARE_URL as _FCONV_PREPARE_URL,
)
from gpt.transport.fconv import (
    FCONV_RESUME_FLAG as _FCONV_RESUME_FLAG,
)
from gpt.transport.fconv import (
    FCONV_RESUME_MAX_FOLLOWS as _FCONV_RESUME_MAX_FOLLOWS,
)
from gpt.transport.fconv import (
    FCONV_RESUME_OFFSETS as _FCONV_RESUME_OFFSETS,
)
from gpt.transport.fconv import (
    SENTINEL_CLASSIC_URL as _SENTINEL_CLASSIC_URL,
)
from gpt.transport.fconv import (
    SENTINEL_PREPARE_URL as _SENTINEL_PREPARE_URL,
)
from gpt.transport.fconv import (
    follow_resume_segment as _fconv_follow_resume_segment,
)
from gpt.transport.fconv import (
    integrity_headers as _fconv_integrity_headers,
)
from gpt.transport.fconv import (
    prepare_enabled as _fconv_prepare_enabled_impl,
)
from gpt.transport.fconv import (
    prepare_turn as _fconv_prepare_turn_impl,
)
from gpt.transport.fconv import (
    resume_enabled as _fconv_resume_enabled_impl,
)
from gpt.transport.file_upload import (
    DEFAULT_MAX_IMAGES_PER_TURN,
    ImageUploadError,
    WebFileUploader,
    default_image_name,
    probe_dimensions,
)
from gpt.transport.model_routing import (
    ModelRoute,
    parse_model_alias_env,
    parse_model_fallback_env,
)
from gpt.transport.model_routing import (
    model_fallback_policy as _model_fallback_policy,
)
from gpt.transport.model_routing import (
    model_route_for as _model_route_for,
)
from gpt.transport.model_routing import (
    upstream_fconv_model as _upstream_fconv_model,
)
from gpt.transport.responses_payload import (
    absorb_message_block as _responses_absorb_message_block,
)
from gpt.transport.responses_payload import (
    absorb_tool_result_block as _responses_absorb_tool_result_block,
)
from gpt.transport.responses_payload import (
    assistant_message_item as _responses_assistant_message_item,
)
from gpt.transport.responses_payload import (
    function_call_item as _responses_function_call_item,
)
from gpt.transport.responses_payload import (
    split_prompt_for_responses as _responses_split_prompt_for_responses,
)
from gpt.transport.responses_payload import (
    strip_reasoning_items as _responses_strip_reasoning_items,
)
from gpt.transport.responses_payload import (
    user_input_item as _responses_user_input_item,
)
from gpt.transport.stream import (
    STRIP_PREFIX_FLAG as _STRIP_PREFIX_FLAG,
)
from gpt.transport.stream import (
    collapse_duplicate as _stream_collapse_duplicate,
)
from gpt.transport.stream import (
    consume_record as _stream_consume_record,
)
from gpt.transport.stream import (
    consume_v1_message as _stream_consume_v1_message,
)
from gpt.transport.stream import (
    consume_v1_record as _stream_consume_v1_record,
)
from gpt.transport.stream import (
    merge_candidate as _stream_merge_candidate,
)
from gpt.transport.stream import (
    strip_leading_noise as _stream_strip_leading_noise,
)
from gpt.transport.token_manager import (
    SentinelTokens,
    TokenManager,
    fconv_prepare_enabled,
    solve_sentinel_pow,
)
from gpt.types import SendRequest, TurnResult


def _resolve_impersonate_target() -> str:
    """Pick the curl_cffi impersonation target matching the minting browser.

    Evidence (docs/reports/cf-clearance-lifecycle-2026-08-24.md): cf_clearance
    is bound to IP + UA + TLS client-hello, and replay only succeeded with the
    exact chrome146 fingerprint of CloakBrowser's Chrome/146.0.7680.177.  We
    therefore prefer "chrome146" when this curl_cffi build supports it and keep
    a checked fallback to the generic "chrome" profile otherwise.
    """
    preferred = "chrome146"
    try:
        import typing

        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        if preferred in typing.get_args(BrowserTypeLiteral):
            return preferred
    except Exception:
        return "chrome"
    return "chrome"


# Resolved once at import; tests may monkeypatch curl_transport.IMPERSONATE_TARGET.
IMPERSONATE_TARGET = _resolve_impersonate_target()

# NOTE (evidence 2026-08-24, docs/reports/sentinel-sdk-probe-2026-08-24.md):
# the ANONYMOUS endpoint /backend-anon/f/conversation was verified live with a
# full sentinel header set — HTTP 200 and a real SSE stream — while the
# authenticated /backend-api path stayed 403 "Unusual activity" (device/IP
# reputation, independent of sentinel correctness).  The default endpoint is
# deliberately unchanged this iteration; moving to the verified anon path is a
# one-line change here.
CONVERSATION_URL = "https://chatgpt.com/backend-api/f/conversation"

# Authenticated Codex branch (docs/reports/codex-sse-spec-2026-08-25.md):
# POST /backend-api/codex/responses speaks the Responses API over SSE and
# needs NO sentinel/turnstile/device headers — Bearer access token plus the
# full cookie jar behind Cloudflare suffice.  Opt-in via WEBGPT_CODEX_SSE
# (default OFF) until a live POST is verified with a real Plus account.
CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
_CODEX_SSE_FLAG = "WEBGPT_CODEX_SSE"
_DEFAULT_CODEX_MODEL = "gpt-5"

# CODEX-IMG-INPUT (2026-08-26, automation ROADMAP row M): gpt.api.server
# encodes /v1/responses ``input_image`` blocks as these single-line markers
# inside user message text (only when WEBGPT_CODEX_SSE is on); the codex
# payload builder converts them back into Responses-API ``input_image`` items
# carrying data URLs — the shape /backend-api/codex/responses accepts.
#
# IMG-MARKER-ESCAPE-FIX (2026-08-26, docs/reports/png-upload-liveprobe-
# 2026-08-26.md): markers that travel through the rendered WEBGPT prompt
# (render_messages wraps every message body in a JSON envelope and escapes
# ``<`` -> ``<`` plus ``"`` -> ``\"`` to keep the controller block
# boundaries unforgeable) reach this transport in their escaped form:
# ``<WEBGPT_IMAGE_DATA mime=\"image/png\">…</WEBGPT_IMAGE_DATA>``.
# The strict raw-only grammar never matched there, so the upload pipeline was
# silently unreachable from any real ingress turn (the marker grammar stays
# deliberately strict on mime/base64 charsets so a truncated or corrupted
# marker can never match and just stays inert text; only the exact two escape
# variants render_messages applies were added).
_WEBGPT_IMAGE_MARKER_RE = re.compile(
    r"(?:<|\\u003c)"
    r"WEBGPT_IMAGE_DATA mime="
    r'(?:["]|\\")'
    r"(?P<mime>[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+)"
    r'(?:["]|\\")'
    r"(?:>|\\u003e)"
    r"(?P<data>[A-Za-z0-9+/=]+)"
    r"(?:<|\\u003c)"
    r"/WEBGPT_IMAGE_DATA"
    r"(?:>|\\u003e)"
)
# Defense-in-depth mirror of the ingress skip cap
# (gpt.api.server._CODEX_IMAGE_MAX_B64_CHARS): replayed history can only ever
# contain markers that passed ingress, but direct transport users get the same
# guard here instead of an oversized upload attempt.
_CODEX_IMAGE_MAX_B64_CHARS = 20 * 1024 * 1024

# IMAGE-UPLOAD-WEB (2026-08-26): opt-in upload of ingress image markers to the
# ChatGPT /backend-api/files pipeline, so f/conversation turns can attach real
# images as multimodal_text + image_asset_pointer parts (spec:
# docs/reports/image-upload-research-2026-08-26.md).  Default OFF; flag ON with
# a failing pipeline must degrade to today's omission-note payload on ANY
# failure (fail-open) — an upload problem never kills the turn.
_IMAGE_UPLOAD_WEB_FLAG = "WEBGPT_IMAGE_UPLOAD_WEB"
_UPLOAD_MAX_BYTES_ENV = "WEBGPT_UPLOAD_MAX_BYTES"
_DEFAULT_UPLOAD_MAX_BYTES = 20 * 1024 * 1024

# PORT-F-CONV-RECIPE — authed /f/conversation prepare chain (spec:
# docs/reports/f-conversation-recipe-fields.md).  Opt-in via
# WEBGPT_FCONV_PREPARE; OFF keeps the legacy in-page sentinel flow untouched.
# FCONV-NOTOKEN-REPLAY (2026-08-26, docs/reports/sse-resume-research-
# 2026-08-26.md): kymuco PR #40/#41 observe the FIRST conduit call carrying
# the literal marker "no-token" — by definition no real conduit token exists
# before this response.  Named constant so a future real token source swaps
# in without touching the flow below.
# FCONV-RESUME-HANDOFF (2026-08-26, docs/reports/sse-resume-research-
# 2026-08-26.md row M): long /f/conversation streams are server-split into
# segments; each segment ends [DONE] plus one ``resume_conversation_token``
# event carrying the continuation token.  Three independent clients byte-agree
# on the follow contract (gptweb2api resume.go, OmniRoute handoff.ts, pro-cli
# handbook): POST /backend-api/f/conversation/resume with body
# {conversation_id, offset}, offsets 0->1->2 advanced ONLY on 404,
# X-Conduit-Token set to the captured resume token, at most ~64 handoffs per
# stream (gptweb2api defaultMaxStreamHandoffs).  Opt-in via WEBGPT_FCONV_RESUME;
# default OFF keeps today's drop-in-place parser untouched.
# TODO [CẦN VERIFY] #9: gptweb2api leaves these empty and stays live-200;
# fill them from a real page capture via env before enabling by default.
_COMPLETION_STATUSES = frozenset({"finished_successfully", "finished", "complete"})
# JSON-patch paths observed in the v1 ("delta_encoding") stream format.
_V1_PARTS_PATH = "/message/content/parts/0"
_V1_STATUS_PATH = "/message/status"

logger = logging.getLogger("gpt.transport.curl")


class CurlCffiTransport:
    """Send ChatGPT turns with Chrome TLS impersonation and streamed SSE."""

    def __init__(
        self,
        token_manager: TokenManager,
        *,
        session: Any | None = None,
        conversation_url: str = CONVERSATION_URL,
        codex_auth: Any | None = None,
        proxy: str | None = None,
    ) -> None:
        if session is None:
            if AsyncSession is None:
                raise RuntimeError(
                    "Hybrid transport requires curl_cffi; install the project dependencies."
                )
            resolved_proxy = proxy or os.environ.get("WEBGPT_PROXY", "").strip() or None
            session = AsyncSession(impersonate=IMPERSONATE_TARGET, proxy=resolved_proxy)
        self.token_manager = token_manager
        self._session = session
        # CODEX-AUTH-INTEGRATION: optional pre-built OAuth credential source
        # (tests inject a stub; production leaves it None and one is lazily
        # constructed on first use).  Dormant unless WEBGPT_CODEX_AUTH_JSON
        # is set together with the codex branch.
        self._codex_auth = codex_auth
        # codex_cli_rs sends one stable uuid4 per CLI session (build_session_headers);
        # mirror that: fixed for this transport instance's lifetime, unique across instances.
        self._codex_session_id = str(uuid.uuid4())
        self.conversation_url = conversation_url
        # IMAGE-UPLOAD-WEB: file-service ids keyed by sha256(image bytes),
        # shared by every turn of this transport instance so replayed history
        # never re-uploads the same image.
        self._web_image_cache: dict[str, str] = {}

    async def send(
        self,
        request: SendRequest,
        *,
        on_delta: Callable[[str, str], Awaitable[None] | None] | None = None,
    ) -> TurnResult:
        """POST directly to ChatGPT's backend and consume its SSE response."""
        started = time.monotonic()
        bundle = await self.token_manager.refresh_if_needed()
        if bundle.is_local_mock:
            return await self._send_local_mock(request, on_delta=on_delta, started=started)
        codex = self._codex_sse_enabled()
        fconv = (not codex) and fconv_prepare_enabled()
        # MODEL-ROUTING-PHASE2: resolve (and thereby validate) the downgrade
        # policy up front so a malformed WEBGPT_MODEL_FALLBACK fails loudly on
        # every turn instead of hiding until the first observed downgrade.
        model_fallback_policy = _model_fallback_policy()
        # CODEX-AUTH-INTEGRATION: with both flags on (WEBGPT_CODEX_SSE plus
        # WEBGPT_CODEX_AUTH_JSON), the Bearer comes from the codex OAuth
        # bundle instead of the web-session access token.  Cookies and
        # cf_clearance still come from the browser-minted ``bundle`` above.
        # Unset flag → ``codex_bearer`` stays None and every downstream path
        # is byte-for-byte identical to the pre-integration behavior.
        codex_bearer: str | None = None
        if codex and self._codex_auth_json_enabled():
            codex_bearer = await self._get_codex_bearer()
        turn_session_id: str | None = None
        turn_trace_id: str | None = None
        conduit_token: str | None = None
        if codex:
            # codex/responses needs no sentinel mint (spec §1): skipping
            # get_sentinel_tokens also skips a browser round-trip per turn.
            sentinel = SentinelTokens()
        elif fconv:
            # Authed prepare chain: local bootstrap proof → requirements →
            # PoW → conduit token.  Identity (UA/device/session) is pinned
            # once here and reused across all three calls of the turn.
            sentinel, turn_session_id, turn_trace_id, conduit_token = (
                await self._prepare_fconv_turn(bundle, request)
            )
        else:
            sentinel = await self.token_manager.get_sentinel_tokens(request.conversation_id)
        try:
            headers = self._build_headers(
                bundle,
                sentinel,
                codex=codex,
                session_id=self._codex_session_id if codex else None,
                fconv=fconv,
                turn_session_id=turn_session_id,
                turn_trace_id=turn_trace_id,
                conduit_token=conduit_token,
                bearer_token=codex_bearer,
            )
        except AuthRequired:
            # Invalidate what this branch actually authenticates with — the
            # codex AND authed-fconv envelopes authenticate with the Bearer
            # token / cookie jar and have no usable sentinel cache, so
            # dropping the sentinel there would leave the rejected
            # credentials cached until the refresh interval expires
            # (review rounds 10 and 13).
            if codex or fconv:
                self._invalidate_access_credentials()
            else:
                self._invalidate_sentinel_cache()
            raise
        payload = (
            self._build_codex_payload(request)
            if codex
            else await self._maybe_build_multimodal_payload(
                bundle, request, enabled=fconv
            )
        )
        response = await self._post_conversation(
            headers,
            payload,
            request,
            url=CODEX_RESPONSES_URL if codex else None,
        )
        challenge = await self._http_challenge_kind(response)
        if challenge is not ChallengeKind.NONE:
            # Cloudflare interstitial: the minted clearance is no longer valid
            # for this IP/TLS/UA combination.  The only valid recovery is to
            # re-mint through the REAL browser, then retry exactly once.
            await self._close_quietly(response)
            logger.warning(
                "Cloudflare challenge on transport POST (%s); re-minting "
                "credentials via the real browser and retrying once.",
                challenge.value,
            )
            await self._remint_credentials()
            # Rebuild the envelope from the freshly extracted snapshot: the
            # old headers still pin the challenged cf_clearance/Bearer pair,
            # so retrying with them would repeat the challenge verbatim.
            bundle = await self.token_manager.refresh_if_needed()
            if not codex:
                if fconv:
                    # A fresh turn identity and conduit for the retry: the
                    # old ones are pinned to the challenged envelope.
                    sentinel, turn_session_id, turn_trace_id, conduit_token = (
                        await self._prepare_fconv_turn(bundle, request)
                    )
                else:
                    sentinel = await self.token_manager.get_sentinel_tokens(
                        request.conversation_id
                    )
            headers = self._build_headers(
                bundle,
                sentinel,
                codex=codex,
                session_id=self._codex_session_id if codex else None,
                fconv=fconv,
                turn_session_id=turn_session_id,
                turn_trace_id=turn_trace_id,
                conduit_token=conduit_token,
                bearer_token=codex_bearer,
            )
            response = await self._post_conversation(
                headers,
                payload,
                request,
                url=CODEX_RESPONSES_URL if codex else None,
            )
            retry_kind = await self._http_challenge_kind(response)
            if retry_kind is not ChallengeKind.NONE:
                status = getattr(response, "status_code", None)
                await self._close_quietly(response)
                self._invalidate_sentinel_cache()
                raise ChallengeDetectedError(
                    "ChatGPT is still serving a Cloudflare challenge page after a "
                    "fresh browser clearance re-mint; backing off instead of "
                    "retrying blindly.",
                    kind=retry_kind,
                    url=self.conversation_url,
                    status_code=status,
                )
        # CODEX-AUTH-INTEGRATION 401 handling: with the OAuth bundle active, a
        # bare 401 means the codex access token expired server-side — rotate
        # once through the bundle and retry EXACTLY once.  If the rotation
        # hits invalid_grant / chain expiry, CodexAuthDead propagates (a
        # rotated-away grant cannot come back; no retry loop).  A second 401
        # marks the OAuth source untrusted (round 13) and falls through to
        # _raise_for_status's existing AuthRequired path.
        if codex_bearer is not None and getattr(response, "status_code", 200) == 401:
            await self._close_quietly(response)
            logger.warning(
                "codex/responses rejected the OAuth bearer (401); rotating the "
                "codex auth token once and retrying."
            )
            self._invalidate_codex_auth_cache()
            # Round 13: invalidate() alone re-reads the SAME still-fresh token
            # from auth.json — force a REAL refresh grant so the retry cannot
            # replay the exact bearer the server just refused.
            codex_bearer = await self._get_codex_bearer(force_refresh=True)
            headers = self._build_headers(
                bundle,
                sentinel,
                codex=codex,
                session_id=self._codex_session_id,
                bearer_token=codex_bearer,
            )
            response = await self._post_conversation(
                headers,
                payload,
                request,
                url=CODEX_RESPONSES_URL,
            )
            if getattr(response, "status_code", 200) == 401:
                # Even a genuinely rotated bearer was refused: latch distrust
                # so later turns force real refreshes instead of serving the
                # rejected snapshot from cache (round 13), then let the
                # generic path below drop the browser-side cache and raise.
                logger.warning(
                    "codex/responses returned 401 twice in one turn; marking "
                    "the codex OAuth credential untrusted."
                )
                self._mark_codex_auth_untrusted()
        try:
            await self._raise_for_status(response, codex=codex, fconv=fconv)
            if codex:
                # One fully accepted request re-validates a distrusted source.
                self._mark_codex_auth_trusted()
            if codex:
                result = await self._stream_codex_sse(response, request, on_delta=on_delta)
            else:
                # FCONV-RESUME-HANDOFF: hand the turn envelope to the parser so
                # a [DONE]+resume_conversation_token split stream can be
                # followed; the envelope is ignored unless WEBGPT_FCONV_RESUME
                # is on and the event actually appears.
                result = await self._stream_sse(
                    response,
                    request,
                    on_delta=on_delta,
                    envelope_headers=headers,
                )
            # MODEL-ROUTING-PHASE2: optional single default-model retry after
            # a served-slug downgrade (WEBGPT_MODEL_FALLBACK=retry-once).
            # Never raises: on any retry trouble the original result stands.
            fallback_result = await self._maybe_retry_model_fallback(
                result,
                request,
                payload=payload,
                headers=headers,
                codex=codex,
                fconv=fconv,
                policy=model_fallback_policy,
                on_delta=on_delta,
            )
            if fallback_result is not None:
                result = fallback_result
            result.duration_ms = int((time.monotonic() - started) * 1_000)
            return result
        finally:
            close = getattr(response, "aclose", None)
            if close is not None:
                closed = close()
                if inspect.isawaitable(closed):
                    await closed

    async def _maybe_retry_model_fallback(
        self,
        result: TurnResult,
        request: SendRequest,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        codex: bool,
        fconv: bool,
        policy: str,
        on_delta: Callable[[str, str], Awaitable[None] | None] | None,
    ) -> TurnResult | None:
        """MODEL-ROUTING-PHASE2: one-shot default-model retry on downgrade.

        Active only under ``WEBGPT_MODEL_FALLBACK=retry-once`` and only when
        attempt one provably downgraded an explicit/alias-routed model (served
        slug differs from the slug we sent — currently observable on the fconv
        SSE path only; the codex endpoint publishes no served slug).  The
        retry re-sends the SAME envelope with the platform default model
        exactly once more and prefixes its stream with
        ``[webgpt:model-fallback <requested>→<served>]`` so CLI consumers can
        tell the substitution apart.  This method never fails the turn: any
        error inside the retry is logged and the complete attempt-one result
        is returned untouched instead.
        """
        if (
            codex
            or policy != "retry-once"
            or not result.model_downgraded
            or not result.requested_model
        ):
            return None
        requested = result.requested_model
        served = result.resolved_model or result.model or ""
        marker = f"[webgpt:model-fallback {requested}→{served}]\n"
        logger.warning(
            "MODEL-ROUTING fallback: requested model=%r but ChatGPT served %r; "
            "retrying once with the default model.",
            requested,
            served,
        )
        try:
            # Same envelope, default model: root "auto" is exactly what the
            # builder emits when no explicit model is set, and the alias-pinned
            # effort is dropped unless the client itself supplied one.
            retry_payload = dict(payload)
            retry_payload["model"] = "auto"
            if not request.reasoning_effort:
                retry_payload.pop("thinking_effort", None)
            # Fresh per-turn uuids — replaying attempt one's message/parent
            # ids would look like a duplicate submission to the conversation.
            messages = retry_payload.get("messages")
            if isinstance(messages, list) and messages and isinstance(messages[0], dict):
                retry_payload["messages"] = [
                    {**messages[0], "id": str(uuid.uuid4())},
                    *messages[1:],
                ]
            if "parent_message_id" in retry_payload:
                retry_payload["parent_message_id"] = str(uuid.uuid4())
            # Strip the routed model from the streamed request too, so the
            # retry's own served slug cannot re-trigger mismatch verification
            # against a route we have deliberately abandoned.
            retry_request = replace(request, model=None)
            if on_delta is None:
                stream_on_delta = None
            else:
                downstream = on_delta
                marker_pending = True

                def stream_on_delta(delta: str, turn_id: str) -> Any:
                    nonlocal marker_pending
                    prefix = marker if marker_pending else ""
                    marker_pending = False
                    return downstream(prefix + delta, turn_id)

            retry_response = await self._post_conversation(
                headers, retry_payload, retry_request
            )
            try:
                await self._raise_for_status(retry_response, codex=codex, fconv=fconv)
                retry_result = await self._stream_sse(
                    retry_response,
                    retry_request,
                    on_delta=stream_on_delta,
                )
            finally:
                await self._close_quietly(retry_response)
        except Exception as exc:  # deliberate: fallback must never fail the turn
            logger.warning(
                "MODEL-ROUTING fallback retry failed (%s); keeping the "
                "original turn result.",
                exc,
            )
            return None
        if not retry_result.text:
            # Retry produced nothing usable — attempt one already carries the
            # full text, so prefer it over an empty substitution.
            return None
        retry_result.text = marker + retry_result.text
        # Telemetry keeps describing the DOWNGRADE observed on attempt one;
        # the deliberate default-model serving of the retry is not a mismatch.
        retry_result.requested_model = requested
        retry_result.resolved_model = result.resolved_model or result.model
        retry_result.model_downgraded = True
        retry_result.model_downgrade_count = 1
        return retry_result

    async def _post_conversation(
        self,
        headers: dict[str, str],
        payload: dict[str, Any],
        request: SendRequest,
        *,
        url: str | None = None,
    ) -> Any:
        return await self._session.post(
            url or self.conversation_url,
            headers=headers,
            json=payload,
            stream=True,
            timeout=request.timeout_seconds,
        )

    async def _remint_credentials(self) -> None:
        """Force a fresh credential snapshot through the real browser context.

        ``extract_all`` bypasses the refresh interval and reads cookies (with a
        fresh cf_clearance), the session token and the device id from the live
        browser page — the same clearance-minting path that produced the
        original bundle.
        """
        self._invalidate_sentinel_cache()
        extract = getattr(self.token_manager, "extract_all", None)
        if not callable(extract):
            raise AuthRequired(
                "Cannot re-mint credentials: the token manager does not expose "
                "a browser-backed extract_all()."
            )
        await extract()

    @staticmethod
    def _fconv_prepare_enabled() -> bool:
        return _fconv_prepare_enabled_impl()

    async def _prepare_fconv_turn(
        self,
        bundle: Any,
        request: SendRequest,
    ) -> tuple[SentinelTokens, str, str, str | None]:
        return await _fconv_prepare_turn_impl(
            bundle,
            request,
            token_manager=self.token_manager,
            post_json=self._post_json,
            user_agent=self._envelope_user_agent(),
            invalidate_access_credentials=self._invalidate_access_credentials,
            solve_pow=solve_sentinel_pow,
        )

    def _integrity_headers(
        self,
        bundle: Any,
        user_agent: str,
        session_id: str,
        trace_id: str,
        *,
        sentinel: SentinelTokens | None = None,
    ) -> dict[str, str]:
        return _fconv_integrity_headers(
            bundle,
            user_agent,
            session_id,
            trace_id,
            sentinel=sentinel,
        )

    async def _post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        timeout: float | None,
    ) -> tuple[int | None, Any]:
        """POST JSON on the shared impersonated session, read one JSON body."""
        response = await self._session.post(
            url,
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        try:
            status = getattr(response, "status_code", None)
            body: Any = None
            reader = getattr(response, "json", None)
            if callable(reader):
                try:
                    parsed = reader()
                    body = (
                        await parsed if inspect.isawaitable(parsed) else parsed
                    )
                except Exception:
                    body = None
            return status, body
        finally:
            await self._close_quietly(response)

    async def _http_challenge_kind(self, response: Any) -> ChallengeKind:
        """Classify whether this HTTP response is a Cloudflare challenge page."""
        status = getattr(response, "status_code", 200)
        if status < 400:
            return ChallengeKind.NONE
        snippet = await self._peek_body_sample(response)
        return classify_http_challenge(status, snippet)

    @staticmethod
    async def _peek_body_sample(response: Any, limit: int = 8192) -> str | None:
        """Best-effort read of the first bytes of a (streamed) error body."""
        pieces: list[str] = []
        consumed = 0
        try:
            async for chunk in CurlCffiTransport._response_chunks(response):
                text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
                pieces.append(text[: limit - consumed])
                consumed += len(pieces[-1])
                if consumed >= limit:
                    break
        except Exception:
            # A body we cannot read must not mask the status-based handling.
            pass
        sample = "".join(pieces)
        return sample or None

    @staticmethod
    async def _close_quietly(response: Any) -> None:
        close = getattr(response, "aclose", None) or getattr(response, "close", None)
        if close is not None:
            try:
                closed = close()
                if inspect.isawaitable(closed):
                    await closed
            except Exception:
                return

    async def _send_local_mock(
        self,
        request: SendRequest,
        *,
        on_delta: Callable[[str, str], Awaitable[None] | None] | None,
        started: float,
    ) -> TurnResult:
        """Serve the dev/test fallback without ever contacting ChatGPT.

        The runtime sees normal deltas and the usual tool-call sentinel, so
        Anthropic/Claude Code SSE and tool handling exercise the production
        protocol adapters rather than a separate mock endpoint.
        """
        text = self._local_mock_text(request.text)
        turn_id = f"turn_local_{uuid.uuid4().hex[:12]}"
        if on_delta is not None:
            for offset in range(0, len(text), 24):
                result = on_delta(text[offset : offset + 24], turn_id)
                if inspect.isawaitable(result):
                    await result
        return TurnResult(
            turn_id=turn_id,
            conversation_id=request.conversation_id or f"local_{uuid.uuid4().hex[:12]}",
            text=text,
            model=request.model.label if request.model else "local-mock",
            status="completed",
            duration_ms=int((time.monotonic() - started) * 1_000),
        )

    @staticmethod
    def _local_mock_text(prompt: str) -> str:
        """Produce deterministic text or one schema-compatible tool call."""
        user_messages = re.findall(
            r'<WEBGPT_MESSAGE role="user">\n(.+?)\n</WEBGPT_MESSAGE>', prompt, re.DOTALL
        )
        user_text = "request"
        if user_messages:
            for chunk in reversed(user_messages):
                raw_chunk = chunk.strip()
                extracted = ""
                try:
                    payload = json.loads(raw_chunk)
                    if isinstance(payload, dict):
                        raw_content = payload.get("content")
                        if isinstance(raw_content, str):
                            extracted = raw_content.strip()
                        elif isinstance(raw_content, list):
                            parts = [
                                block.get("text", "")
                                for block in raw_content
                                if isinstance(block, dict) and isinstance(block.get("text"), str)
                            ]
                            extracted = " ".join(parts).strip()
                    elif isinstance(payload, str):
                        extracted = payload.strip()
                except json.JSONDecodeError:
                    extracted = raw_chunk

                clean = re.sub(r"<system-reminder>.*?</system-reminder>", "", extracted, flags=re.DOTALL).strip()
                if clean:
                    user_text = clean
                    break

        declarations = re.search(r"Available tools: (\[.+?\])\n", prompt)
        has_tool_result = "<WEBGPT_TOOL_RESULT>" in prompt
        is_correction = "WEBGPT CONTROLLER CORRECTION:" in prompt
        requests_tool = bool(
            is_correction
            or re.match(r"^\s*(?:read|bash)\s", user_text, re.IGNORECASE)
            or re.search(r"\b(?:tool|command|execute|inspect|status|pyproject)\b|\brun\s", user_text, re.IGNORECASE)
        )
        if declarations and requests_tool and not has_tool_result:
            try:
                tools = json.loads(declarations.group(1))
            except json.JSONDecodeError:
                tools = []
            if isinstance(tools, list) and tools:
                selected = next(
                    (
                        tool
                        for tool in tools
                        if isinstance(tool, dict)
                        and tool.get("name") in {"Read", "read", "Bash", "bash"}
                    ),
                    tools[0],
                )
                if isinstance(selected, dict) and isinstance(selected.get("name"), str):
                    arguments = CurlCffiTransport._local_mock_arguments(selected)
                    payload = {
                        "name": selected["name"],
                        "arguments": arguments,
                    }
                    return "<WEBGPT_TOOL_CALL>\n" + json.dumps(payload) + "\n</WEBGPT_TOOL_CALL>"
        return CurlCffiTransport._format_conversational_reply(user_text)

    @staticmethod
    def _format_conversational_reply(user_text: str) -> str:
        cleaned = user_text.strip().casefold()
        if cleaned in {"hi", "hello", "hey", "chao", "chào", "xin chao", "xin chào"}:
            return "Xin chào! Tôi có thể giúp gì cho bạn hôm nay?"
        if any(q in cleaned for q in ["toio laf ai", "tôi là ai", "toi la ai", "who am i"]):
            return "Bạn là lập trình viên / người dùng trên hệ thống, và tôi là trợ lý AI (Claude Code) đồng hành cùng bạn để phân tích, lập trình và giải quyết tác vụ."
        if any(q in cleaned for q in ["ban la ai", "bạn là ai", "who are you"]):
            return "Tôi là Claude Code - trợ lý AI lập trình được kết nối trực tiếp qua WebGPT Gateway."
        if any(q in cleaned for q in ["giup gi", "giúp gì", "help", "can you help"]):
            return "Tôi có thể hỗ trợ bạn đọc/viết mã nguồn, chạy lệnh shell, kiểm thử phần mềm, debug và xây dựng toàn bộ dự án."
        return f"Tôi đã tiếp nhận yêu cầu: {user_text.strip()}. Bạn cần tôi thực hiện bước nào tiếp theo?"

    @staticmethod
    def _local_mock_arguments(tool: dict[str, Any]) -> dict[str, Any]:
        parameters = tool.get("parameters")
        if not isinstance(parameters, dict):
            return {}
        properties = parameters.get("properties")
        required = parameters.get("required")
        if not isinstance(properties, dict) or not isinstance(required, list):
            return {}
        arguments: dict[str, Any] = {}
        for name in required:
            if not isinstance(name, str):
                continue
            schema = properties.get(name)
            schema = schema if isinstance(schema, dict) else {}
            if name in {"file_path", "path", "filename"}:
                arguments[name] = "README.md"
            elif name in {"command", "cmd"}:
                arguments[name] = "pwd"
            elif schema.get("type") == "boolean":
                arguments[name] = False
            elif schema.get("type") in {"integer", "number"}:
                arguments[name] = 1
            elif schema.get("type") == "array":
                arguments[name] = []
            elif schema.get("type") == "object":
                arguments[name] = {}
            else:
                arguments[name] = "mock"
        return arguments

    # -- IMAGE-UPLOAD-WEB (2026-08-26): ingress markers → asset pointers -----

    @staticmethod
    def _image_upload_web_enabled() -> bool:
        """Whether the web file-upload pipeline is opted in (default OFF)."""
        return os.environ.get(_IMAGE_UPLOAD_WEB_FLAG, "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _web_upload_max_bytes() -> int:
        """Per-image byte cap; WEBGPT_UPLOAD_MAX_BYTES overrides the default."""
        raw = os.environ.get(_UPLOAD_MAX_BYTES_ENV, "").strip()
        if not raw:
            return _DEFAULT_UPLOAD_MAX_BYTES
        try:
            value = int(raw)
        except ValueError:
            return _DEFAULT_UPLOAD_MAX_BYTES
        return value if value > 0 else _DEFAULT_UPLOAD_MAX_BYTES

    async def _maybe_build_multimodal_payload(
        self,
        bundle: Any,
        request: SendRequest,
        *,
        enabled: bool,
    ) -> dict[str, Any]:
        """Build the f/conversation payload, uploading images when opted in.

        ``enabled`` rides the fconv gate: only the authed prepare chain turns
        carry uploads.  Disabled → legacy text-only payload, untouched.
        """
        if not enabled:
            return self._build_conversation_payload(request)
        image_assets = await self._upload_turn_images(bundle, request)
        return self._build_conversation_payload(request, image_assets=image_assets)

    def _file_upload_headers(self, bundle: Any) -> dict[str, str]:
        """Backend-api envelope for the files endpoints (research §2).

        Same credential snapshot as the SSE turn — Bearer access token, full
        cookie jar incl. cf_clearance, device id, pinned UA.  No sentinel
        headers: the files API never asked for them (gptweb2api capture).
        """
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {bundle.access_token}",
            "Content-Type": "application/json",
            "OAI-Language": "en-US",
            "OAI-Device-Id": bundle.oai_device_id or "",
            "User-Agent": self._envelope_user_agent(),
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }
        cookies = dict(bundle.cookies)
        if bundle.cf_clearance:
            cookies[_CF_CLEARANCE_COOKIE] = bundle.cf_clearance
        if cookies:
            headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in cookies.items()
            )
        account_id = getattr(bundle, "chatgpt_account_id", None)
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        return headers

    async def _upload_turn_images(
        self,
        bundle: Any,
        request: SendRequest,
    ) -> list[tuple[int, int, str, int, str, tuple[int, int] | None]] | None:
        """Upload every eligible marker image; None keeps the legacy payload.

        Fail-open contract: flag off, no markers, missing credentials, decode
        errors and ANY upload error all degrade toward whatever succeeded
        (None when empty), so the turn falls back to today's placeholder.
        """
        if not self._image_upload_web_enabled():
            return None
        if _WEBGPT_IMAGE_MARKER_RE.search(request.text) is None:
            return None
        if not getattr(bundle, "access_token", ""):
            logger.warning("Image upload skipped: credential bundle has no access token.")
            return None
        max_bytes = self._web_upload_max_bytes()
        uploader = WebFileUploader(
            self._session,
            lambda: self._file_upload_headers(bundle),
            timeout=request.timeout_seconds,
            max_bytes=max_bytes,
            cache=self._web_image_cache,
        )
        assets = await self._collect_image_assets(
            request.text, uploader.upload_image, max_bytes=max_bytes
        )
        return assets or None

    @classmethod
    async def _collect_image_assets(
        cls,
        text: str,
        upload: Callable[[bytes, str, str], Any],
        *,
        max_bytes: int,
    ) -> list[tuple[int, int, str, int, str, tuple[int, int] | None]]:
        """Decode + upload markers into span-anchored asset records.

        ``upload`` is any ``(data, mime, name) -> file_id`` awaitable callable
        so tests drive this without HTTP.  Records are ``(start, end, mime,
        size_bytes, file_id, dimensions)``; failed/oversized markers are
        skipped here and re-render as omission notes by the payload builder.
        At most :data:`DEFAULT_MAX_IMAGES_PER_TURN` images per turn (the
        first ones, matching gptweb2api's attachment ceiling).
        """
        matches = list(_WEBGPT_IMAGE_MARKER_RE.finditer(text))[
            :DEFAULT_MAX_IMAGES_PER_TURN
        ]
        assets: list[tuple[int, int, str, int, str, tuple[int, int] | None]] = []
        for match in matches:
            mime = match.group("mime")
            encoded = match.group("data")
            # Defense-in-depth mirror of the codex branch cap: replayed
            # history passed ingress, direct transport users do not bypass it.
            if len(encoded) > _CODEX_IMAGE_MAX_B64_CHARS:
                continue
            try:
                data = base64.b64decode(encoded)
            except (binascii.Error, ValueError):
                continue
            if len(data) > max_bytes:
                continue
            dims = probe_dimensions(data)
            name = default_image_name(mime)
            try:
                file_id = await upload(data, mime, name)
            except ImageUploadError as exc:
                logger.warning(
                    "Web image upload failed (%s ~%dKB): %s",
                    mime,
                    len(data) // 1024,
                    exc,
                )
                continue
            except Exception:  # uploader bug must never kill the turn
                logger.warning("Web image upload errored unexpectedly.", exc_info=True)
                continue
            assets.append((match.start(), match.end(), mime, len(data), file_id, dims))
        return assets

    @staticmethod
    def _multimodal_parts_and_attachments(
        text: str,
        assets: list[tuple[int, int, str, int, str, tuple[int, int] | None]],
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        """Interleave text segments with uploaded pointers (research §4).

        Markers without an uploaded asset (failed, oversized, undecodable, or
        beyond the per-turn cap) keep the exact omission note of the legacy
        path, so a partial failure reads like today's degraded output.
        """
        by_span = {asset[0:2]: asset for asset in assets}
        parts: list[Any] = []
        attachments: list[dict[str, Any]] = []
        buffer: list[str] = []
        cursor = 0

        def flush_text() -> None:
            segment = "".join(buffer)
            buffer.clear()
            if segment.strip():
                parts.append(segment)

        for match in _WEBGPT_IMAGE_MARKER_RE.finditer(text):
            buffer.append(text[cursor : match.start()])
            cursor = match.end()
            asset = by_span.get(match.span())
            if asset is None:
                buffer.append(f"[image omitted: {match.group('mime')}]")
                continue
            flush_text()
            _, _, mime, size_bytes, file_id, dims = asset
            pointer: dict[str, Any] = {
                "content_type": "image_asset_pointer",
                "asset_pointer": f"file-service://{file_id}",
                "size_bytes": size_bytes,
            }
            attachment: dict[str, Any] = {
                "id": file_id,
                "name": default_image_name(mime),
                "size": size_bytes,
                "mime_type": mime,
            }
            if dims is not None:
                pointer["width"], pointer["height"] = dims
                attachment["width"], attachment["height"] = dims
            pointer["fovea"] = None
            pointer["metadata"] = {"dalle": None, "gizmo": None}
            parts.append(pointer)
            attachments.append(attachment)
        buffer.append(text[cursor:])
        flush_text()
        return parts, attachments

    def _build_conversation_payload(
        self,
        request: SendRequest,
        image_assets: list[
            tuple[int, int, str, int, str, tuple[int, int] | None]
        ] | None = None,
    ) -> dict[str, Any]:
        """Build the browser-compatible subset of the conversation payload.

        IMAGE-UPLOAD-WEB: with ``image_assets`` (uploaded file-service ids
        anchored to marker spans), the user message upgrades to
        ``multimodal_text`` carrying ``image_asset_pointer`` parts plus
        ``metadata.attachments`` (research §4 recipe).  ``None`` preserves the
        legacy text-only message byte-for-byte.

        MODEL-ROUTING-PHASE1: an operator-installed WEBGPT_MODEL_ALIAS entry
        matching this request's id/label rewrites the root ``model`` slug and
        may pin ``thinking_effort``; without a match every byte below is
        produced exactly as before.
        """
        model = request.model.id if request.model and request.model.id else None
        model = model or (request.model.label if request.model else "auto")
        route = _model_route_for(model)
        if route is not None:
            model = route.slug
        message: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "author": {"role": "user"},
        }
        if image_assets:
            parts, attachments = self._multimodal_parts_and_attachments(
                request.text, image_assets
            )
            message["content"] = {
                "content_type": "multimodal_text",
                "parts": parts,
            }
            message["metadata"] = {"attachments": attachments}
        else:
            # CODEX-IMG-INPUT: without a successful upload pipeline every
            # ingress marker degrades to a short omission note instead of
            # leaking megabytes of base64 into the prompt.
            message["content"] = {
                "content_type": "text",
                "parts": [self._strip_image_markers(request.text)],
            }
        payload: dict[str, Any] = {
            "action": "next",
            "messages": [message],
            "model": model,
            "parent_message_id": str(uuid.uuid4()),
            "conversation_mode": {"kind": "primary_assistant"},
        }
        if request.conversation_id:
            payload["conversation_id"] = request.conversation_id
        effort = request.reasoning_effort or (route.effort if route else None)
        if effort:
            e = effort.strip().lower()
            if e in {"high", "max", "extended", "3"}:
                payload["thinking_effort"] = "extended"
            elif e in {"medium", "standard", "2"}:
                payload["thinking_effort"] = "standard"
            elif e in {"low", "instant", "1"}:
                payload["thinking_effort"] = "low"
            else:
                payload["thinking_effort"] = effort
        return payload

    def _build_codex_payload(self, request: SendRequest) -> dict[str, Any]:
        """Build the Responses-API payload for /backend-api/codex/responses.

        Spec (docs/reports/codex-sse-spec-2026-08-25.md §2): ``store: false``
        is mandatory under OAuth access tokens and ``stream: true`` is
        mandatory (stream:false → HTTP 400).  The endpoint is stateless, so
        conversation_id is never sent — every turn re-sends full history as
        input items, exactly like the f/conversation path.  Model slugs are
        dotted ("gpt-5.2"); the dashed form is rejected server-side.

        CLI-shape parity guards (next-horizon research 2026-08-25, F1.3 /
        David-Factor rules): replayed ``reasoning`` items are stripped
        (store:false ⇒ nothing persisted ⇒ replay is meaningless noise and a
        detectable divergence) and ``max_output_tokens`` /
        ``max_completion_tokens`` are deleted — codex_cli_rs never sends them.

        CODEX-IMG-INPUT: user messages carrying ingress image markers are
        expanded into interleaved input_text / input_image parts; text-only
        histories pass through with identical objects (byte-identical payload).
        """
        instructions, input_items = self._split_prompt_for_responses(request.text)
        model = request.model.id if request.model and request.model.id else ""
        # MODEL-ROUTING-PHASE1: same opt-in env map as the fconv builder; the
        # operator supplies the dotted codex-form slug here.  No effort field:
        # the codex/responses envelope is spec-pinned (codex-sse-spec §2) and
        # no reasoning field is evidenced on this endpoint.
        route = _model_route_for(model)
        if route is not None:
            model = route.slug
        payload = {
            "model": model or _DEFAULT_CODEX_MODEL,
            "instructions": "\n\n".join(instructions),
            "input": self._expand_image_markers(self._strip_reasoning_items(input_items)),
            "tools": [],
            "tool_choice": "auto",
            "store": False,
            "stream": True,
        }
        for forbidden in ("max_output_tokens", "max_completion_tokens"):
            payload.pop(forbidden, None)
        return payload

    @staticmethod
    def _strip_reasoning_items(
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return _responses_strip_reasoning_items(items)

    # -- CODEX-IMG-INPUT (2026-08-26): ingress image markers → input_image ----

    @classmethod
    def _expand_image_markers(cls, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Expand ingress image markers inside user messages into image parts.

        Histories without any marker come back untouched (identical objects ⇒
        byte-identical payloads); only user message items that actually carry
        a marker are rebuilt, interleaving ``input_text`` and ``input_image``
        parts in original order.
        """
        if not any(cls._item_has_image_marker(item) for item in items):
            return items
        return [
            cls._expand_user_message_item(item)
            if cls._item_has_image_marker(item)
            else item
            for item in items
        ]

    @classmethod
    def _item_has_image_marker(cls, item: Any) -> bool:
        if not (
            isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role") == "user"
        ):
            return False
        content = item.get("content")
        if not isinstance(content, list):
            return False
        return any(
            isinstance(part, dict)
            and part.get("type") == "input_text"
            and isinstance(part.get("text"), str)
            and _WEBGPT_IMAGE_MARKER_RE.search(part["text"]) is not None
            for part in content
        )

    @classmethod
    def _expand_user_message_item(cls, item: dict[str, Any]) -> dict[str, Any]:
        """Rebuild one user message, splitting its text around image markers.

        Parts without markers are appended unchanged (same object); a marked
        text part becomes alternating non-empty text segments and image parts.
        """
        parts: list[dict[str, Any]] = []
        for part in item["content"]:
            if not (
                isinstance(part, dict)
                and part.get("type") == "input_text"
                and isinstance(part.get("text"), str)
                and _WEBGPT_IMAGE_MARKER_RE.search(part["text"]) is not None
            ):
                parts.append(part)
                continue
            text = part["text"]
            cursor = 0
            for match in _WEBGPT_IMAGE_MARKER_RE.finditer(text):
                head = text[cursor : match.start()]
                if head.strip():
                    parts.append({"type": "input_text", "text": head})
                parts.append(cls._codex_image_part(match.group("mime"), match.group("data")))
                cursor = match.end()
            tail = text[cursor:]
            if tail.strip():
                parts.append({"type": "input_text", "text": tail})
        rebuilt = dict(item)
        rebuilt["content"] = parts
        return rebuilt

    @classmethod
    def _codex_image_part(cls, mime: str, data: str) -> dict[str, Any]:
        """One input_image part with a data URL, or an omission note if huge."""
        if len(data) > _CODEX_IMAGE_MAX_B64_CHARS:
            logger.warning(
                "Codex image marker (%s, ~%dKB) exceeds the %dMB upload cap; "
                "sending an omission note instead.",
                mime,
                len(data) // 1024,
                _CODEX_IMAGE_MAX_B64_CHARS // (1024 * 1024),
            )
            return {
                "type": "input_text",
                "text": f"[image omitted: {mime} ~{len(data) // 1024}KB exceeds upload cap]",
            }
        return {"type": "input_image", "image_url": f"data:{mime};base64,{data}"}

    @classmethod
    def _strip_image_markers(cls, text: str) -> str:
        """Degrade ingress image markers for the web conversation path.

        Legacy fallback (IMAGE-UPLOAD-WEB flag off, non-fconv branches, or a
        fully failed upload batch): each marker becomes a short omission note
        instead of leaking megabytes of base64 into the pasted prompt.  Text
        without markers is returned untouched.
        """
        if _WEBGPT_IMAGE_MARKER_RE.search(text) is None:
            return text
        return _WEBGPT_IMAGE_MARKER_RE.sub(
            lambda match: f"[image omitted: {match.group('mime')}]", text
        )

    @classmethod
    def _split_prompt_for_responses(
        cls, text: str
    ) -> tuple[list[str], list[dict[str, Any]]]:
        return _responses_split_prompt_for_responses(text)

    @staticmethod
    def _absorb_message_block(
        role: str,
        body: str,
        instructions: list[str],
        input_items: list[dict[str, Any]],
    ) -> set[str]:
        return _responses_absorb_message_block(role, body, instructions, input_items)

    @staticmethod
    def _absorb_tool_result_block(
        body: str,
        input_items: list[dict[str, Any]],
        function_call_ids: set[str],
    ) -> None:
        _responses_absorb_tool_result_block(body, input_items, function_call_ids)

    @staticmethod
    def _user_input_item(text: str) -> dict[str, Any]:
        return _responses_user_input_item(text)

    @staticmethod
    def _assistant_message_item(text: str) -> dict[str, Any]:
        return _responses_assistant_message_item(text)

    @staticmethod
    def _function_call_item(call: dict[str, Any]) -> dict[str, Any] | None:
        return _responses_function_call_item(call)

    @staticmethod
    def _envelope_user_agent() -> str:
        return _credential_user_agent()

    @staticmethod
    def _build_headers(
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
        return _credential_build_headers(
            bundle,
            sentinel,
            codex=codex,
            session_id=session_id,
            fconv=fconv,
            turn_session_id=turn_session_id,
            turn_trace_id=turn_trace_id,
            conduit_token=conduit_token,
            bearer_token=bearer_token,
        )

    def _invalidate_sentinel_cache(self) -> None:
        """Drop the cached sentinel so the next turn mints fresh credentials."""
        invalidate = getattr(self.token_manager, "invalidate_sentinel", None)
        if callable(invalidate):
            invalidate()

    def _invalidate_access_credentials(self) -> None:
        """Drop the cached access token / cookie jar the codex branch relies on.

        The codex/responses envelope authenticates purely with the Bearer
        token plus cookies — there is no sentinel to drop, so a rejection
        must invalidate this snapshot instead or the next turn reuses the
        exact credentials the server just refused.
        """
        invalidate = getattr(self.token_manager, "invalidate_access_token", None)
        if callable(invalidate):
            invalidate()

    @staticmethod
    def _codex_auth_json_enabled() -> bool:
        """Whether the codex OAuth bundle source is opted in (WEBGPT_CODEX_AUTH_JSON).

        Lazy import keeps gpt.transport.codex_auth fully dormant (no module
        load at all) unless the codex branch is actually in play.
        """
        try:
            from gpt.transport.codex_auth import codex_auth_enabled
        except ImportError:  # pragma: no cover - defensive; same package
            return False
        return codex_auth_enabled()

    async def _get_codex_bearer(self, *, force_refresh: bool = False) -> str:
        """Access token from the codex OAuth bundle (refreshing when stale).

        Lazily builds one :class:`CodexAuthManager` per transport instance on
        first use.  May raise ``CodexAuthDead`` / ``CodexAuthInvalid`` /
        ``CodexAuthTransient`` from the codex_auth module — all propagate to
        the caller unchanged.  ``force_refresh=True`` (round 13) makes the
        source post a real refresh grant instead of serving a cached or
        on-disk still-fresh snapshot — used after a resource-server 401.
        """
        source = self._codex_auth
        if source is None:
            from gpt.transport.codex_auth import (
                CodexAuthManager,  # lazy: dormant module
            )

            source = CodexAuthManager()
            self._codex_auth = source
        return await source.get_access_token(force_refresh=force_refresh)

    def _invalidate_codex_auth_cache(self) -> None:
        """Drop the cached OAuth snapshot so the next call reloads + rotates.

        Deliberately does NOT clear a terminal DEAD mark — codex_auth already
        fails fast on dead grants and only a fresh ``codex login`` fixes them.
        """
        invalidate = getattr(self._codex_auth, "invalidate", None)
        if callable(invalidate):
            invalidate()

    def _mark_codex_auth_untrusted(self) -> None:
        """Latch distrust (round 13): rotated bearers were refused twice.

        Guarded getattr like every other optional-source hook, so test stubs
        without the latch simply no-op.  The real manager then forces real
        refreshes on later fetches until an accepted request clears it.
        """
        mark = getattr(self._codex_auth, "mark_untrusted", None)
        if callable(mark):
            mark(
                "codex/responses returned 401 twice in one turn; the freshly "
                "rotated bearer was also rejected"
            )

    def _mark_codex_auth_trusted(self) -> None:
        """Clear the distrust latch once a codex request succeeded."""
        mark = getattr(self._codex_auth, "mark_trusted", None)
        if callable(mark):
            mark()

    async def _raise_for_status(self, response: Any, *, codex: bool = False, fconv: bool = False) -> None:
        status = getattr(response, "status_code", 200)
        if status < 400:
            return
        # LIMIT-SIGNATURE-TAXONOMY (2026-08-26): classify the failure body
        # BEFORE choosing an exception, so the global rate-limit breaker can
        # only ever be fed a genuine quota verdict.  A Cloudflare/Turnstile
        # interstitial on ANY status (403/503 — occasionally even a 429
        # envelope) routes to typed challenge recovery instead of
        # RateLimited; an undecipherable body keeps the legacy mapping below,
        # none of which trips the breaker.  A bare 401 is always an auth
        # problem and skips this gate entirely so the round-13 semantics are
        # untouched.
        if status != 401:
            sample = await self._peek_body_sample(response)
            signal = classify_limit_signal(status, sample)
            if signal is LimitSignal.CHALLENGE:
                kind = classify_http_challenge(status, sample)
                logger.warning(
                    "HTTP %s carried an anti-bot challenge page (%s), not a "
                    "quota verdict; raising ChallengeDetectedError so the "
                    "rate-limit breaker is never fed a false trip.",
                    status,
                    kind.value,
                )
                raise ChallengeDetectedError(
                    "ChatGPT served an anti-bot challenge page instead of an "
                    "API verdict; the only recovery is a real-browser "
                    "clearance re-mint, not a rate-limit cooldown.",
                    kind=kind,
                    url=self.conversation_url,
                    status_code=status,
                )
        if status in {401, 403}:
            # Invalidate what this request actually authenticated with: the
            # legacy path pins a minted sentinel trio, while the codex and
            # authed-fconv envelopes carry the Bearer token + cookie jar
            # (review round 10; fconv added in review round 12).
            if codex or fconv:
                self._invalidate_access_credentials()
            else:
                self._invalidate_sentinel_cache()
            raise AuthRequired(f"ChatGPT hybrid request was rejected ({status}).")
        if status == 429:
            raise RateLimited("ChatGPT hybrid request was rate limited.")
        raise ProtocolChanged(f"ChatGPT hybrid request failed with HTTP {status}.")

    async def _stream_sse(
        self,
        response: Any,
        request: SendRequest,
        *,
        on_delta: Callable[[str, str], Awaitable[None] | None] | None = None,
        envelope_headers: dict[str, str] | None = None,
    ) -> TurnResult:
        """Consume the classic/v1 conversation SSE stream into the delta flow.

        ``envelope_headers`` (FCONV-RESUME-HANDOFF) is the turn's fconv
        envelope handed in by ``send`` so a split stream can be followed via
        POST /f/conversation/resume; it is used ONLY when WEBGPT_FCONV_RESUME
        is on and a ``resume_conversation_token`` was captured — every other
        path ignores it entirely.
        """
        decoder = SSEDecoder()
        text = ""
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        conversation_id = request.conversation_id
        model = request.model.label if request.model else None
        complete = False
        # MODEL-ROUTING-PHASE1: the label seeds ``model`` above, so only a
        # value CHANGE proves the server actually published a model_slug.
        requested_slug = _upstream_fconv_model(request)
        served_slug_seen = False
        # LIVE-F4: number of characters of ``text`` already pushed through
        # on_delta.  The noise-prefix cut may remove already-counted chars,
        # so deltas are derived from this pointer instead of trusting the
        # parser's raw append length.
        emitted_upto = 0
        prefix_pending = CurlCffiTransport._flag_enabled(_STRIP_PREFIX_FLAG)
        # FCONV-RESUME-HANDOFF: only an opted-in turn gets a capture dict, so
        # the OFF path parses byte-for-byte identically (the event is still
        # dropped, just silently as before).
        resume_capture: dict[str, str] | None = (
            {} if CurlCffiTransport._fconv_resume_enabled() else None
        )

        def absorb(record: str) -> str:
            """Consume one record, apply hygiene, return the emitted delta."""
            nonlocal text, turn_id, conversation_id, model, complete, emitted_upto, prefix_pending
            nonlocal served_slug_seen
            previous_model = model
            text, turn_id, conversation_id, model, is_complete, _ = self._consume_record(
                record, text, turn_id, conversation_id, model, capture=resume_capture
            )
            if model != previous_model:
                # A server-side model_slug/resolved_model_slug was captured.
                served_slug_seen = True
            if prefix_pending and text:
                stripped, prefix_pending = CurlCffiTransport._strip_leading_noise(text)
                if stripped != text:
                    removed = len(text) - len(stripped)
                    text = stripped
                    emitted_upto = max(emitted_upto - removed, 0)
            if len(text) > emitted_upto:
                delta = text[emitted_upto:]
                emitted_upto = len(text)
            else:
                delta = ""
                emitted_upto = min(emitted_upto, len(text))
            complete = complete or is_complete
            return delta

        async for chunk in self._response_chunks(response):
            for record in decoder.feed(chunk):
                delta = absorb(record)
                if delta and on_delta is not None:
                    callback_result = on_delta(delta, turn_id)
                    if inspect.isawaitable(callback_result):
                        await callback_result
        for record in decoder.finish():
            delta = absorb(record)
            if delta and on_delta is not None:
                callback_result = on_delta(delta, turn_id)
                if inspect.isawaitable(callback_result):
                    await callback_result
        # FCONV-RESUME-HANDOFF: "[DONE] but a resume_conversation_token was
        # still captured" is the server saying this long stream was split.
        # Follow the handoff endpoint with the SAME decoder (continuity —
        # resumed frames patch onto the existing message tree, never suppress)
        # and the SAME absorb closure, so appended deltas keep their arrival
        # order.  Never raises into the turn: the text already streamed stands.
        hops = 0
        last_resume_token: str | None = None
        if resume_capture is not None and envelope_headers:
            # ``envelope_headers`` doubles as the fconv-context marker: the
            # handoff must ride the turn's real envelope (cookies/Bearer/
            # trace id), so a captured token on any other path stays inert.
            while complete and hops < _FCONV_RESUME_MAX_FOLLOWS:
                token = resume_capture.pop("token", "")
                if not token:
                    break
                last_resume_token = token
                hop_conversation = (
                    resume_capture.get("conversation_id") or conversation_id
                )
                try:
                    outcome = await self._follow_fconv_resume_segment(
                        token=token,
                        conversation_id=hop_conversation,
                        base_headers=envelope_headers,
                        request=request,
                        decoder=decoder,
                        absorb=absorb,
                        on_delta=on_delta,
                        turn_id=turn_id,
                    )
                except Exception as exc:  # a failed handoff never kills the turn
                    logger.warning(
                        "FCONV-RESUME handoff raised (%s); keeping the %d "
                        "segment(s) already streamed.",
                        exc,
                        hops + 1,
                    )
                    break
                hops += 1
                if outcome != "ok":
                    break
                nxt = resume_capture.get("token")
                if not nxt or nxt == token:
                    # The segment carried no new handle — or repeated the same
                    # one (gptweb2api's token-loop guard): stream is whole.
                    break
        metadata: dict[str, Any] = {}
        if hops:
            metadata["fconv_resume"] = {
                "hops": hops,
                "conversation_id": conversation_id,
                "token": last_resume_token,
            }
        if not complete and not text:
            raise ProtocolChanged("Conversation stream ended without an assistant response.")
        # MODEL-ROUTING-PHASE2: compute the downgrade verdict once so the
        # WARNING and the per-request TurnResult telemetry stay in lockstep.
        downgraded = bool(
            requested_slug
            and served_slug_seen
            and model
            and model.strip().casefold() != requested_slug.strip().casefold()
        )
        if downgraded:
            # Silent server-side downgrade (research §B3): the stream resolved
            # a different slug than we asked for.  Operational signal only —
            # log + telemetry fields, never fail the turn mid-stream.
            logger.warning(
                "MODEL-ROUTING mismatch: requested model=%r but ChatGPT served "
                "model_slug=%r (conversation=%s); possible silent downgrade.",
                requested_slug,
                model,
                conversation_id,
            )
        return TurnResult(
            turn_id=turn_id,
            conversation_id=conversation_id,
            text=text,
            model=model,
            status="completed" if complete or text else "failed",
            requested_model=requested_slug,
            resolved_model=model if served_slug_seen and model else None,
            model_downgraded=downgraded,
            model_downgrade_count=1 if downgraded else 0,
            metadata=metadata,
        )

    async def _follow_fconv_resume_segment(
        self,
        *,
        token: str,
        conversation_id: str | None,
        base_headers: dict[str, str] | None,
        request: SendRequest,
        decoder: SSEDecoder,
        absorb: Callable[[str], str],
        on_delta: Callable[[str, str], Awaitable[None] | None] | None,
        turn_id: str,
    ) -> str:
        return await _fconv_follow_resume_segment(
            token=token,
            conversation_id=conversation_id,
            base_headers=base_headers,
            request=request,
            decoder=decoder,
            absorb=absorb,
            on_delta=on_delta,
            turn_id=turn_id,
            conversation_url=self.conversation_url,
            post_conversation=self._post_conversation,
            response_chunks=self._response_chunks,
            close_quietly=self._close_quietly,
        )

    async def _stream_codex_sse(
        self,
        response: Any,
        request: SendRequest,
        *,
        on_delta: Callable[[str, str], Awaitable[None] | None] | None = None,
    ) -> TurnResult:
        """Consume a Responses-API SSE stream into the transport delta flow.

        Same SSEDecoder/on_delta/emitted_upto frame as ``_stream_sse``; only
        the record consumer differs (``_consume_codex_record``).  Each
        ``response.output_text.delta`` event therefore pushes exactly its own
        ``delta`` through ``on_delta``, in arrival order.

        Deltas that arrive BEFORE any lifecycle event (``response.created``
        et al.) are buffered, not emitted: at that point only a random
        placeholder turn_id exists and callbacks can never be retroactively
        corrected once the real response id shows up.  The held text stays in
        ``text`` (so TurnResult never loses it) and flushes through on_delta
        under the authoritative id as soon as one arrives; if no lifecycle
        event ever arrives the text is still delivered via the final result.
        """
        decoder = SSEDecoder()
        text = ""
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        complete = False
        emitted_upto = 0
        turn_known = False

        def absorb(record: str) -> str:
            nonlocal text, turn_id, complete, emitted_upto, turn_known
            previous_turn = turn_id
            text, turn_id, is_complete, _ = self._consume_codex_record(record, text, turn_id)
            complete = complete or is_complete
            if turn_id != previous_turn:
                # A created/in_progress/completed event revealed the real id.
                turn_known = True
            if not turn_known:
                # Hold pre-created deltas under the placeholder id; they are
                # already accumulated in ``text`` and flush once the real id
                # lands (emitted_upto is still 0).
                return ""
            if len(text) > emitted_upto:
                delta = text[emitted_upto:]
                emitted_upto = len(text)
            else:
                delta = ""
                emitted_upto = min(emitted_upto, len(text))
            return delta

        async for chunk in self._response_chunks(response):
            for record in decoder.feed(chunk):
                delta = absorb(record)
                if delta and on_delta is not None:
                    callback_result = on_delta(delta, turn_id)
                    if inspect.isawaitable(callback_result):
                        await callback_result
        for record in decoder.finish():
            delta = absorb(record)
            if delta and on_delta is not None:
                callback_result = on_delta(delta, turn_id)
                if inspect.isawaitable(callback_result):
                    await callback_result
        if not complete and not text:
            raise ProtocolChanged("Codex responses stream ended without an assistant response.")
        requested_id = request.model.id if request.model else None
        route = _model_route_for(requested_id)
        return TurnResult(
            turn_id=turn_id,
            conversation_id=request.conversation_id,
            text=text,
            model=request.model.label if request.model else None,
            status="completed" if complete or text else "failed",
            # MODEL-ROUTING-PHASE1 telemetry: this endpoint's SSE records do
            # not expose a served model slug yet, so only the requested side
            # is recorded here (no mismatch verdict possible).
            requested_model=route.slug if route is not None else requested_id,
        )

    @staticmethod
    def _consume_codex_record(
        record: str,
        text: str,
        turn_id: str,
    ) -> tuple[str, str, bool, str]:
        """Consume one Responses-API SSE record.

        Returns ``(text, turn_id, complete, delta)``.  Text is assembled ONLY
        from ``response.output_text.delta`` events (field ``delta``): the
        completed snapshot's ``output[]`` can arrive empty even though deltas
        already streamed (hermes-agent#5678), so it must never be used as a
        fallback.  ``[DONE]`` does not occur on this endpoint but completes
        harmlessly if seen.
        """
        if record == "[DONE]":
            return text, turn_id, True, ""
        try:
            payload = json.loads(record)
        except json.JSONDecodeError as exc:
            raise ProtocolChanged("Codex responses SSE contained invalid JSON.") from exc
        if not isinstance(payload, dict):
            return text, turn_id, False, ""
        event_type = payload.get("type")
        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                return text + delta, turn_id, False, delta
            return text, turn_id, False, ""
        if event_type in {"response.created", "response.in_progress", "response.completed"}:
            response = payload.get("response")
            response_id = response.get("id") if isinstance(response, dict) else None
            new_turn = response_id if isinstance(response_id, str) and response_id else turn_id
            return text, new_turn, event_type == "response.completed", ""
        if event_type in {"response.failed", "error"} or payload.get("error"):
            raise ProtocolChanged(
                f"Codex responses stream returned an error event ({event_type!r})."
            )
        # Metadata events (output_item.added/.done, content_part.*, function
        # call argument deltas…) carry no user-visible text at v1.
        return text, turn_id, False, ""

    @staticmethod
    async def _response_chunks(response: Any) -> AsyncIterator[bytes | str]:
        for name in ("aiter_bytes", "aiter_content"):
            iterator = getattr(response, name, None)
            if iterator is not None:
                async for chunk in iterator():
                    yield chunk
                return
        lines = getattr(response, "aiter_lines", None)
        if lines is None:
            raise ProtocolChanged("curl_cffi response does not expose a streaming iterator.")
        async for line in lines():
            yield f"{line}\n\n" if isinstance(line, str) else line + b"\n\n"

    @staticmethod
    def _flag_enabled(name: str) -> bool:
        """Read a hygiene kill switch; flags default on, ``0`` disables."""
        return os.environ.get(name, "1") != "0"

    @staticmethod
    def _codex_sse_enabled() -> bool:
        """Whether the authenticated codex/responses branch is opted in.

        Deliberately default-OFF (unlike the hygiene flags above): the path
        still awaits one successful live POST verification with a real Plus
        account, and flipping it on must never disturb a running gateway
        until then.  Same truthy set as the local-mock opt-in.
        """
        return os.environ.get(_CODEX_SSE_FLAG, "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _fconv_resume_enabled() -> bool:
        return _fconv_resume_enabled_impl()

    @staticmethod
    def _collapse_duplicate(new_text: str, old_text: str) -> str:
        return _stream_collapse_duplicate(new_text, old_text)

    @classmethod
    def _merge_candidate(cls, text: str, candidate: str) -> tuple[str, str]:
        return _stream_merge_candidate(text, candidate)

    @staticmethod
    def _strip_leading_noise(text: str) -> tuple[str, bool]:
        return _stream_strip_leading_noise(text)

    @staticmethod
    def _consume_record(
        record: str,
        text: str,
        turn_id: str,
        conversation_id: str | None,
        model: str | None,
        capture: dict[str, str] | None = None,
    ) -> tuple[str, str, str | None, str | None, bool, str]:
        return _stream_consume_record(
            record, text, turn_id, conversation_id, model, capture=capture
        )

    @classmethod
    def _consume_v1_record(
        cls,
        payload: dict[str, Any],
        text: str,
        turn_id: str,
        conversation_id: str | None,
        model: str | None,
        capture: dict[str, str] | None = None,
    ) -> tuple[str, str, str | None, str | None, bool, str]:
        return _stream_consume_v1_record(
            payload, text, turn_id, conversation_id, model, capture=capture
        )

    @staticmethod
    def _consume_v1_message(
        message: Any,
        text: str,
        turn_id: str,
        conversation_id: str | None,
        model: str | None,
    ) -> tuple[str, str, str | None, str | None, bool, str]:
        return _stream_consume_v1_message(
            message, text, turn_id, conversation_id, model
        )

    async def close(self) -> None:
        close = getattr(self._session, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


__all__ = [
    "CLOAKBROWSER_USER_AGENT",
    "CODEX_RESPONSES_URL",
    "CONVERSATION_URL",
    "IMPERSONATE_TARGET",
    "_CODEX_VERSION",
    "_FCONV_PREPARE_NOTOKEN",
    "_FCONV_PREPARE_URL",
    "_FCONV_RESUME_FLAG",
    "_FCONV_RESUME_MAX_FOLLOWS",
    "_FCONV_RESUME_OFFSETS",
    "_SENTINEL_CLASSIC_URL",
    "_SENTINEL_PREPARE_URL",
    "_WEBGPT_IMAGE_MARKER_RE",
    "CurlCffiTransport",
    "ModelRoute",
    "parse_model_alias_env",
    "parse_model_fallback_env",
]
