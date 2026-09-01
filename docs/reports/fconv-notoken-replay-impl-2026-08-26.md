# FCONV-NOTOKEN-REPLAY — IMPL (offline phần) 2026-08-26

Phạm vi: chỉ offline theo ROADMAP row FCONV-NOTOKEN-REPLAY. KHÔNG bắn live,
KHÔNG bật env, KHÔNG commit, KHÔNG restart gateway. Nguồn:
docs/reports/sse-resume-research-2026-08-26.md (kymuco PR #40/#41 marker
`X-Conduit-Token: no-token`; prepare authed + body 15-field → 200).

## Đã làm

1. **gpt/transport/curl_transport.py**
   - Hằng số `_FCONV_PREPARE_NOTOKEN = "no-token"` cạnh `_FCONV_PREPARE_URL`
     (~:149) — literal marker kymuco, dễ thay khi có conduit thật.
   - Trong `_prepare_fconv_turn` (~:452): header prepare giờ gắn
     `X-Conduit-Token: no-token` trước `_post_json(_FCONV_PREPARE_URL, …)`.
     Không đụng codex/bearer/image-upload; envelope SSE cuối KHÔNG đổi
     (`_build_headers` vẫn chỉ stamp `X-Conduit-Token` với token thật).
2. **scripts/fconv_replay.py** (mới)
   - argparse + main-guard; `--help` và dry-run zero side-effect (đã verify
     exit 0 / stderr rỗng). Default DRY-RUN in plan; `--live` mới chạy thang
     ≤4 request đúng thứ tự requirements → PoW local → prepare (body
     15-field + marker) → conversation POST.
   - Steps 1–3 đi qua `_prepare_fconv_turn` THẬT qua subclass instrumented
     (override `_post_json`) — mỗi hop in URL, header quan trọng (Authorization/
     Cookie redact), body 200 chars, status + response 200 chars; dừng sớm kèm
     verdict. Step 4 tái dùng `_build_headers`/`_maybe_build_multimodal_payload`
     của transport, đọc ≤200 bytes SSE đầu rồi close.
   - Verdict theo tiêu chí ROADMAP: conversation stream → ALIVE (exit 0);
     prepare 200+conduit nhưng conv 4xx → BRANCH-DEAD, pivot CODEX-SSE OAuth
     (exit 1); prepare fail/không conduit → PREPARE-FAIL, thử 1 lần profile/IP
     khác trước kết luận (exit 1); lỗi transport → INCOMPLETE (exit 2).
   - Script TỰ KHÔNG set env: `--live` khi flag OFF → từ chối exit 2 TRƯỚC khi
     import nặng/browser (đã verify).
3. **tests/test_fconv_prepare.py** (+3 test → 24 total, pass 0.17s)
   - `test_prepare_request_carries_notoken_marker_header`: flag ON → prepare
     mang `X-Conduit-Token: no-token`; sentinel stage không có header này;
     SSE cuối mang token thật ≠ marker.
   - `test_prepare_marker_sent_even_when_prepare_fails`: prepare 500 → marker
     vẫn trên request, turn vẫn non-fatal, SSE không có X-Conduit-Token.
   - `test_flag_off_never_sends_prepare_or_marker`: flag OFF → đúng 1 call
     (CONVERSATION_URL), không marker nào outbound.

## Verify

- `.venv/bin/python -m pytest tests/test_fconv_prepare.py -q` → 24 passed.
- `tests/test_file_upload.py` (cùng transport) → 22 passed, không ảnh hưởng.
- `--help` exit 0 stderr rỗng; dry-run in plan exit 0; `--live` flag-off exit 2.
- ruff/mypy không có trong venv hiện tại (module missing) — bỏ qua.

## Hướng dẫn coordinator bắn live (an toàn)

```bash
# 1. preflight thủ công, KHÔNG qua script env-set:
WEBGPT_FCONV_PREPARE=1 .venv/bin/python scripts/fconv_replay.py --live \
  --profile <profile-dir> [--model gpt-5.x] [--timeout 45]
# 2. Chạy NGẮN, headless mặc định; nếu PREPARE-FAIL/BRANCH-DEAD: thử đúng 1 lần
#    từ profile/IP khác rồi mới chấm theo ROADMAP; sau đó unset env (shell scoped).
```

Lưu ý: script không tự set env (guard exit 2) — coordinator export trong cùng
lệnh; gateway đang chạy không bị ảnh hưởng (script dựng stack riêng).
