# CORRECTION-TIGHTEN — siết correction loop + vá instrumentation (2026-08-26)

Row ROADMAP #79 · spec từ trace-forensics-2026-08-25 (Q4/Q5.3/Q5.4): corr=4 tốn
77.9s (~10× turn sạch p50 8s), `request_completed.correction_count` luôn =0 và
`turn_id` null trên 92% rows completed dù correction loop thật sự chạy.

Lưu ý lịch sử: một instance trước crash máy đã kịp merge phần code (STATE.md tick
"Trước crash đã kịp merge: CORRECTION-TIGHTEN"); dispatch này nghiệm thu lại toàn
bộ diff, chạy đủ targeted + kề bên + evals, và chốt report. Không đụng
`gpt/api/protocol_adapters.py`, `gpt/utils/assistantturn.py`,
`gpt/transport/codex_auth.py`; không restart gateway; không commit.

## Spec áp dụng

1. **Layered budget (cap 2 cho protocol-shaped):** `_PROTOCOL_SHAPED_CORRECTION_REASONS`
   = {MALFORMED_TOOL, INVALID_WRITE, MULTI_TOOL} có sub-budget cứng
   `_PROTOCOL_SHAPED_MAX_CORRECTIONS = 2`, tính bằng
   `min(WEBGPT_MAX_CORRECTIONS, 2)` — kể cả khi operator bật env lên 4, vòng
   MALFORMED_TOOL dừng ở đúng 2 corrections. Prose/refusal-shaped
   (`TOOL_REFUSAL`, `TOOL_REFUSAL_SOFT`, `FALSE_COMPLETION`) giữ trọn budget env.
   Vượt cap → fail-fast: trace `correction_budget_exhausted` (reason, class,
   used/cap, max_corrections, correction_count, turn_id) rồi raise
   `MalformedToolCall("Tool correction budget exhausted (... protocol_shaped 2/2)…")`.
2. **Anti-repeat hint-once:** mỗi base correction prompt được băm SHA-256. Nếu
   prompt mới giống hệt lần gửi trước (malformation y hệt) → KHÔNG resend
   byte-identical; lần 1 append escalation hint
   (`_CONTROLLER_CORRECTION_ESCALATION` / `_SOFT_CORRECTION_ESCALATION` tùy
   protocol) để prompt thật sự đổi; nếu model vẫn trả cùng base correction →
   trace `persistent_correction_repeat` + raise
   `MalformedToolCall("Correction loop not converging: …")` ngay, không đốt thêm send.
3. **Instrumentation:**
   - `runtime.execute_raw_on_session`: `correction_count` / `multi_tool_turns` /
     `last_turn_id` hoisted lên trên try; `last_turn_id = result.turn_id` sau
     MỖI send (initial + correction); `submit_completed` metadata giờ mang
     `correction_count` thật, `multi_tool_turns`, `turn_id`.
   - Đường failure cũng mang đủ: `submit_failed_before_commit_unknown` emit
     `error_type` + `correction_count` + `turn_id` cuối cùng.
   - Event `tool_correction` (1 event / 1 correction prompt được GỬI) enrich:
     `correction_class`, `correction_index`, `effective_cap`,
     `task_context_chars`.
   - Consumer `api/server.py finalize()`: `request_completed.correction_count`
     đếm event `tool_correction` (trước đây là hằng 0); `turn_id` lấy từ
     `submit_completed`, fallback chain sang failure events
     (TURN-ID-FAILURE-TRACE) cho error response không có session header.

## Diff summary

| File | Thay đổi |
|---|---|
| `gpt/gateway/runtime.py` | constants mới (~:984-1008); layered budget + anti-repeat trong correction loop (~:1960-2106); instrumentation hoist + metadata submit_completed/failure (~:1796-1806, :2228-2236, :2195-2208) |
| `gpt/api/server.py` | `finalize()` đếm `tool_correction` + turn_id fallback failure events (~:216-263) |
| `tests/test_correction_tighten.py` | MỚI 364 dòng: 6 test pin cap/hint/instrumentation |
| `evals/goldens/13_correction_budget_protocol_cap.json` | MỚI golden (i) |
| `evals/goldens/14_correction_anti_repeat_escalation.json` | MỚI golden (ii) |

Không hồi quy vùng cấm — xác minh còn nguyên sau khi chạy test:
`_fresh_tool_conversation` (:305/:757/:769), heuristic exemption (~:523),
`_original_task_context` (:388/:1833), CORRECTION breaker markers (41 hit
grep 'breaker|no_op'), markup allow-prose (`allow_prose=` :686), SOFT-FRAMING
region + `_SOFT_FRAMING_TEXT` (:1039). Số dòng lệch so với spec vì diff wave
chung tích lũy, không phải do đụng vào.

## Tests nguyên văn

```
$ .venv/bin/python -m pytest -q tests/test_correction_tighten.py
......                                                                   [100%]
6 passed in 0.25s

$ .venv/bin/python -m pytest -q tests/test_correction_context.py tests/test_gateway_agent_loop.py
.............................                                            [100%]
29 passed in 1.98s

$ .venv/bin/python -m pytest -q tests/test_prose_correction_live.py tests/test_refusal_detection.py tests/test_discover_policy.py
.................................................                        [100%]
49 passed in 0.36s

$ .venv/bin/python -m pytest -q tests/test_api_server.py
..........................................                               [100%]
42 passed in 0.60s

$ .venv/bin/python evals/run_evals.py
EVALS RESULT: total=19 pass=19 xfail=0 skip=0 fail=0
```

Không chạy full suite theo phân công (coordinator lo ranh giới wave); pytest
plain `-q`, không `--timeout`. ruff không có trong `.venv` (module missing) — bỏ qua.

## Quyết định chính

- Interleave shapes trong test cap (MALFORMED → MULTI_TOOL → MALFORMED khác
  nhau) để cả LIVE-R3 identical-reason guard lẫn anti-repeat đều không preempt,
  đảm bảo chính layered budget là thứ kết thúc vòng.
- Anti-repeat so digest **base** correction prompt (chưa gồm escalation) nên
  hint escalation chỉ phát đúng 1 lần rồi fail-fast — không ping-pong hint.
- `persistent_tool_failure` (LIVE-R3, có trước) vẫn precedence cao hơn cap cho
  reason MALFORMED_TOOL lặp y hệt → thực tế còn fail sớm hơn 2; cap 2 là trần,
  không phải đích phải đạt.
