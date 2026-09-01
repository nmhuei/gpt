# MODEL-ROUTING-PHASE2 — Downgrade telemetry + WEBGPT_MODEL_FALLBACK (2026-08-26)

Phase-2 theo roadmap row S (tiếp nối `model-routing-phase1-2026-08-26.md`):
telemetry đếm downgrade per-request + fallback policy chống silent downgrade.
Vẫn opt-in hoàn toàn: không bật env ở đâu, không live call, warn là mặc định
⇒ hành vi runtime byte-identical như phase 1.

## Đã làm

### 1. TurnResult telemetry (gpt/utils/types.py)

3 field mới cạnh `requested_model` (đã có từ phase 1):

- `resolved_model: str | None = None` — slug server thực sự publish cho turn
  (None khi stream không publish slug nào).
- `model_downgraded: bool = False`
- `model_downgrade_count: int = 0` — đếm per-request (0/1; một request chỉ có
  một stream chính).

Điền tại `_stream_sse` (fconv): verdict tính MỘT lần, WARNING "MODEL-ROUTING
mismatch" (message giữ nguyên phase 1) và telemetry cùng nguồn nên không bao
giờ lệch nhau. Codex path không đổi — endpoint chưa expose served slug nên
không kết luận được mismatch (`resolved_model` stays None).

### 2. Fallback policy `WEBGPT_MODEL_FALLBACK`

- `parse_model_fallback_env()` module-level cạnh các helper phase 1;
  unset/empty ⇒ `warn`; chấp nhận case-insensitive `warn` | `retry-once`;
  giá trị lạ → ValueError fail-loud.
- Policy được đọc + validate NGAY ĐẦU `send()` cho mọi request ⇒ env hỏng
  fail-loud ngay, không nấp đến khi xảy ra downgrade.
- `warn` (mặc định): chỉ log + telemetry, đúng 1 POST upstream, không marker,
  payload/text byte-identical phase 1.
- `retry-once`: khi attempt-1 chứng minh downgrade (`model_downgraded`) →
  `_maybe_retry_model_fallback()` gửi đúng thêm 1 lần nữa:
  - envelope GIỐNG HẾT, riêng root `"model"` = `"auto"` (default fconv) và
    drop alias-pinned `thinking_effort` trừ khi client tự gửi effort;
  - uuid message/parent mới (tránh bị coi duplicate submission);
  - request stream dùng `replace(request, model=None)` để served-slug của
    retry không tự trigger lại mismatch verification với route đã bỏ;
  - marker text đầu stream `[webgpt:model-fallback <req>→<got>]\n`: emit qua
    on_delta ở delta đầu của retry VÀ prepend vào result.text (stream và text
    luôn khớp);
  - TurnResult trả về mang telemetry của attempt-1 (requested/resolved/
    downgraded/count=1); status/duration tính trên toàn request;
  - retry lỗi gì cũng KHÔNG fail-hard: log warning "MODEL-ROUTING fallback
    retry failed", trả nguyên result attempt-1 (text đầy đủ).
- Chỉ áp dụng path fconv (codex excluded — chưa thấy được served slug).

## Files

- `gpt/transport/curl_transport.py` — helper policy + `_stream_sse` telemetry
  + hook cuối `send()` + `_maybe_retry_model_fallback()`. Không đụng vùng cấm
  (`_prepare_fconv_turn`, codex bearer, `_upload_turn_images`, 1082/2087).
- `gpt/utils/types.py` — 3 field TurnResult (git diff trước khi sửa: chỉ
  phase-1 từng đụng, không agent khác touch hôm nay).
- `tests/test_model_routing_phase1.py` — giữ nguyên file, mở rộng 7 test.

## Tests (16 total trong file: 9 phase-1 + 7 mới)

Policy parser warn/retry-once/casefold/malformed · mismatch điền đủ
resolved/downgraded/count=1 · matched+absent slug ⇒ count=0 (matched điền
resolved, absent None) · warn default + explicit "warn": 1 POST duy nhất,
không marker, telemetry vẫn ghi · retry-once happy path: đúng 2 POST, retry
payload model="auto" + drop effort pin + uuid mới, marker đầu delta retry và
đầu result.text, telemetry giữ evidence attempt-1 · served khớp ⇒ không retry
(1 POST) · retry nổ RuntimeError ⇒ giữ nguyên result gốc, warning logged.

Chạy: `.venv/bin/python -m pytest tests/test_model_routing_phase1.py -q`
(16 passed). Suite liên quan curl_transport/codex_sse/file_upload/
fconv_prepare/model_effort/session/hybrid_cache_dir/normalize/codex_auth:
102 passed. **Full suite: 1293 passed.** ruff clean (touched files); mypy
0 error tại curl_transport/types (23 lỗi pre-existing ngoài phạm vi, giống
phase 1).

## Env dán cho operator (khi có DECISIONS.md entry)

```bash
WEBGPT_MODEL_ALIAS='claude-sonnet-4-5=gpt-5-5-thinking:low'
WEBGPT_MODEL_FALLBACK='warn'   # hoặc 'retry-once'
```

## Out of scope (phase 3)

Precheck `capabilities()`, pin per-conversation, feed breaker từ mismatch,
bảng retired→replacement, retry-once cho codex (cần endpoint expose slug).
