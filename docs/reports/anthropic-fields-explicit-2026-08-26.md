# ANTHROPIC-FIELDS-EXPLICIT — 2026-08-26

Row S từ PARITY-DELTA-AUDIT: `parse_anthropic_request` âm thầm bỏ qua/biến đổi
một số field mà API Anthropic thật xử lý rõ ràng. Scope file:
`gpt/api/protocol_adapters.py`, `tests/test_protocol_adapters.py`.

## Verdict từng điểm (BƯỚC 1 verify + BƯỚC 2 fix)

### 1. `stop_sequences` — CONFIRMED silent-drop → FIXED (400 envelope)

Verify: field không được đọc bất kỳ đâu trong `parse_anthropic_request`; không
được copy vào synthetic request → client (Claude Code) tưởng stop sequences có
hiệu lực trong khi backend không hề thấy.

Fix: non-list → 400 "stop_sequences must be an array."; non-empty list → 400
"stop_sequences is not supported by this gateway yet; the request is rejected
instead of silently ignoring it...". Empty array `[]` vẫn accept (no-op, đúng
ngữ nghĩa API thật). 400 đi qua `RequestValidationError` → handler `/v1/messages`
chuyển thành envelope chuẩn `{"type":"error","error":{"type":"invalid_request_error",...}}`
(không cần sửa server.py — đường ống sẵn có).

### 2. `thinking` enabled — CONFIRMED silent-drop → FIXED (400, có ngoại lệ adaptive)

Fix: chỉ `type="enabled"` → 400 "extended thinking ... is not supported by this
gateway yet; set thinking.type='disabled' or omit the field."
Malformed (non-dict / type không phải string) → 400 validation.

**Phát hiện quan trọng khi regression**: fixture client thật
(`tests/fixtures/clients/claude-code/{simple,stream}.json`) cho thấy Claude Code
hiện tại gửi `"thinking": {"type": "adaptive", "display": "omitted"}` trên MỌI
request. Phiên bản đầu reject mọi type != "disabled" làm fail 2 test fixture.
Chốt: `adaptive` (và type lạ khác) → accept-and-ignore kèm debug log
"anthropic thinking accepted-and-ignored type=..." — explicit nhưng không phá
client production. Chỉ `enabled` (ý định bật extended thinking rõ ràng) mới 400.

### 3. `metadata` — CONFIRMED silent-drop → FIXED (accept-and-ignore + debug log)

Fix: metadata present (dict hoặc kiểu lạ) → accept, không crash, debug log
`anthropic metadata accepted-and-ignored keys=[...]` (hoặc type=... nếu non-dict).

### 4. Block `document` (PDF) — CONFIRMED silent-drop → FIXED (placeholder)

Verify: `_block_sequence_text` chỉ xử lý `image` và `text`; document góp phần
rỗng → PDF biến mất khỏi prompt.

Fix: helper `_document_placeholder()` local trong protocol_adapters.py theo
pattern image placeholder sáng nay: `[document omitted: <title>, <mime>]`,
mime-only `[document omitted: application/pdf]`, không có gì `[document omitted:
unknown]`. Áp dụng cho cả user block array lẫn `tool_result` block array
(cùng `_block_sequence_text`). Payload base64 bị vứt ở ingress, không decode.

Gating rollback: dùng chung kill-switch `WEBGPT_IMAGE_PLACEHOLDER=0`
(`image_placeholder_enabled()` từ `gpt.promptcompat`) — tắt switch thì document
về lại silent-drop y như image. Lý do gate chung: một lever rollback duy nhất
cho cả nhóm unsupported-block marker; đã ghi chú trong code.

## Files

- `gpt/api/protocol_adapters.py` — import logging + `image_placeholder_enabled`;
  `_document_placeholder()`; nhánh document trong `_block_sequence_text`;
  section ANTHROPIC-FIELDS-EXPLICIT trong `parse_anthropic_request`.
- `tests/test_protocol_adapters.py` — 8 test mới.

## Tests mới (8)

1. `test_anthropic_stop_sequences_non_empty_rejected_with_envelope` — /v1/messages → 400 envelope chuẩn, message nêu stop_sequences + not supported.
2. `test_anthropic_stop_sequences_parser_rejects_but_empty_array_passes`
3. `test_anthropic_thinking_enabled_rejected_with_envelope` — 400 envelope, message nêu thinking.
4. `test_anthropic_thinking_disabled_and_absent_accepted`
5. `test_anthropic_thinking_adaptive_accepted_like_current_client` — guard regression fixture thật.
6. `test_anthropic_metadata_accept_and_ignore_logged` — caplog DEBUG, dict + non-dict đều tolerate.
7. `test_anthropic_document_block_becomes_placeholder_marker` — title+mime, mime-only, unknown, tool_result.
8. `test_anthropic_document_kill_switch_drops_silently`

## Kết quả chạy

- `.venv/bin/python -m pytest tests/test_protocol_adapters.py -q`
  → `29 passed in 0.39s` (21 cũ + 8 mới, toàn file xanh)
- Regression set (api_server, claude_code_conformance, count_tokens_align,
  client_fixtures, claude_bootstrap_full_tools, delta_tooluse_and_handshake,
  stream_close_and_crash, gateway_agent_loop + protocol_adapters)
  → `211 passed in 3.02s`
- `ruff check` trên 2 file → `All checks passed!`
- `mypy gpt/api_protocol_adapters.py` → 0 error thuộc module này
  (23 error pre-existing ở file khác, đúng baseline trước thay đổi).

## Out of scope ghi nhận

Fixture còn chứa `context_management` / `output_config` — chưa nằm trong row S,
hiện silent-ignore. Nếu cần explicit hóa tiếp, đề xuất row riêng.
