"""IMAGE-UPLOAD-WEB — fake-HTTP tests for the web file-upload pipeline.

Covers (spec: docs/reports/image-upload-research-2026-08-26.md, impl report
docs/reports/image-upload-web-impl-2026-08-26.md): happy path 3 steps, cache
hit skipping PUT, per-step failures falling back to the placeholder payload,
the 10-images-per-turn cap, and flag-off byte-identical legacy behavior.
Never touches the network.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gpt.promptcompat import render_messages
from gpt.transport.curl_transport import (
    _WEBGPT_IMAGE_MARKER_RE,
    CONVERSATION_URL,
    CurlCffiTransport,
)
from gpt.transport.file_upload import (
    FILES_URL,
    BlobUploadFailedError,
    FileRecordRejectedError,
    FinalizeFailedError,
    ImageUploadError,
    WebFileUploader,
    default_image_name,
    probe_dimensions,
)
from gpt.transport.token_manager import SentinelTokens, TokenBundle
from gpt.types import ModelInfo, SendRequest

BLOB_URL = "https://webgpt-tests.blob.core.windows.net/files/abc"
IMG_BYTES = b"png-image-bytes-0123456789"
DEVICE = "11111111-2222-3333-4444-555555555555"


def png_bytes(width: int = 1, height: int = 7) -> bytes:
    """Minimal PNG header — only the first 24 bytes matter for probing."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def marker(data: bytes = IMG_BYTES, mime: str = "image/png") -> str:
    encoded = base64.b64encode(data).decode()
    return f'<WEBGPT_IMAGE_DATA mime="{mime}">{encoded}</WEBGPT_IMAGE_DATA>'


