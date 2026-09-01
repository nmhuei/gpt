"""ChatGPT web file upload — the /backend-api/files three-step pipeline.

IMAGE-UPLOAD-WEB (2026-08-26, spec:
docs/reports/image-upload-research-2026-08-26.md).  Recipe, byte-identical
across gptweb2api / chat2api / chathub and stable 2023 → 2026:

1. ``POST /backend-api/files`` body ``{file_name, file_size,
   use_case: "multimodal"}`` → ``{file_id, upload_url}``.
2. ``PUT upload_url`` with the raw bytes plus Azure blob headers
   (``x-ms-blob-type: BlockBlob``, ``x-ms-version``) — the host is Azure,
   outside the chatgpt.com Cloudflare perimeter.
3. ``POST /backend-api/files/{file_id}/uploaded`` body ``{}``; a
   ``processing`` status is polled on ``GET /backend-api/files/{file_id}``.

Every failure raises an :class:`ImageUploadError` subclass; the transport
fail-opens to its placeholder behavior so a turn never dies because an
upload did.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any

logger = logging.getLogger("gpt.transport.file_upload")

FILES_URL = "https://chatgpt.com/backend-api/files"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_IMAGES_PER_TURN = 10
_POLL_INTERVAL_S = 0.75
_POLL_TIMEOUT_S = 15.0
_BLOB_API_VERSION = "2020-04-08"
_READY_STATUSES = frozenset({"success", "processed", "complete"})

_IMAGE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/heic": "heic",
    "image/heif": "heif",
}


class ImageUploadError(Exception):
    """Base for every web-upload failure; callers must fail open."""


class FileRecordRejectedError(ImageUploadError):
    """Step 1 failed: no usable {file_id, upload_url} record came back."""


class BlobUploadFailedError(ImageUploadError):
    """Step 2 failed: Azure blob storage rejected the raw image bytes."""


class FinalizeFailedError(ImageUploadError):
    """Step 3 failed: the file never reached a ready state in time."""


def default_image_name(mime: str) -> str:
    """Derive a stable upload name from a mime type (markers carry none)."""
    return f"image.{_IMAGE_EXTENSIONS.get(mime.casefold(), 'bin')}"


def probe_dimensions(data: bytes) -> tuple[int, int] | None:
    """Best-effort pixel size from PNG/GIF/JPEG headers; None when unknown.

    The recipe sends width/height only when decodable (chat2api behavior);
    undecodable formats simply omit the keys.
    """
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            return (
                int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"),
            )
        if data[:4] == b"GIF8" and len(data) >= 10:
            return (
                int.from_bytes(data[6:8], "little"),
                int.from_bytes(data[8:10], "little"),
            )
        return _probe_jpeg_dimensions(data)
    except Exception:  # malformed headers must never break an upload
        return None


def _probe_jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if data[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            offset += 2  # standalone markers carry no length segment
            continue
        segment_len = int.from_bytes(data[offset + 2 : offset + 4], "big")
        if segment_len < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[offset + 5 : offset + 7], "big")
            width = int.from_bytes(data[offset + 7 : offset + 9], "big")
            return width, height
        offset += 2 + segment_len
    return None


class WebFileUploader:
    """Three-step upload against one shared impersonated session.

    ``headers_factory`` supplies fresh backend-api headers per call (the
    credential snapshot may rotate between turns); the Azure PUT builds its
    own minimal header set.  Successful uploads are cached by content hash so
    replayed history never re-uploads the same bytes.
    """

    def __init__(
        self,
        session: Any,
        headers_factory: Callable[[], Mapping[str, str]],
        *,
        timeout: float | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        cache: MutableMapping[str, str] | None = None,
        poll_interval_s: float = _POLL_INTERVAL_S,
        poll_timeout_s: float = _POLL_TIMEOUT_S,
    ) -> None:
        self._session = session
        self._headers_factory = headers_factory
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._cache: MutableMapping[str, str] = {} if cache is None else cache
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s

    async def upload_image(self, data: bytes, mime: str, name: str | None = None) -> str:
        """Upload one image; returns its file-service id.

        Raises an :class:`ImageUploadError` subclass on any failure; nothing
        is cached unless all three steps succeeded.
        """
        key = hashlib.sha256(data).hexdigest()
        cached = self._cache.get(key)
        if cached:
            logger.debug("Web image cache hit (%s…); skipping upload.", key[:12])
            return cached
        size = len(data)
        if size <= 0:
            raise ImageUploadError("Refusing to upload an empty image body.")
        if size > self._max_bytes:
            raise ImageUploadError(
                f"Image of {size} bytes exceeds the {self._max_bytes}-byte cap."
            )
        if not name:
            name = default_image_name(mime)
        try:
            file_id, upload_url = await self._create_file_record(name, size)
            await self._put_blob(upload_url, data, mime)
            await self._finalize_upload(file_id)
        except ImageUploadError:
            raise
        except Exception as exc:  # defensive: session/network surprises too
            raise ImageUploadError(f"Unexpected web-upload failure: {exc}") from exc
        self._cache[key] = file_id
        return file_id

    async def _create_file_record(self, name: str, size: int) -> tuple[str, str]:
        body = {"file_name": name, "file_size": size, "use_case": "multimodal"}
        response = await self._perform(
            "post", FILES_URL, json=body, headers=dict(self._headers())
        )
        try:
            status = getattr(response, "status_code", None)
            envelope = await self._parsed_json(response)
        finally:
            await self._close_quietly(response)
        if status is None or not 200 <= status < 400 or not isinstance(envelope, dict):
            raise FileRecordRejectedError(
                f"File record creation failed with HTTP {status}."
            )
        file_id = envelope.get("file_id") or envelope.get("id")
        upload_url = envelope.get("upload_url")
        if not isinstance(file_id, str) or not file_id:
            raise FileRecordRejectedError("File record carries no file_id/id.")
        if not isinstance(upload_url, str) or not upload_url:
            raise FileRecordRejectedError("File record carries no upload_url.")
        return file_id, upload_url

    async def _put_blob(self, upload_url: str, data: bytes, mime: str) -> None:
        headers = {
            "Content-Type": mime or "application/octet-stream",
            "x-ms-blob-type": "BlockBlob",
            "x-ms-version": _BLOB_API_VERSION,
        }
        response = await self._perform("put", upload_url, data=data, headers=headers)
        try:
            status = getattr(response, "status_code", None)
        finally:
            await self._close_quietly(response)
        if status is None or not 200 <= status < 300:
            raise BlobUploadFailedError(
                f"Azure blob storage rejected the image bytes (HTTP {status})."
            )

    async def _finalize_upload(self, file_id: str) -> None:
        uploaded_url = f"{FILES_URL}/{file_id}/uploaded"
        response = await self._perform(
            "post", uploaded_url, json={}, headers=dict(self._headers())
        )
        try:
            status = getattr(response, "status_code", None)
            envelope = await self._parsed_json(response)
        finally:
            await self._close_quietly(response)
        if status is None or not 200 <= status < 400:
            raise FinalizeFailedError(f"Finalize call failed with HTTP {status}.")
        state = self._status_of(envelope)
        if state in _READY_STATUSES:
            return
        deadline = time.monotonic() + self.poll_timeout_s
        detail_url = f"{FILES_URL}/{file_id}"
        while True:
            if time.monotonic() >= deadline:
                raise FinalizeFailedError(
                    f"File {file_id} still not ready after "
                    f"{self.poll_timeout_s:.0f}s (last status: {state!r})."
                )
            await asyncio.sleep(self.poll_interval_s)
            poll = await self._perform("get", detail_url, headers=dict(self._headers()))
            try:
                http_status = getattr(poll, "status_code", None)
                envelope = await self._parsed_json(poll)
            finally:
                await self._close_quietly(poll)
            if http_status is not None and 200 <= http_status < 400:
                state = self._status_of(envelope)
                if state in _READY_STATUSES:
                    return

    @staticmethod
    def _status_of(envelope: Any) -> str:
        if isinstance(envelope, dict):
            value = envelope.get("status")
            if isinstance(value, str):
                return value.casefold()
        return ""

    def _headers(self) -> Mapping[str, str]:
        return self._headers_factory()

    async def _perform(self, method: str, url: str, **kwargs: Any) -> Any:
        request = getattr(self._session, method)
        return await request(url, timeout=self._timeout, **kwargs)

    @staticmethod
    async def _parsed_json(response: Any) -> Any:
        reader = getattr(response, "json", None)
        if not callable(reader):
            return None
        try:
            parsed = reader()
            return await parsed if inspect.isawaitable(parsed) else parsed
        except Exception:
            return None

    @staticmethod
    async def _close_quietly(response: Any) -> None:
        close = getattr(response, "aclose", None) or getattr(response, "close", None)
        if close is None:
            return
        try:
            closed = close()
            if inspect.isawaitable(closed):
                await closed
        except Exception as exc:
            logger.debug("Could not close upload response cleanly: %s", exc)
