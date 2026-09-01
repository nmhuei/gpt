# LATE-FAIL-SURFACE — 2026-08-26 (row S, PARITY-DELTA-AUDIT)

## Verdict verify: finding CHÍNH XÁC

Đọc `gpt/gateway/server.py` (trước sửa: khối except của `_anthropic_live_stream`
~1712-1739 + `_anthropic_no_retry_close` 1751-1807) và bản mirror y hệt trong
`gpt/api/server.py` (~1448-1473 + ~1485-1533). Cả hai đường lỗi mid-stream đều
đóng stream giả lập hoàn thành (`content_block_stop` → `message_delta(end_turn)`
→ `message_stop`) bất kể `started_content`, kèm lý do dạng text
`[webgpt-gateway:<type>] <message>` — đúng như audit mô tả. Không có case nào
phân biệt pre-content/post-content.

Khóa R4-DOUBLING (ROADMAP row R4, done 2026-08-25) chỉ có ý nghĩa khi nội dung
ĐÃ stream: retry sẽ nhân bản output cục phần. Khi chưa deliver gì, retry chỉ
tạo generation mới sạch — không mất gì. Test khóa cũ
(`tests/test_stream_close_and_crash.py::test_wedged_backend_...`,
`tests/test_api_server.py::test_anthropic_mid_stream_error_closes_without_retry_signal`,
`...::test_anthropic_live_stream_error_reports_output_usage`) đều khóa nhánh
pre-content nên phải cập nhật theo contract mới; test post-content
(`...after_deltas_appends_reason_and_stops`) giữ nguyên hành vi wire.

## Thay đổi

Cả hai file mirror cùng pattern:

1. `gpt/gateway/server.py` + `gpt/api/server.py`:
   - `_ANTHROPIC_STREAM_ERROR_TYPES` (module-level): whitelist type Anthropic
     contract cho SSE error frame.
   - `__init__`: counter `self.late_failure_masked = 0`.
   - Khối except `_anthropic_live_stream`: giữ log `anthropic_live_stream_error`;
     nếu `started_content == False` → yield helper `_anthropic_stream_error_event`
     rồi return (không content block, không terminator, không ping sau error);
     nếu đã stream → `late_failure_masked += 1` + log structured
     `late_failure_masked {...}` + giữ nguyên close sạch R4.
   - Helper mới `_anthropic_stream_error_event(error_payload)`: phát đúng 1 frame
     `event: error` / data `{"type":"error","error":{"type","message"}}`; type
     ngoài contract (vd `generation_timeout`) coerce về `api_error`.

## Files

- `/home/light/GitHub/gpt/gpt/gateway/server.py` (prod)
- `/home/light/GitHub/gpt/gpt/api/server.py` (mirror)
- `/home/light/GitHub/gpt/tests/test_stream_close_and_crash.py` — wedged test
  chuyển sang kỳ vọng error frame; thêm test masked-close + counter.
- `/home/light/GitHub/gpt/tests/test_api_server.py` — 3 test cập nhật, 1 test
  helper coercion mới.
- `/home/light/GitHub/gpt/docs/automation/ROADMAP.md` — row S → done.

Không đụng runtime.py/toolcall.py/curl_transport.py/protocol_adapters.py/utils/*.

## Tests (nguyên văn)

```
tests/test_stream_close_and_crash.py tests/test_claude_code_conformance.py
  23 passed in 0.56s
tests/test_api_server.py
  43 passed in 0.43s
tests/test_gateway_agent_loop.py tests/test_streaming_contract.py
tests/test_messages.py tests/test_delta_tooluse_and_handshake.py
tests/test_stream_hygiene.py
  46 passed in 1.19s
tests/test_usage_estimation.py tests/test_fault_injection.py
  27 passed in 0.21s
```

Tổng 139 passed, 0 failed. py_compile OK cả 4 file. ruff/mypy không cài trong
venv hiện tại (bỏ qua bước lint).

## Ghi chú thiết kế

- Error frame KHÔNG mang usage object (giống Anthropic thật); concern
  PARITY-P0-1 về usage>0 trên error-only turn giờ chỉ áp dụng nhánh
  đã-stream (vẫn được test ở `test_anthropic_live_stream_error_reports_output_usage`
  sau khi chuyển sang kịch bản stream-partial-then-fail).
- Pre-content failure để SDK tự quyết retry (5xx-class) — chấp nhận theo row S:
  generation thay thế bắt đầu sạch, không nhân bản nội dung.
- Counter là per-process in-memory (systemd unit chạy dài → đủ đo tần suất qua
  log scrape; không thêm endpoint/exporter trong phạm vi fix tối thiểu).
