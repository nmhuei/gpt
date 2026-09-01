# IMAGE-UPLOAD-WEB — Implementation report

Ngày: 2026-08-26 · Spec: `docs/reports/image-upload-research-2026-08-26.md`
Trạng thái: **DONE (flag OFF mặc định)** — chưa live-verify, không bật env, không commit.

## Files

| File | Thay đổi |
|---|---|
| `gpt/transport/file_upload.py` | MỚI (~250 dòng): pipeline 3 bước + cache + exceptions |
| `gpt/transport/curl_transport.py` | Chỉ vùng fconv payload/image: flag, `_maybe_build_multimodal_payload`, `_upload_turn_images`, `_collect_image_assets`, `_file_upload_headers`, `_multimodal_parts_and_attachments`, `_build_conversation_payload(request, image_assets=None)` |
| `tests/test_file_upload.py` | MỚI: 22 test fake-HTTP |
| `.env.example` | KHÔNG đụng (ngoài phạm vi cho phép) |

## Implement đúng spec research

- Bước 1 `POST /backend-api/files` body `{file_name,file_size,use_case:"multimodal"}` → đọc `file_id|id` + `upload_url`; thiếu → `FileRecordRejectedError`.
- Bước 2 `PUT upload_url` raw bytes + `Content-Type` + `x-ms-blob-type: BlockBlob` + `x-ms-version: 2020-04-08`; non-2xx → `BlobUploadFailedError`.
- Bước 3 `POST /backend-api/files/{id}/uploaded` `{}`; `success|processed|complete` là xong, `processing` → poll `GET /backend-api/files/{id}` mỗi 0.75s timeout 15s → quá hạn `FinalizeFailedError`.
- Nhúng payload: message user cuối → `multimodal_text`, parts xen kẽ text + `image_asset_pointer` (`asset_pointer:"file-service://<id>"`, `size_bytes`, `width/height` khi decode được PNG/GIF/JPEG, `fovea:null`, `metadata:{dalle:null,gizmo:null}`) + `metadata.attachments`.
- Cache `sha256(bytes)→file_id` trên transport instance (`_web_image_cache`) — turn replay không re-upload, cache hit bỏ cả PUT.
- Cap 10 ảnh/turn (lấy đầu tiên); b64 >20MB hoặc decoded >`WEBGPT_UPLOAD_MAX_BYTES` (default 20MB) → skip.
- Fail-open: flag off / không marker / thiếu access token / b64 lỗi / mọi `ImageUploadError` (kể cả exception bất ngờ từ session) → giữ nguyên payload placeholder `[image omitted: mime]` như cũ. Upload chỉ chạy nhánh fconv (`WEBGPT_FCONV_PREPARE=1`); codex branch không đụng.
- Headers upload: Bearer + cookie jar (cf_clearance) + OAI-Device-Id + UA pin + ChatGPT-Account-ID (nếu bundle có); KHÔNG sentinel.

## Test (22 passed)

Happy path 3 bước (body/headers PUT Azure khớp recipe); cache hit bỏ toàn bộ HTTP kể cả PUT; cache qua dict inject được; fail từng bước → exception riêng đúng loại; processing→poll→processed; poll timeout; wrap exception lạ; empty/oversize refuse trước HTTP; probe PNG/GIF/JPEG/unknown; name mapping; cap 12→10 (spans đầu); b64 padding lỗi skip; integration send() fconv: multimodal parts + attachments đủ trường; envelope credentials; cache hit xuyên turn; upload fail → payload byte-identical legacy; flag off → zero HTTP upload + payload khớp builder cũ; partial fail trộn pointer + note; overflow >10 thành notes.

## Điểm rủi ro còn lại

1. **Chưa live-verify**: recipe lấy từ 3 nguồn OSS; cần 1 live probe PNG nhỏ qua account Plus thật (kiểm tra model mô tả đúng ảnh) trước khi bật ON mặc định.
2. Endpoint drift (`use_case`/shape `/uploaded`) — guard bằng classify response + kill-switch `WEBGPT_IMAGE_UPLOAD_WEB`.
3. Latency turn +2 RTT (+poll ≤15s worst-case) khi có ảnh mới.
4. Ảnh upload gắn account dùng chung của gateway.
