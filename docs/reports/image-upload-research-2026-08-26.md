# IMAGE-UPLOAD-WEB — Research: upload ảnh lên ChatGPT Web qua HTTP

Ngày: 2026-08-26 · READ-ONLY research cho roadmap row IMAGE-UPLOAD-WEB (L)
Phạm vi: recipe upload file/ảnh qua `/backend-api/files` + cách nhúng vào conversation payload; đối chiếu transport sẵn có.

## 1. Kết luận

**KHẢ THI — độ tin cậy CAO.** 3 nguồn độc lập (2 Go + 1 TypeScript, đều chưa archived,
push trong vòng 6 tháng gần nhất) mô tả cùng một recipe 3 bước, byte-level khớp nhau:

| Nguồn | Ngữ cảnh | Push | Stars |
|---|---|---|---|
| `5yu4n/gptweb2api` (`internal/chatgpt/files_upload.go`, `client.go`) | Go reverse-API — chính là dự án repo này đã port sentinel/fconv recipe từ đó | 2026-08-24 | 4 |
| `aurorax-neo/chat2api` (`app/service/chat_vision.go`) | Go chat2api có vision | 2026-07-21 | 696 |
| `chathub-dev/chathub` (`src/app/bots/chatgpt-webapp/client.ts`, `index.ts`) | TS extension (10k stars) — recipe giống hệt từ 2024 đến nay | 2026-02-27 | 10 647 |

Recipe ổn định ≥ 2 năm (cùng shape xuất hiện ở các project 2023–2026). Lưu ý ngày nguồn:
recipe ChatGPT hay hỏng theo thời gian, nhưng 3/3 nguồn còn sống và mới xác nhận shape này.

## 2. Recipe nguyên văn

### Bước 1 — tạo file record

```
POST https://chatgpt.com/backend-api/files
Content-Type: application/json
Authorization: Bearer <access_token>
Cookie: <full jar incl. cf_clearance>
ChatGPT-Account-ID: <account uuid>        # optional; gptweb2api set khi có
OAI-Device-Id / OAI-Language / OAI-Session-Id / User-Agent / Origin / Referer
                                          # KHÔNG cần OpenAI-Sentinel-* hay turnstile
Body:
{
  "file_name": "image.png",
  "file_size": 123456,
  "use_case": "multimodal"
  // chat2api gửi thêm "width", "height" (pixel) — optional
}
Response 200:
{ "file_id": "file-XXX", "upload_url": "https://...azure.../...", "status": ... }
```
(gptweb2api đọc `file_id` hoặc `id`; chathub check `status == "success"`.)

### Bước 2 — PUT bytes lên blob storage

```
PUT <upload_url>          # host Azure blob, KHÔNG qua Cloudflare của chatgpt.com
Content-Type: image/png   # mime thật của file
x-ms-blob-type: BlockBlob
x-ms-version: 2020-04-08  # chat2api + chathub gửi; gptweb2api không gửi vẫn chạy
Body: raw bytes
→ 201 Created
```

### Bước 3 — finalize

```
POST https://chatgpt.com/backend-api/files/{file_id}/uploaded
Content-Type: application/json
(headers như bước 1)
Body: {}
Response: { "status": "success" }         # các giá trị ready khác: processed/complete;
                                          # nếu "processing" → poll GET /backend-api/files/{file_id}
                                          # mỗi ~750ms, timeout 15s (gptweb2api)
```

### Bước 4 — nhúng vào conversation payload (POST /backend-api/f/conversation)

Message user cuối chuyển thành multimodal:

```json
{
  "id": "<uuid>",
  "author": {"role": "user"},
  "content": {
    "content_type": "multimodal_text",
    "parts": [
      "text prompt...",
      {
        "content_type": "image_asset_pointer",
        "asset_pointer": "file-service://<file_id>",
        "size_bytes": 123456,
        "width": 1024,            // omit nếu không decode được
        "height": 768,
        "fovea": null,
        "metadata": {"dalle": null, "gizmo": null}
      }
    ]
  },
  "metadata": {
    "attachments": [
      {"id": "<file_id>", "name": "image.png", "size": 123456, "mime_type": "image/png",
       "width": 1024, "height": 768}   // width/height chỉ chat2api ghi
    ]
  }
}
```

Hạn chế theo source code: tối đa **10 attachments/turn** (gptweb2api), ceiling bytes
512 MiB (guard nội bộ), ảnh phải decode được pixel size thì mới gửi width/height.
Chỉ áp dụng cho message user MỚI NHẤT (chat2api bỏ ảnh ở history turns — replay
history chỉ giữ text). Upload yêu cầu account auth (chat2api fail-fast khi thiếu).

