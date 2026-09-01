# CODEXFIX-B — runtime.py guard ↔ breaker + prompt budget (2026-08-26)

Phạm vi: codex review #12 finding #1 (High) và #6 (Low) tại `gpt/gateway/runtime.py`.
Quy trình: verify từng finding bằng repro chạy trên cây hiện tại → cả hai ĐÚNG → fix.
Không đụng toolcall.py / curl_transport.py / token_manager.py / api/server.py /
protocol_adapters.py; không commit, không restart gateway.

## Finding 1 (High) — VERDICT: ĐÚNG, đã fix

### Bằng chứng (repro `~/Downloads/codexfix-b/verify_finding1.py`, trước fix)

- Transcript chỉ chứa no-op `Bash{"command":"true"}` + result:
  `_fresh_tool_conversation()=False` vĩnh viễn ⇒ cả hai nhánh FALSE_COMPLETION
  (`_tool_correction_issue` layer-3 action-claim và generic task-directed) trả
  `None` ⇒ skip no-op tại correction loop (armed metronome,
  `correction_skipped_noop_repeat`) là dead-code đúng trong kịch bản RC3 nó sinh ra
  (debug-r8: 30× FALSE_COMPLETION sau các lần commit `<cmd>true</cmd>`).
- Placeholder-only (`"..."`, golden 17) cũng mất guard.
- FIX-R8B vẫn phải giữ: transcript có REAL call (Write với args thật) + prose
  claim ⇒ `None` (golden 18 scenario "stale").

### Thiết kế chọn — freshness theo NỘI DUNG transcript

`_fresh_tool_conversation` (runtime.py :346, helper mới `_single_noop_invoke`
:306): quét toàn bộ `messages+tail`; fresh=True khi và chỉ khi mọi tool activity
đều "provably did nothing":

- mỗi turn tối đa 1 invoke; invoke đó là canonical no-op shell
  (`true`/`:`/`#noop`/`noop` — đồng bộ `_NOOP_SHELL_COMMANDS`) HOẶC placeholder
  body theo `_is_placeholder_command` (golden 17), gồm cả blob arguments phi-object
  kiểu JSON string `"..."`;
- mọi `role:"tool"` result phải map được về một call-id no-op in-transcript;
  result mồ côi/không khớp (history bị cắt) = evidence of real work ⇒ stale
  (conservative, giữ tinh thần FIX-R8B).

Real work bất kỳ ⇒ stale ⇒ prose sau đó là summary hợp lệ (hành vi golden 18 giữ
nguyên). Nhờ vậy FALSE_COMPLETION sống lại sau no-op commits ⇒ chuỗi
correction → noop_streak → skip branch hoạt động lại đúng thiết kế breaker.

### Diff tóm tắt

- `gpt/gateway/runtime.py`: +import `_is_placeholder_command` (từ
  `gpt.utils.toolcall`, read-only); hàm `_single_noop_invoke` (:306);
  `_fresh_tool_conversation` viết lại (:346).
- `tests/test_correction_tighten.py`: +2 test — unit freshness (noop/placeholder
  fresh, real/orphan stale) và end-to-end metronome 3 request qua một record dùng
  chung (ghim `conversation_id` để breaker key ổn định) kết thúc bằng event
  `correction_skipped_noop_repeat`, `noop_streak=2`.
- `evals/goldens/18_fixr8b_fresh_guard.json`: +scenario data-only
  "metronome-noop-commits+claim" want FALSE_COMPLETION (runner không đổi).

## Finding 6 (Low) — VERDICT: ĐÚNG, đã fix

### Bằng chứng (repro `verify_finding6.py` trước fix)

`WEBGPT_MAX_PROMPT_CHARS=4000`, soft handshake cần thiết: prompt render 3635 chars
< 4000 đi qua cả hai check (:1703/:1707 cũ) rồi `_with_soft_handshake()` cộng thêm
**814 chars** (handshake+framing) ⇒ gửi lên web **4449 chars > limit**, đúng kịch bản
echo RC1 (scale-dependent echo khi vượt ~25k thực tế).

### Fix

- Helper `_soft_handshake_overhead_chars()` (:1128) = đúng độ dài suffix mà
  `_with_soft_handshake` cộng (`\n\n` + handshake + `\n\n` + framing).
- Trong `execute_raw_on_session` (:1744-1796): `effective_max_chars =
  max(1, max_prompt_chars - reserved)` với `reserved` = overhead khi
  `soft_handshake_appended`, dùng cho CẢ trigger compaction LẪN hard raise ⇒
  prompt sau handshake luôn ≤ configured limit (rstrip chỉ làm ngắn thêm).
  Compaction fail-closed giờ raise với message nêu rõ effective limit + số chars
  đã reserve. Trace metadata `prompt_compacted` vẫn báo `max_prompt_chars`
  gốc (operator view không đổi).

## Số test nguyên văn

- Trước fix (baseline): `pytest tests/test_correction_tighten.py tests/test_prompt_budget.py -q`
  → **26 passed**; evals all → **total=19 pass=18 fail=1**
  (`fixr8b-placeholder-cmd-no-tool-call` FAIL sẵn từ trước — thuộc finding #5
  codex12, file toolcall.py ngoài phạm vi; golden 17 đang fail ở nhánh
  placeholder-only bị trim prose).
- Sau fix: cùng 2 file pytest → **29 passed in 0.23s** (+3 test mới).
  Evals all → **total=19 pass=18 fail=1** (không đổi; golden 17 vẫn fail y
  baseline, golden 18 PASS với scenario metronome mới).
- Regression lân cận: `test_correction_context, delta_tooluse_and_handshake,
  discover_policy, fault_injection, gateway_agent_loop, prose_correction_live,
  refusal_detection, stealth_protocol, ui_stream_hygiene` → **149 passed**.
- ruff/mypy: không có trong `.venv` (module not found) — bỏ qua bước này.

## Ghi chú ngoài phạm vi (không sửa)

1. Trong lúc verify, một repro tạm gặp
   `_extract_soft_candidates() missing 1 required positional argument: 'definitions'`
   từ đường parse soft; `toolcall.py` có mtime 09:12 hôm nay (agent khác đang sửa)
   và lỗi đã tự hết trên cây hiện tại — theo dõi thêm.
2. Golden 17 fail tồn dư (finding #5 codex12, `_finalize_parse` không trim khi
   raw_calls rỗng) — chờ agent phụ trách toolcall.py.

— fix-agent codexfix-b, 2026-08-26.