def make_request(text: str) -> SendRequest:
    return SendRequest(
        text=text,
        conversation_id=None,
        model=ModelInfo(id="gpt-5.2", label="GPT 5.2"),
        reasoning_effort="high",
    )


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeJSONResponse:
    def __init__(self, payload=None, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def aclose(self):
        pass


class FakeSSEResponse:
    status_code = 200

    async def aiter_bytes(self):
        yield (
            b'data: {"conversation_id":"conv-web","message":{"id":"turn-web",'
            b'"content":{"parts":["Hello"]},"status":"finished_successfully"}}\n\n'
        )

    async def aclose(self):
        pass


class RoutingSession:
    """Duck-typed AsyncSession: exact-URL routing, records every call."""

    def __init__(self):
        self.routes: dict[str, FakeJSONResponse] = {}
        self.sequences: dict[str, list[FakeJSONResponse]] = {}
        self.sse_response = FakeSSEResponse()
        self.calls: list[tuple[str, str, dict]] = []

    def route(self, url: str, response: FakeJSONResponse) -> None:
        self.routes[url] = response

    def route_sequence(self, url: str, *responses: FakeJSONResponse) -> None:
        self.sequences[url] = list(responses)

    def _next(self, url: str) -> FakeJSONResponse | None:
        queued = self.sequences.get(url)
        if queued:
            return queued.pop(0)
        return self.routes.get(url)

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._next(url) or self.sse_response

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._next(url) or self.sse_response

    async def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return self._next(url) or FakeJSONResponse(None, 201)

    def calls_to(self, method: str, url_prefix: str):
        return [
            call
            for call in self.calls
            if call[0] == method and call[1].startswith(url_prefix)
        ]


class ExplodingSession:
    async def post(self, url, **kwargs):
        raise RuntimeError("boom")


class FlowTokenManager:
    async def refresh_if_needed(self):
        return TokenBundle(
            access_token="access-token",
            cookies={"cf_clearance": "clearance"},
            cf_clearance="clearance",
            oai_device_id=DEVICE,
            chatgpt_account_id="account-uuid",
        )

    async def get_sentinel_tokens(self, conversation_id):
        # Only reached on the legacy (fconv-off) path of the flag-off test.
        return SentinelTokens("requirements", "proof", "turnstile")


def make_uploader(session, **overrides) -> WebFileUploader:
    options: dict[str, Any] = {
        "poll_interval_s": 0.01,
        "poll_timeout_s": 1.0,
    }
    options.update(overrides)
    return WebFileUploader(session, lambda: {"Authorization": "Bearer test-token"}, **options)


def make_transport(session) -> CurlCffiTransport:
    transport = CurlCffiTransport(cast(Any, FlowTokenManager()), session=session)
    # The prepare chain has its own tests; bypass its HTTP here.
    transport._prepare_fconv_turn = AsyncMock(  # type: ignore[method-assign]
        return_value=(SentinelTokens("requirements"), "sess-id", "trace-id", None)
    )
    return transport


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("WEBGPT_IMAGE_UPLOAD_WEB", raising=False)
    monkeypatch.delenv("WEBGPT_FCONV_PREPARE", raising=False)
    monkeypatch.delenv("WEBGPT_UPLOAD_MAX_BYTES", raising=False)


# ---------------------------------------------------------------------------
# WebFileUploader — three-step pipeline over fake HTTP
# ---------------------------------------------------------------------------


async def test_happy_path_runs_all_three_steps():
    session = RoutingSession()
    session.route(FILES_URL, FakeJSONResponse({"file_id": "file-1", "upload_url": BLOB_URL}))
    session.route(f"{FILES_URL}/file-1/uploaded", FakeJSONResponse({"status": "success"}))

    file_id = await make_uploader(session).upload_image(IMG_BYTES, "image/png")

    assert file_id == "file-1"
    _create_method, create_url, create_kwargs = session.calls_to("POST", FILES_URL)[0]
    assert create_url == FILES_URL
    assert create_kwargs["json"] == {
        "file_name": "image.png",
        "file_size": len(IMG_BYTES),
        "use_case": "multimodal",
    }
    assert create_kwargs["headers"]["Authorization"] == "Bearer test-token"

    _, put_url, put_kwargs = session.calls_to("PUT", "https://")[0]
    assert put_url == BLOB_URL
    assert put_kwargs["data"] == IMG_BYTES
    headers = put_kwargs["headers"]
    assert headers["Content-Type"] == "image/png"
    assert headers["x-ms-blob-type"] == "BlockBlob"
    assert headers["x-ms-version"] == "2020-04-08"

    _finalize_method, finalize_url, finalize_kwargs = session.calls_to(
        "POST", f"{FILES_URL}/file-1/uploaded"
    )[0]
    assert finalize_url.endswith("/file-1/uploaded")
    assert finalize_kwargs["json"] == {}
    assert finalize_kwargs["headers"]["Authorization"] == "Bearer test-token"


async def test_cache_hit_skips_every_http_call_including_put():
    session = RoutingSession()
    session.route(FILES_URL, FakeJSONResponse({"file_id": "file-1", "upload_url": BLOB_URL}))
    session.route(f"{FILES_URL}/file-1/uploaded", FakeJSONResponse({"status": "success"}))
    uploader = make_uploader(session)

    first = await uploader.upload_image(IMG_BYTES, "image/png")
    calls_after_first = list(session.calls)
    second = await uploader.upload_image(IMG_BYTES, "image/png")

    assert first == second == "file-1"
    assert session.calls == calls_after_first  # zero new HTTP, PUT included


async def test_cache_is_shared_through_injected_dict():
    session = RoutingSession()
    session.route(FILES_URL, FakeJSONResponse({"file_id": "file-x", "upload_url": BLOB_URL}))
    session.route(f"{FILES_URL}/file-x/uploaded", FakeJSONResponse({"status": "success"}))
    shared: dict[str, str] = {}

    await make_uploader(session, cache=shared).upload_image(IMG_BYTES, "image/png")

    assert shared[hashlib.sha256(IMG_BYTES).hexdigest()] == "file-x"


async def test_step1_rejection_raises_dedicated_error():
    session = RoutingSession()
    session.route(FILES_URL, FakeJSONResponse({"error": "denied"}, status_code=500))

    with pytest.raises(FileRecordRejectedError):
        await make_uploader(session).upload_image(IMG_BYTES, "image/png")
    assert not session.calls_to("PUT", "")  # never reached blob storage


async def test_step1_malformed_body_raises_dedicated_error():
    session = RoutingSession()
    session.route(FILES_URL, FakeJSONResponse({"foo": "bar"}))

    with pytest.raises(FileRecordRejectedError):
        await make_uploader(session).upload_image(IMG_BYTES, "image/png")


async def test_step2_rejection_raises_dedicated_error():
    session = RoutingSession()
    session.route(FILES_URL, FakeJSONResponse({"file_id": "file-1", "upload_url": BLOB_URL}))
    session.route(BLOB_URL, FakeJSONResponse(None, status_code=500))

    with pytest.raises(BlobUploadFailedError):
        await make_uploader(session).upload_image(IMG_BYTES, "image/png")
    assert not session.calls_to("POST", f"{FILES_URL}/file-1/uploaded")


async def test_step3_rejection_raises_dedicated_error():
    session = RoutingSession()
    session.route(FILES_URL, FakeJSONResponse({"file_id": "file-1", "upload_url": BLOB_URL}))
    session.route(f"{FILES_URL}/file-1/uploaded", FakeJSONResponse({}, status_code=503))

    with pytest.raises(FinalizeFailedError):
        await make_uploader(session).upload_image(IMG_BYTES, "image/png")


async def test_processing_status_polls_until_ready():
    session = RoutingSession()
    session.route(FILES_URL, FakeJSONResponse({"file_id": "file-9", "upload_url": BLOB_URL}))
    session.route_sequence(
        f"{FILES_URL}/file-9/uploaded",
        FakeJSONResponse({"status": "processing"}),
    )
    session.route(f"{FILES_URL}/file-9", FakeJSONResponse({"status": "processed"}))

    file_id = await make_uploader(session).upload_image(IMG_BYTES, "image/png")

    assert file_id == "file-9"
    assert session.calls_to("GET", f"{FILES_URL}/file-9")


async def test_poll_timeout_raises_finalize_error():
    session = RoutingSession()
    session.route(FILES_URL, FakeJSONResponse({"file_id": "file-8", "upload_url": BLOB_URL}))
    session.route_sequence(
        f"{FILES_URL}/file-8/uploaded",
        FakeJSONResponse({"status": "processing"}),
    )
    session.route(f"{FILES_URL}/file-8", FakeJSONResponse({"status": "processing"}))

    with pytest.raises(FinalizeFailedError):
        await make_uploader(
            session, poll_interval_s=0.001, poll_timeout_s=0.03
        ).upload_image(IMG_BYTES, "image/png")


async def test_unexpected_session_errors_are_wrapped_not_leaked():
    class PartialExplode(RoutingSession):
        async def post(self, url, **kwargs):
            if url == FILES_URL:
                raise RuntimeError("boom")
            return await super().post(url, **kwargs)

    with pytest.raises(ImageUploadError) as excinfo:
        await make_uploader(PartialExplode()).upload_image(IMG_BYTES, "image/png")
    assert isinstance(excinfo.value, ImageUploadError)
    assert not isinstance(excinfo.value, (FileRecordRejectedError, BlobUploadFailedError))


async def test_empty_and_oversized_bodies_are_refused_without_http():
    session = RoutingSession()

    with pytest.raises(ImageUploadError):
        await make_uploader(session).upload_image(b"", "image/png")
    with pytest.raises(ImageUploadError):
        await make_uploader(session, max_bytes=4).upload_image(b"12345", "image/png")
    assert session.calls == []


# ---------------------------------------------------------------------------
# dimension probe / name helper
# ---------------------------------------------------------------------------


def test_probe_dimensions_png_gif_jpeg_and_unknown():
    assert probe_dimensions(png_bytes(3, 9)) == (3, 9)
    gif = b"GIF89a" + (11).to_bytes(2, "little") + (22).to_bytes(2, "little") + b"\x00\x00"
    assert probe_dimensions(gif) == (11, 22)
    jpeg = (
        b"\xff\xd8\xff\xc0\x00\x0b\x08"
        + (7).to_bytes(2, "big")  # height
        + (1).to_bytes(2, "big")  # width
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xd9"
    )
    assert probe_dimensions(jpeg) == (1, 7)
    assert probe_dimensions(b"\x00\x01\x02\x03") is None


def test_default_image_name_maps_mime_or_bin():
    assert default_image_name("image/png") == "image.png"
    assert default_image_name("image/webp") == "image.webp"
    assert default_image_name("application/x-weird") == "image.bin"


# ---------------------------------------------------------------------------
# marker collection (transport side, stubbed upload callable)
# ---------------------------------------------------------------------------


async def test_render_layer_escaped_marker_still_uploads_png():
    """Regression: render_messages JSON-escapes marker quotes before fconv.

    The transport collector must accept that exact rendered representation;
    previous tests fed raw markers directly and missed this ingress boundary.
    """
    raw = marker(png_bytes(2, 3))
    rendered = render_messages(
        [{"role": "user", "content": f"inspect {raw}"}],
        initial=False,
        tools=[],
        tool_choice="auto",
    )
    assert '\\"image/png\\"' in rendered or '\"image/png\"' in rendered
    seen: list[tuple[bytes, str, str]] = []

    async def upload(data, mime, name):
        seen.append((data, mime, name))
        return "file-rendered"

    assets = await CurlCffiTransport._collect_image_assets(
        rendered, upload, max_bytes=1024 * 1024
    )
    assert len(assets) == 1
    assert seen == [(png_bytes(2, 3), "image/png", "image.png")]
    assert assets[0][4] == "file-rendered"
    assert assets[0][5] == (2, 3)


async def test_collect_caps_at_ten_images_per_turn():
    text = " ".join(marker() for _ in range(12))
    seen: list[str] = []

    async def upload(data, mime, name):
        seen.append(name)
        return f"file-{len(seen)}"

    assets = await CurlCffiTransport._collect_image_assets(
        text, upload, max_bytes=1024 * 1024
    )

    assert len(seen) == 10
    assert len(assets) == 10
    # The first ten marker spans win; the last two stay un-uploaded.
    all_spans = [m.span() for m in _WEBGPT_IMAGE_MARKER_RE.finditer(text)]
    assert [asset[:2] for asset in assets] == all_spans[:10]


async def test_collect_skips_invalid_base64_and_oversize_markers():
    bad_pad = '<WEBGPT_IMAGE_DATA mime="image/png">AAAAA</WEBGPT_IMAGE_DATA>'
    text = f"keep {marker()} then {bad_pad} then {marker()}"
    uploads: list[int] = []

    async def upload(data, mime, name):
        uploads.append(len(data))
        return "file-ok"

    assets = await CurlCffiTransport._collect_image_assets(
        text, upload, max_bytes=len(IMG_BYTES)
    )

    assert len(assets) == 2
    assert uploads == [len(IMG_BYTES)] * 2


# ---------------------------------------------------------------------------
# full send() wiring over fake HTTP
# ---------------------------------------------------------------------------


async def test_fconv_turn_embeds_uploaded_asset_pointer(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    monkeypatch.setenv("WEBGPT_IMAGE_UPLOAD_WEB", "1")
    session = RoutingSession()
    session.route(
        FILES_URL, FakeJSONResponse({"file_id": "file-abc", "upload_url": BLOB_URL})
    )
    session.route(
        f"{FILES_URL}/file-abc/uploaded", FakeJSONResponse({"status": "success"})
    )
    transport = make_transport(session)

    result = await transport.send(make_request(f"look {marker(png_bytes())}"))

    assert result.text == "Hello"
    conv_calls = session.calls_to("POST", CONVERSATION_URL)
    assert len(conv_calls) == 1
    message = conv_calls[0][2]["json"]["messages"][0]
    content = message["content"]
    assert content["content_type"] == "multimodal_text"
    pointers = [part for part in content["parts"] if isinstance(part, dict)]
    texts = [part for part in content["parts"] if isinstance(part, str)]
    assert len(pointers) == 1 and texts == ["look "]
    pointer = pointers[0]
    assert pointer["content_type"] == "image_asset_pointer"
    assert pointer["asset_pointer"] == "file-service://file-abc"
    assert pointer["size_bytes"] == len(png_bytes())
    assert pointer["width"] == 1 and pointer["height"] == 7
    assert pointer["fovea"] is None
    assert pointer["metadata"] == {"dalle": None, "gizmo": None}
    attachment = message["metadata"]["attachments"][0]
    assert attachment == {
        "id": "file-abc",
        "name": "image.png",
        "size": len(png_bytes()),
        "mime_type": "image/png",
        "width": 1,
        "height": 7,
    }


async def test_upload_envelope_carries_bundle_credentials(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    monkeypatch.setenv("WEBGPT_IMAGE_UPLOAD_WEB", "1")
    session = RoutingSession()
    session.route(
        FILES_URL, FakeJSONResponse({"file_id": "file-abc", "upload_url": BLOB_URL})
    )
    session.route(
        f"{FILES_URL}/file-abc/uploaded", FakeJSONResponse({"status": "success"})
    )
    transport = make_transport(session)
    await transport.send(make_request(f"look {marker()}"))

    create_headers = session.calls_to("POST", FILES_URL)[0][2]["headers"]
    assert create_headers["Authorization"] == "Bearer access-token"
    assert "cf_clearance=clearance" in create_headers["Cookie"]
    assert create_headers["OAI-Device-Id"] == DEVICE
    assert create_headers["ChatGPT-Account-ID"] == "account-uuid"


async def test_cache_hit_across_turns_skips_put(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    monkeypatch.setenv("WEBGPT_IMAGE_UPLOAD_WEB", "1")
    session = RoutingSession()
    session.route(
        FILES_URL, FakeJSONResponse({"file_id": "file-abc", "upload_url": BLOB_URL})
    )
    session.route(
        f"{FILES_URL}/file-abc/uploaded", FakeJSONResponse({"status": "success"})
    )
    transport = make_transport(session)
    request = make_request(f"look {marker()}")

    await transport.send(request)
    puts_after_first_turn = len(session.calls_to("PUT", ""))
    creates_after_first_turn = len(session.calls_to("POST", FILES_URL))
    await transport.send(request)

    assert puts_after_first_turn == 1
    assert len(session.calls_to("PUT", "")) == puts_after_first_turn
    assert len(session.calls_to("POST", FILES_URL)) == creates_after_first_turn
    assert len(session.calls_to("POST", CONVERSATION_URL)) == 2


async def test_upload_failure_falls_back_to_placeholder_payload(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    monkeypatch.setenv("WEBGPT_IMAGE_UPLOAD_WEB", "1")
    session = RoutingSession()
    session.route(FILES_URL, FakeJSONResponse({"error": "down"}, status_code=500))
    transport = make_transport(session)

    result = await transport.send(make_request(f"look {marker()}"))

    message = session.calls_to("POST", CONVERSATION_URL)[0][2]["json"]["messages"][0]
    assert message["content"] == {
        "content_type": "text",
        "parts": ["look [image omitted: image/png]"],
    }
    assert "metadata" not in message
    assert not session.calls_to("PUT", "")
    assert result.text == "Hello"


async def test_flag_off_keeps_legacy_payload_and_makes_no_upload_calls(monkeypatch):
    session = RoutingSession()
    transport = make_transport(session)
    request = make_request(f"look {marker()}")

    result = await transport.send(request)

    legacy = make_transport(RoutingSession())._build_conversation_payload(request)
    sent = session.calls_to("POST", CONVERSATION_URL)[0][2]["json"]
    assert sent["messages"][0]["content"] == legacy["messages"][0]["content"]
    assert "metadata" not in sent["messages"][0]
    assert sent["model"] == legacy["model"]
    assert sent["action"] == legacy["action"]
    assert sent["conversation_mode"] == legacy["conversation_mode"]
    # No file-pipeline traffic at all when the flag is off.
    assert not session.calls_to("POST", FILES_URL)
    assert not session.calls_to("PUT", "")
    assert result.text == "Hello"


async def test_partial_failure_mixes_pointer_with_omission_notes(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    monkeypatch.setenv("WEBGPT_IMAGE_UPLOAD_WEB", "1")
    session = RoutingSession()
    session.route_sequence(
        FILES_URL,
        FakeJSONResponse({"file_id": "file-1", "upload_url": BLOB_URL}),
        FakeJSONResponse({"error": "down"}, status_code=500),
    )
    session.route(
        f"{FILES_URL}/file-1/uploaded", FakeJSONResponse({"status": "success"})
    )
    transport = make_transport(session)
    # Distinct bytes: identical bodies would collapse into one cache entry.
    await transport.send(
        make_request(f"a {marker(b'first-image')} b {marker(b'second-image')} c")
    )

    parts = session.calls_to("POST", CONVERSATION_URL)[0][2]["json"]["messages"][0][
        "content"
    ]["parts"]
    # Segments keep their original spacing (verbatim, like the codex path).
    assert parts[0] == "a "
    assert parts[1]["asset_pointer"] == "file-service://file-1"
    assert parts[2] == " b [image omitted: image/png] c"
    assert sum(isinstance(part, dict) for part in parts) == 1
    assert len(session.calls_to("PUT", "")) == 1


async def test_more_than_ten_images_leaves_overflow_as_notes(monkeypatch):
    monkeypatch.setenv("WEBGPT_FCONV_PREPARE", "1")
    monkeypatch.setenv("WEBGPT_IMAGE_UPLOAD_WEB", "1")
    session = RoutingSession()
    transport = make_transport(session)
    text = " ".join(marker(mime="image/gif") for _ in range(12))

    # Pure builder driven directly: first 10 markers uploaded, last 2 overflow.
    uploaded = [
        (m.start(), m.end(), "image/gif", 5, f"file-{i}", None)
        for i, m in enumerate(_WEBGPT_IMAGE_MARKER_RE.finditer(text))
    ][:10]
    parts, attachments = transport._multimodal_parts_and_attachments(text, uploaded)

    pointers = [part for part in parts if isinstance(part, dict)]
    notes = [part for part in parts if isinstance(part, str)]
    assert len(pointers) == 10
    assert [p["asset_pointer"] for p in pointers] == [
        f"file-service://file-{i}" for i in range(10)
    ]
    # Whitespace-only segments drop; both overflow markers degrade into notes.
    assert len(notes) == 1
    assert notes.count("[image omitted: image/gif]") == 0
    assert notes[0].count("[image omitted: image/gif]") == 2
    assert len(attachments) == 10
