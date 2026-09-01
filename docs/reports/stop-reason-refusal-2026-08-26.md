# STOP-REASON-REFUSAL — row M, parity-delta-audit 2026-08-26

Ngày: 2026-08-26 · Scope: `gpt/gateway/runtime.py`, `gpt/gateway/server.py`, `gpt/api/server.py`, tests.

## 1. Verify hiện trạng (Bước 1)

Đúng như audit mô tả, có một điểm audit ghi không chính xác:

- **Raise points** (`gpt/gateway/runtime.py`): mọi terminal refusal đều `raise MalformedToolCall`
  tại 5 điểm: `:2031` breaker trip false-completion livelock, `:2096` correction budget
  exhausted (mọi reason), `:2117` persistent soft refusal (trace
  `persistent_tool_refusal`), `:2141` persistent hard TOOL_REFUSAL/MALFORMED_TOOL
  (trace `persistent_tool_failure`), `:2203` anti-repeat "not converging".
- **Mapping**: `_map_exception` ở cả hai server (`gpt/gateway/server.py:2546`,
  `gpt/api/server.py:2317`) map `MalformedToolCall → 502 malformed_model_tool_call`;
  envelope Anthropic `_anthropic_error` đổi thành `502 api_error`. Grep 0 hit
  `stop_reason:"refusal"` trong production → CLI chỉ thấy step-fail.
- **Phân biệt đã tốt sẵn**: lỗi hạ tầng KHÔNG đi qua các raise trên —
  `RateLimited → 429` (+ phát hiện overload theo fragment/flag → `529 overloaded_error`),
  `AnonymousSessionUnavailable/AuthRequired → 503`, `GenerationTimeout → 504`.
  Điểm audit ghi "breaker đã trip" là chưa chuẩn: breaker (`:2031`) trip với MỌI
  reason lặp lại (kể cả FALSE_COMPLETION/MALFORMED_TOOL) nên **không phải refusal
  xác định** → giữ nguyên 502.

## 2. Thiết kế chọn

Exception subclass thay vì đổi mapping theo message-string:

```python
class ModelRefusalError(MalformedToolCall):  # gpt/gateway/runtime.py
```

- Subclass ⇒ mọi guard/handler `except MalformedToolCall` hiện hành không đổi;
  OpenAI endpoint vẫn rơi vào nhánh `MalformedToolCall` → 502 (wire OpenAI không có
  stop_reason refusal).
- Convert có điều kiện theo reason, chỉ khi refusal **xác định**:
  - `:2117` persistent soft refusal → luôn `ModelRefusalError`;
  - `:2141` persistent hard → `ModelRefusalError` khi `reason == "TOOL_REFUSAL"`,
    MALFORMED_TOOL giữ nguyên;
  - `:2096` budget exhausted → convert khi `reason.startswith("TOOL_REFUSAL")`.
- Giữ nguyên 502: breaker trip `:2031`, anti-repeat `:2203`, mọi reason
  FALSE_COMPLETION/MALFORMED_TOOL.
- **Anthropic boundary** (`/v1/messages`, cả hai server): helper mới
  `_anthropic_refusal_response()` trả HTTP 200 envelope message hoàn chỉnh
  (`stop_reason:"refusal"`, 1 text block `[webgpt-gateway:model_refusal] <lý do>`,
  usage chars/4 chuẩn `anthropic_usage`). Non-stream: except branch riêng trước
  `except Exception`, log `error_code:"model_refusal"`. Stream
  (`_anthropic_live_stream`): đóng như turn HOÀN THÀNH — content block text +
  `message_delta` stop_reason `"refusal"` + `message_stop`, không bao giờ
  `event: error` (R4-DOUBLING: completed turn là kết quả duy nhất client không retry);
  nếu text đã stream thì nối `\n\n` vào cùng block index 0.
- Vùng cấm không đụng: `_single_noop_invoke`, `_fresh_tool_conversation`,
  `_soft_handshake_overhead_chars`, khối :1744–1796, correction counter :2184,
  curl_transport/toolcall/protocol_adapters/accounts/conftest.

## 3. Files

| File | Thay đổi |
|---|---|
| `gpt/gateway/runtime.py` | +class `ModelRefusalError`; 3 raise point chuyển có điều kiện |
| `gpt/gateway/server.py` | import + `_anthropic_refusal_response` + except branch non-stream + stream close branch |
| `gpt/api/server.py` | mirror y hệt gateway |
| `tests/test_stop_reason_refusal.py` | mới, 11 test |

## 4. Tests

11 test mới (file riêng): 4 runtime (hard-persistent, soft-persistent,
budget-exhausted refusal, **regression** malformed giữ `MalformedToolCall` thuần),
5 Anthropic boundary (200+refusal non-stream; stream refusal trước/sau content;
**regression** malformed→502 api_error; **regression bắt buộc** RateLimited→429 /
overloaded→529), 2 biên giới (gateway parity; **OpenAI giữ 502**).

Kết quả: targeted run `test_stop_reason_refusal + test_api_server +
test_correction_tighten + test_prose_correction_live + test_refusal_detection +
test_gateway_agent_loop + test_stream_correct_dedup + test_delta_tooluse_and_handshake +
test_fault_injection` = **142 passed**, 0 failed. Không commit, không restart service.

## 5. Ghi chú

- `mypy`/`ruff` không có trong `.venv` (CLAUDE.md ghi nhưng module thiếu); dùng
  ruff standalone: 2 file touched của tôi sạch. 3 warning còn lại (2 RUF001 dấu
  nháy cong runtime.py:258, 1 I001 thứ tự import gateway/server.py:66) là WIP
  tồn tại trước can thiệp, thuộc vùng người khác đang làm — không đụng.
- Chưa đóng kèm row này: `stop_reason:"max_tokens"` (G5 nửa còn lại) và
  STREAM-CORRECT-DEDUP.
