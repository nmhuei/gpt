"""Compatibility facade for the single production gateway implementation.

Historically this repository carried two ~100KB API-server implementations:
``gpt.api.server`` and ``gpt.gateway.server``.  Production has used the latter
for a long time.  Keeping both caused fixes to land in one copy but not the
other, so this module now deliberately delegates to the production server.

Public imports remain stable.  A few private helpers are re-exported because
older tests/debug scripts used them while the duplicate existed.
"""
from __future__ import annotations

import inspect
import re
from typing import Any

from gpt.gateway import http_contract as _http_contract
from gpt.gateway import server as _impl

# Compatibility names.  create_api_app synchronizes the two monkeypatchable
# dependency names before construction, which preserves old fault-injection
# tests that patched gpt.api.server.AccountStore/ChatGPTWebSession.
AccountStore = _impl.AccountStore
ChatGPTWebSession = _impl.ChatGPTWebSession
DEFAULT_RESPONSE_SESSION_CAP = _impl.DEFAULT_RESPONSE_SESSION_CAP
_JSON_DELTA_CHUNK_CHARS = _impl._JSON_DELTA_CHUNK_CHARS

# Legacy Responses/Codex ingress helpers retained as a small compatibility
# layer.  The production gateway no longer needs a duplicate server body, but
# older tests/debug clients still import these private helpers directly.
_CODEX_SSE_FLAG = "WEBGPT_CODEX_SSE"
_CODEX_IMAGE_MARKER_TMPL = (
    '<WEBGPT_IMAGE_DATA mime="{mime}">{data}</WEBGPT_IMAGE_DATA>'
)
_CODEX_IMAGE_MAX_B64_CHARS = 20 * 1024 * 1024
_MIME_RE = re.compile(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+")
_B64_RE = re.compile(r"[A-Za-z0-9+/=]+")


def _codex_image_ingest_enabled() -> bool:
    return _impl._env_flag(_CODEX_SSE_FLAG)


def _inline_image_data(block: dict[str, Any]) -> tuple[str, str] | None:
    source = block.get("source")
    if isinstance(source, dict):
        media_type = source.get("media_type")
        data = source.get("data")
        if (
            source.get("type") == "base64"
            and isinstance(media_type, str)
            and isinstance(data, str)
        ):
            return media_type, data
        return None
    url = block.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if (
        not isinstance(url, str)
        or not url.startswith("data:")
        or ";base64," not in url
    ):
        return None
    header, _, data = url.partition(",")
    mime = header[len("data:") :].split(";", 1)[0]
    return mime, data


def _validated_image_data(mime: str, data: str) -> tuple[str, str] | None:
    mime = mime.strip()
    compact = "".join(data.split())
    if not mime or not compact:
        return None
    if _MIME_RE.fullmatch(mime) is None or _B64_RE.fullmatch(compact) is None:
        return None
    return mime, compact


def _codex_image_replacement(block: dict[str, Any]) -> str:
    extracted = _inline_image_data(block)
    if extracted is None:
        _impl.logger.info(
            "Dropped /v1/responses image without inline data "
            "(remote URLs are not fetched at ingress)."
        )
        return "[image omitted: unsupported image source]"
    validated = _validated_image_data(*extracted)
    if validated is None:
        _impl.logger.info(
            "Dropped /v1/responses image with malformed mime/base64 payload."
        )
        return "[image omitted: malformed image payload]"
    mime, data = validated
    if len(data) > _CODEX_IMAGE_MAX_B64_CHARS:
        _impl.logger.warning(
            "Skipping /v1/responses image (%s, ~%dKB): exceeds the %dMB codex "
            "upload cap; sending an omission note instead.",
            mime,
            len(data) // 1024,
            _CODEX_IMAGE_MAX_B64_CHARS // (1024 * 1024),
        )
        return (
            f"[image omitted: {mime} ~{len(data) // 1024}KB exceeds upload cap]"
        )
    return _CODEX_IMAGE_MARKER_TMPL.format(mime=mime, data=data)


def _inject_codex_image_markers(body: Any) -> None:
    if not _codex_image_ingest_enabled() or not isinstance(body, dict):
        return
    raw_input = body.get("input")
    if not isinstance(raw_input, list):
        return
    for item in raw_input:
        if not isinstance(item, dict) or item.get("role", "user") != "user":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for index, block in enumerate(content):
            if (
                isinstance(block, dict)
                and block.get("type") in {"input_image", "image"}
            ):
                content[index] = {
                    "type": "input_text",
                    "text": _codex_image_replacement(block),
                }


_ProductionWebChatAPIServer = _impl.WebChatAPIServer


class WebChatAPIServer(_ProductionWebChatAPIServer):
    """Compatibility constructor over the production server.

    The historical standalone server did not build a worker pool for the
    default browser/max_workers=1 case; many embedders monkeypatch
    get_or_create_session directly. Preserve only that constructor semantic
    while inheriting every request/runtime implementation from production.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        signature = inspect.signature(_ProductionWebChatAPIServer.__init__)
        bound = signature.bind_partial(None, *args, **kwargs)
        transport = bound.arguments.get("transport", "browser")
        max_workers = int(bound.arguments.get("max_workers", 1))
        account_profiles = bound.arguments.get("account_profiles")
        mock_backend = bool(bound.arguments.get("mock_backend", False))
        super().__init__(*args, **kwargs)
        if (
            not mock_backend
            and not account_profiles
            and transport == "browser"
            and max_workers <= 1
        ):
            self._worker_factory = None

_RequestTraceMiddleware = _impl._RequestTraceMiddleware
_error = _impl._error
_session_headers = _impl._session_headers
_client_name = _impl._client_name
_anthropic_request_headers = _impl._anthropic_request_headers
_sse_event = _impl._sse_event
_is_overloaded_rate_limit = _impl._is_overloaded_rate_limit
_advisory_ratelimit_headers = _http_contract._advisory_ratelimit_headers
_anthropic_error = _impl._anthropic_error
_anthropic_exception_error = _impl._anthropic_exception_error
_anthropic_refusal_response = _impl._anthropic_refusal_response
_messages_prompt_text = _impl._messages_prompt_text
_request_prompt_text = _impl._request_prompt_text
_anthropic_payload_usage = _impl._anthropic_payload_usage
_lifespan = _impl._lifespan


def create_api_app(*args: Any, **kwargs: Any):
    # Keep legacy monkeypatch points functional without maintaining a second
    # server implementation.
    vars(_impl)["AccountStore"] = AccountStore
    vars(_impl)["ChatGPTWebSession"] = ChatGPTWebSession
    original_server = _impl.WebChatAPIServer
    vars(_impl)["WebChatAPIServer"] = WebChatAPIServer
    try:
        return _impl.create_api_app(*args, **kwargs)
    finally:
        vars(_impl)["WebChatAPIServer"] = original_server


__all__ = [
    "DEFAULT_RESPONSE_SESSION_CAP",
    "WebChatAPIServer",
    "create_api_app",
]
