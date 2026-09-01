# ANTHROPIC-INGRESS-IMAGE — Mở đường placeholder ảnh qua /v1/messages

Ngày: 2026-08-26 · Status: DONE · ROADMAP ref: ANTHROPIC-INGRESS-IMAGE (gap của P1-2A)

## Gap đã xác nhận

`parse_anthropic_request` (`gpt/api/protocol_adapters.py`) strip block `type=image`
ở ingress: `_text_blocks(content, {"text"})` chỉ giữ block `text`, nên render layer
(`gpt/utils/promptcompat.py content_text()` — P1-2A) không bao giờ thấy image block
trên đường `/v1/messages` thật của Claude CLI → placeholder chưa từng bắn.

## Thay đổi (chỉ `gpt/api/protocol_adapters.py`)

- Helper mới `_block_sequence_text()`: đi bộ block array **theo thứ tự gốc**;
  block `text` giữ nguyên văn; block `image` đóng góp marker qua chính
  `content_text([block])` của P1-2A → tái dùng toàn bộ placeholder format
  (`[image omitted: <mime> ~<KB>KB — image upload not supported yet]`) và kill
  switch `WEBGPT_IMAGE_PLACEHOLDER=0` (off → im lặng drop như cũ), không nhân
  bản logic. Text-only payload byte-identical với `_text_blocks` cũ.
- Áp dụng cho (1) message content role user, (2) `tool_result` content dạng list
  (screenshot tool của Claude CLI trả ảnh ở đây — cùng class silent-drop).
  Assistant/system giữ nguyên `_text_blocks` cũ.
- Base64 payload bị vứt tại biên giới này — chỉ metadata marker chảy xuống;
  không decode, không upload, không fetch URL nguồn.
- Size dùng estimator chung `_base64_size_kb` (len×3//4, ceil KB) thay vì ÷1.37
  như đề xuất task — một nguồn duy nhất khớp với render layer, tránh hai công thức
  lệch nhau cho cùng một marker.
- Image-only user message (không text): trước đây rơi ra lỗi
  "messages must contain supported content"; giờ (kill switch ON) thành marker,
  kill switch OFF tái hiện đúng hành vi cũ kể cả việc raise.

## Kiểm tra đường OpenAI

`/v1/chat/completions`: `parse_chat_completion_request` (`gpt/requests.py`) không
đụng `content` — pass-through nguyên block list → `image_url` tới thẳng
`content_text` và thành placeholder bình thường (khớp test 6 của P1-2A).
**Không đụng.** Ghi chú ngoài scope: đường `/v1/responses` vẫn drop
`input_image` (chưa ai yêu cầu, Claude CLI không dùng).

## Tests (`tests/test_protocol_adapters.py`, +6 mới)

1. Image base64 trong user message → parsed content chứa marker đúng mime+size
   (~5KB), rendered prompt có `[image omitted: image/png ~5KB`.
2. Text-only blocks → output byte-identical như trước (`"a\nb"`, rendered so
   khớp 1:1).
3. Kill switch `WEBGPT_IMAGE_PLACEHOLDER=0` → silent drop trở lại (có text: chỉ
   text; image-only: raise như hành vi cũ).
4. `tool_result` list content chứa image → marker trong tool message.
5. Image `source.type=url` → marker `[image omitted: unknown …]`, URL không bao
   giờ bị fetch (assert `"example.com"` không xuất hiện trong prompt).
6. Nhiều image xen kẽ text → thứ tự block gốc được giữ.

Kết quả targeted: `.venv/bin/python -m pytest tests/test_protocol_adapters.py -q`
→ **21 passed** (15 nguyên văn giữ trọn vẹn + 6 mới). Kề bên:
`+ tests/test_image_placeholder.py tests/test_prompt_budget.py tests/test_api_server.py
tests/test_gateway_agent_loop.py` → **109 passed in 2.78s**.
`py_compile` OK; ruff/mypy không có trong `.venv` (giống ghi nhận P1-2A).

## Lưu ý vận hành

Không commit, không restart gateway (theo quy tắc task); chỉ đụng 2 file được phép.
