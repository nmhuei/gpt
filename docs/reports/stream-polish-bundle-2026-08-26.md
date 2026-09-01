# STREAM-POLISH-BUNDLE — 2026-08-26

Gói 4 row S từ `docs/reports/parity-delta-audit-2026-08-26.md` (mục 4). Làm tuần tự
trong cùng file set: `gpt/gateway/server.py`, `gpt/api/server.py`, tests. Không đụng
runtime.py / curl_transport / toolcall / protocol_adapters / utils (nên không thêm
flag mới vào class `RateLimited` — xem row 3). Không commit, không restart gateway.

## Verdict từng row

| Row | Verdict | Ghi chú |
|---|---|---|
| PING-WIRE | **DONE** | `": ping\n\n"` → `_sse_event("ping", {"type":"ping"})` ở cả hai server (gateway `_anthropic_live_stream` ~:1681, api ~:1555 sau shift). Heartbeat giờ là frame Anthropic chuẩn, vẫn bị chặn sau terminator (LIVE-F1 giữ nguyên). |
| JSON-DELTA-CHUNK | **DONE** | `partial_json` chunk 512 ký tự (`_JSON_DELTA_CHUNK_CHARS`) trong gateway `_anthropic_block_events` và api `_anthropic_content_events`. Audit chỉ liệt kê gateway nhưng pattern burst đơn tồn tại song song ở cả hai — mirror sang api để giữ parity. Concat các miếng == JSON cũ đúng từng byte; input nhỏ vẫn 1 frame. |
| OVERLOADED-529 | **DONE** | Helper `_is_overloaded_rate_limit()` (cả 2 server): nhận diện qua attribute `overloaded=True` trên exception HOẶC message marker ("overloaded", "over capacity", "high demand", "server is busy"). `_map_exception` trả 529 trước mapping generic; `_anthropic_error` thêm `529: (529, "overloaded_error")`. RateLimited thường (anonymous quota...) giữ nguyên 429/rate_limit_error. Vì cấm sửa `utils/state.py` nên flag là attribute gán tại raise-site/marker message — chưa có raise site nào phát overload hôm nay, path sẽ sống khi transport bắt đầu surface tín hiệu overload. |
| HEADER-PARITY | **DONE** | Inject trong `_RequestTraceMiddleware.traced_send` của cả hai server cho mọi response `/v1/*`: `request-id` (echo uuid trace nội bộ `req_*`, bỏ qua nếu app đã set) + advisory tĩnh `anthropic-ratelimit-requests-limit: 100` / `-remaining` (100 khi breaker closed, 0 khi open/half-open) / `-reset` (`Ns` ceil từ cooldown còn lại) derive từ snapshot `global_rate_limit_breaker()`. Breaker lỗi → fallback coi như closed, không bao giờ làm hỏng response. |

## Điểm cần biết

- Line number trong audit lệch so với working tree (ping thực ra :1655/:1529 lúc bắt đầu) — đã verify theo nội dung, không theo số dòng.
- Vùng newly-merged được giữ nguyên vẹn: late-fail `_anthropic_stream_error_event`, counter `late_failure_masked`, STREAM-CORRECT-DEDUP remainder reconciliation — chỉ đọc, không sửa logic.
- Quan sát ngoài phạm vi (không đổi): `BackendCoolingDown` (breaker mở) hiện rơi xuống nhánh 500 `internal_error` vì không có trong `_map_exception`; nếu muốn 529 luôn cho trạng thái này thì là row riêng (chạm runtime/factory flow).
- `x-should-retry` cho 529 giữ "false" như 429 hiện hành; SDK Anthropic tự retry với >=500 nên CLI vẫn backoff đúng.

## Tests

- File mới `tests/test_stream_polish.py`: **8 test** (2 ping, 3 json-chunk, 2 overload-529, 1 header-parity end-to-end qua httpx ASGITransport trên cả hai app, có trip/reset global breaker an toàn).
- Cập nhật assertion ping cũ cho format mới: `tests/test_stream_close_and_crash.py` (3 chỗ) + `tests/test_api_server.py` (1 chỗ).
- Targeted runs theo từng row đều pass; chạy gộp 9 suite kề bên:
  `test_stream_polish + test_stream_close_and_crash + test_api_server + test_claude_code_conformance + test_messages + test_delta_tooluse_and_handshake + test_gateway_agent_loop + test_backoff_breaker + test_fault_injection`
  = **123 passed, 0 failed**.
- `py_compile` OK trên cả 3 file. ruff/mypy không có trong `.venv` hiện tại (module not found) — bỏ qua bước này, không cài thêm.
