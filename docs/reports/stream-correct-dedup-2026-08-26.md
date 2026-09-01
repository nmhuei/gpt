# STREAM-CORRECT-DEDUP — chặn text trùng khi correction giữa stream

- Ngày: 2026-08-26
- Gap: **G1** của `docs/reports/parity-delta-audit-2026-08-26.md` (top-gap #1)
- Phạm vi file: `gpt/gateway/runtime.py`, `gpt/gateway/server.py`, `tests/test_stream_correct_dedup.py` (mới). Không đụng `api/server.py`, `curl_transport.py`, `protocol_adapters.py`, `utils/*`; không commit, không restart gateway, không live turn.
- Vùng cấm FIX-R8B/FIX-B trong runtime.py (`_single_noop_invoke`, `_fresh_tool_conversation`, `_soft_handshake_overhead_chars`, budget reserve ~:1744-1796, breaker markers, markup :665, framing :919-990): **không bị chạm** — diff chỉ gồm import block và `_forward_response_deltas`.

---

## 1. Verify (BƯỚC 1) — repro tái hiện ĐÚNG mô tả audit ⇒ hành vi thật khớp audit

Repro: fake streaming session phát delta như HybridTransport; attempt 1 stream prose FALSE_COMPLETION ("I've created fizzbuzz.py and ran it…") → classifier bắt FALSE_COMPLETION → correction → attempt 2 commit prose khác (task cố tình trung tính để attempt 2 là commit hợp lệ). Quan sát SSE phía server qua `/v1/messages` stream.

Kết quả TRƯỚC fix (test RED, verbatim từ pytest):

```
AssertionError: corrected final text duplicated on the wire:
  count=2
  block0="I've created fizzbuzz.py and ran it. The output is 1 1 2 3 5 8.The repository keeps a flat module structure. Entry points live under gpt/.The repository keeps a flat module structure. Entry points live under gpt/."
assert 2 == 1
```

Cơ chế xác nhận y hệt mục 2c của parity-delta-audit:

1. `event_task` (`CompletionRuntime._forward_response_deltas`) được tạo MỘT lần trước vòng correction (runtime.py ~:1890) và sống suốt vòng lặp ⇒ delta của attempt 2 cũng được forward vào cùng `content_block` index 0.
2. Finalize: payload chỉ chứa text attempt cuối; prefix mismatch với toàn bộ text đã stream ⇒ nhánh else của remainder reconciliation (server.py, vùng ~:1684) re-emit TOÀN BỘ final_text ⇒ text corrected xuất hiện 2 lần trên wire.

Unit-level: forwarder băng qua ranh giới attempt (`received == ['one ', 'two', 'three ', 'four']`) — cùng một cơ chế, không có test nào từng khóa hành vi này trước đây (đúng như audit ghi "chưa có test nào phủ kịch bản này").

## 2. Fix (BƯỚC 2) — ranh giới attempt dùng terminal event có sẵn; reconciliation per-attempt

Chọn phương án 1 của audit ("đánh dấu ranh giới attempt") nhưng KHÔNG cần event type mới: mỗi `send()` đã kết thúc bằng `ResponseCompleted` / `ResponseFailed` trong cùng FIFO queue mà forwarder đọc.

- `runtime.py :: _forward_response_deltas`: dừng forward ngay khi gặp `ResponseCompleted | ResponseFailed`. Sạch theo cấu trúc — FIFO bảo đảm không bao giờ forward delta của attempt sau khi attempt đó đã chấm dứt, bất kể pacing/scheduling; mọi attempt về sau chỉ deliver qua finalized payload. Không đụng vòng correction/budget/breaker.
- `server.py` (vùng remainder reconciliation): thêm comment khóa hợp đồng per-attempt — `emitted` chỉ chứa delta attempt đầu; prefix-match ⇒ chỉ append phần đuôi (không replay byte đã deliver); mismatch ⇒ full finalized text là lần delivery ĐẦU TIÊN trên stream này chứ không phải replay. Hành vi không đổi so với trước ở nhánh này (nhánh else vốn đúng SAU khi runtime chặn cross-attempt delta); cái sai nằm ở runtime và đã được cắt tận gốc.

SSE contract giữ nguyên: `message_start → content_block_start → content_block_delta* → content_block_stop → [block sau text] → message_delta(stop_reason+usage) → message_stop`; ping/deadline/disconnect không đổi.

Hệ quả chấp nhận được (đúng tinh thần "KHÔNG phát lại phần đã deliver"): prose FALSE_COMPLETION của attempt 1 đã stream trước khi gateway biết nó hỏng thì vẫn hiện đúng MỘT lần trên wire — không thể unsend, nhưng không bao giờ bị nhân bản hay replay.

## 3. Tests (BƯỚC 3)

`tests/test_stream_correct_dedup.py` — 4 test, RED trước fix (3 fail), GREEN sau fix:

| Test | Khóa điều gì |
|---|---|
| `test_correction_mid_stream_does_not_duplicate_committed_text` | Repro G1: corrected text xuất hiện đúng 1 lần trong block 0; stale prose ≤ 1 lần; `end_turn` + `message_stop` |
| `test_corrected_tool_use_after_streamed_prose_keeps_sieve_and_blocks_intact` | Correction → `<cmd>` tool_use: đúng 1 tool_use block, `stop_reason:"tool_use"`, tag protocol không leak vào text_delta |
| `test_single_attempt_stream_is_progressive_and_byte_identical` | Regression wire-identity luồng thường 1 attempt: delta == đúng từng chunk gốc, đúng thứ tự, skeleton SSE chuẩn, remainder không cộng thêm byte |
| `test_forward_response_deltas_stops_at_attempt_boundary` | Unit: forwarder dừng tại `ResponseCompleted`/`ResponseFailed`, không forward delta attempt sau |

Fake session mô phỏng pacing thật (yield giữa các emit + emit `ResponseCompleted` cuối mỗi send) — phiên bản fake đồng bộ tuyệt đối khiến `event_task` không kịp schedule và che mất bug; đã sửa trong quá trình repro.

## 4. Chạy kiểm chứng

```
tests/test_stream_correct_dedup.py                                    4 passed
test_delta_tooluse_and_handshake + test_gateway_agent_loop +
test_correction_tighten + test_claude_code_conformance               48 passed
test_messages + test_fault_injection + test_failover + test_session   29 passed
test_api_server                                                       43 passed
```

Tổng 124 passed, targeted only, `.venv/bin/python -m pytest -q` (không --timeout).

## 5. Còn ngỏ (ngoài scope)

- Stale prose attempt 1 vẫn hiển thị 1 lần khi correction xảy ra sau khi đã stream (bất khả kháng trên SSE; muốn sạch phải đánh đổi progressive streaming của happy path).
- Usage estimator đếm cả stale prose đã stream (ước lượng chars/4, lệch nhỏ, cosmetic).
- G2-G10 còn lại của parity-delta-audit không đổi trạng thái.