## 3. Đối chiếu transport sẵn có (repo READ-ONLY)

Đọc `gpt/transport/curl_transport.py` + `token_manager.py`:

- **TokenBundle đủ mọi thứ**: `access_token` ✓, full cookie jar gồm `cf_clearance` ✓,
  `oai_device_id` ✓, `chatgpt_account_id` ✓ (chỉ khi extraction xác định được).
  Không thiếu credential nào cho cả 3 endpoint upload.
- **Session TLS**: `AsyncSession(impersonate="chrome146")` — cùng fingerprint với
  cf_clearance mint → gọi `/backend-api/files` thẳng được, không cần browser.
  PUT blob ra host Azure nên không đụng Cloudflare.
- **Helper sẵn**: `_post_json()` (JSON POST trên shared session) + pattern
  `_integrity_headers()` — thêm `upload_image()` là việc cục bộ.
- **Ingress đã có marker**: `gpt.api.server` encode ảnh thành
  `<WEBGPT_IMAGE_DATA mime=...>base64</WEBGPT_IMAGE_DATA>` (CODEX-IMG-INPUT);
  web path hiện `_strip_image_markers()` degrade thành `[image omitted]`.
  Implement = thay chỗ degrade bằng detect-marker → upload → build parts.
- **Không cần sentinel** cho upload (gptweb2api webHeaders không mang Sentinel-*),
  nhưng POST conversation kèm ảnh vẫn đi đường fconv hiện tại (giữ nguyên envelope).
- Codex branch (`WEBGPT_CODEX_SSE`) đã nhận `input_image` data URL — parity ảnh
  tồn tại sẵn ở codex; row này dành cho đường fconv/web thuần.

## 4. Row ROADMAP đề xuất

**IMAGE-UPLOAD-WEB (S-M — hạ từ L): scope hẹp hơn dự kiến vì ingress/marker/envelope đã có sẵn.**

- Scope implement S-M:
  - Module mới `gpt/transport/file_upload.py`: `upload_image(name, mime, data) -> file_id`
    (3 bước trên, poll processing, cap 10 ảnh/turn + `WEBGPT_UPLOAD_MAX_BYTES` default ~20 MB).
  - `curl_transport.py` đường fconv: detect `<WEBGPT_IMAGE_DATA>` markers → decode b64 →
    upload → build `multimodal_text` parts + `metadata.attachments` (chỉ message user cuối).
  - Chỉ bật khi bundle authed thật (không local-mock); fail-open về `[image omitted]` khi
    upload lỗi (không chết turn).
- Vùng file: `gpt/transport/curl_transport.py` (+~120 dòng), `gpt/transport/token_manager.py`
  (0 — bundle đủ), module mới `file_upload.py` (~200 dòng), `tests/test_session.py` hoặc
  test mới (mock session), `.env.example`.
- Flag env gợi ý: `WEBGPT_IMAGE_UPLOAD_WEB` (default **OFF**, opt-in như
  `WEBGPT_FCONV_PREPARE`), `WEBGPT_UPLOAD_MAX_BYTES`.
- Verify gate trước ON mặc định: 1 live probe PNG nhỏ qua account Plus thật, kiểm tra
  model mô tả đúng nội dung ảnh (không placeholder).
- Rủi ro:
  1. Endpoint drift — recipe ổn định 2023→2026 nhưng `use_case`/`uploaded` shape có thể đổi;
     guard bằng classify response + kill-switch flag.
  2. Ảnh upload gắn account — dùng chung account gateway; không có evidence cho
     `/backend-anon` (anon gần như chắc chắn không có files API).
  3. Upload làm tăng latency turn (~2 RTT + poll ≤15s worst-case); chỉ upload ảnh của
     message cuối, cache file_id theo hash bytes để tránh re-upload giữa các turn replay.
  4. cf_clearance/TLS mismatch trên POST /backend-api/files nếu UA env drift — tái dùng
     `_envelope_user_agent()` như envelope hiện tại.

## 5. Trích dẫn nguồn

- https://github.com/5yu4n/gptweb2api — internal/chatgpt/files_upload.go, images_direct.go, client.go (fetch 2026-08-26)
- https://github.com/aurorax-neo/chat2api — app/service/chat_vision.go (fetch 2026-08-26)
- https://github.com/chathub-dev/chathub — src/app/bots/chatgpt-webapp/{client,index}.ts (fetch 2026-08-26)
