# PNG-UPLOAD-LIVEPROBE — Live verify IMAGE-UPLOAD-WEB

Ngày: 2026-08-26 · Scope: điều kiện bật `WEBGPT_IMAGE_UPLOAD_WEB` (spec: `image-upload-web-research/impl-2026-08-26.md`)
Verdict: **FAIL — KHÔNG bật flag**. Pipeline upload KHÔNG BAO GIỜ chạy trên đường gateway thật. Root cause đã xác định byte-level.

## Setup

Instance test `:18001` (hybrid + headless + `--allow-authenticated`), env `WEBGPT_FCONV_PREPARE=1 WEBGPT_IMAGE_UPLOAD_WEB=1 WEBGPT_RUNTIME_ROOT=/tmp/pngprobe-runtime`, profile copy `personal` (đã xoá Singleton*). Ảnh test: PNG 64×64 solid tím `#7A3FF2`, 136 bytes, `sha256 9179629d205b883b…` (`~/Downloads/pngprobe-work/probe.png`). Gửi `/v1/messages` non-stream, text chứa marker `<WEBGPT_IMAGE_DATA mime="image/png">…</WEBGPT_IMAGE_DATA>` (marker là cách duy nhất `_upload_turn_images` nhận diện ảnh — image block Anthropic bị strip base64 ở ingress theo thiết kế `ANTHROPIC-INGRESS-IMAGE`). 2 turn live (budget ≤3).

## Kết quả từng bước

| Bước | Kết quả | Bằng chứng |
|---|---|---|
| POST /backend-api/files | **KHÔNG xảy ra** | strace connect() đầy đủ cả 2 turn: chỉ có chatgpt.com edge (172.64.155.209 / 104.18.32.47 + AAAA) và pypi/DNS; không một kết nối nào tới Azure/blob. Server-side `GET /backend-api/conversation/{id}` (cred từ token cache profile copy): user message `content_type:"text"`, **không** `image_asset_pointer` |
| PUT Azure | **KHÔNG xảy ra** | như trên |
| /uploaded | **KHÔNG xảy ra** | như trên |
| Model mô tả đúng ảnh | **FAIL** | Turn 1 trả lời **"xám"** (SAI — ảnh tím). Turn 2 trả lời "Tím" nhưng ground-truth server-side chứng minh KHÔNG có ảnh đính kèm ⇒ đáp án đúng là model đoán/suy từ chuỗi base64 nhìn thấy dưới dạng text (không ổn định giữa các turn). Log server 0 warning "Web image upload failed", 0 cache-hit |

Không crash, không lỗi nguyên văn nào ở pipeline — nó đơn giản là **không được gọi**, im lặng đúng cơ chế fail-open.

## Root cause (byte-level)

Prompt-debug dump thực tế (`promptdbg/000003_*_pre_gpt.txt`), nguyên văn context marker:

```
gi? Tra loi dung 1 tu.\n<WEBGPT_IMAGE_DATA mime=\"image/png\">iVBORw0KGgoAAAANSUhEUg…
```

Marker tới transport đã bị escape `<`→`<` và `"`→`\"`. Regex trigger `_WEBGPT_IMAGE_MARKER_RE` (`gpt/transport/curl_transport.py:123`) đòi literal `<WEBGPT_IMAGE_DATA mime="mime">` ⇒ không bao giờ khớp ⇒ `_upload_turn_images` return None âm thầm.

Nguồn escape: `gpt/utils/promptcompat.py:691-696`

```python
message_payload: dict[str, Any] = {"content": message.content}
encoded = json.dumps(message_payload, ensure_ascii=False).replace("<", "\\u003c")
```

Đây là thiết kế chống giả mạo block của protocol WEBGPT_MESSAGE (`_segment` docstring: "JSON-encoded with `<` escaped, so the literal markers can only occur at block boundaries") — mâu thuẫn trực tiếp với hợp đồng in-text marker của IMAGE-UPLOAD-WEB. 22 unit test pass vì inject marker thẳng vào `SendRequest.text`, bỏ qua tầng render.

Phát hiện phụ: (a) response text gateway trả về gồm toàn bộ prompt bootstrap + answer nối đuôi nhau trong khi assistant message thật server-side chỉ là `['Tím']` — artifact tách text phía gateway, riêng biệt với upload; (b) đường codex `/v1/responses` không qua JSON-block encode nên CODEX-IMG-INPUT vẫn sống — chỉ nhánh fconv bị cụt.

## Khuyến nghị

1. **Giữ `WEBGPT_IMAGE_UPLOAD_WEB=OFF`.** Bật lúc này zero giá trị: pipeline unreachable qua mọi ingress thật.
2. Fix thuộc owner (không thuộc scope probe): chuyển ảnh ra out-of-band (field riêng trên `SendRequest` thay vì in-text marker) HOẶC render-layer emit pointer trực tiếp. Sau fix phải rerun probe này (recipe sẵn dùng).
3. Cleanup xong: instance kill, `pngprobe-profile` + `/tmp/pngprobe-runtime` đã xoá; production `:18000` không bị đụng (healthz 200, Singleton gốc nguyên vẹn). Evidence giữ tại `~/Downloads/pngprobe-work/` (req/resp/strace/server.log/probe.png).
