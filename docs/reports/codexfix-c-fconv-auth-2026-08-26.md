# Codex-Fix C — fconv auth invalidation + off-loop PoW (2026-08-26)

Fix agent report cho codex review #12 (`~/Downloads/webgpt/codex-reviews/codex12-yesterday-fixes-2026-08-26.md`), findings 3 + 4 (vùng transport).

## FINDING 3 (Medium) — fconv 401/403 giữ nguyên Bearer bundle hỏng

**Verdict: ĐÚNG — đã fix.**

Verify trên đĩa:
- Prepare stage: `_prepare_fconv_turn()` gửi `Authorization: Bearer {bundle.access_token}` qua `_integrity_headers()` cho cả `_SENTINEL_PREPARE_URL` lẫn fallback `_SENTINEL_CLASSIC_URL`; khi cả hai fail (kể cả 401/403) nó raise `ProtocolChanged` **không invalidate gì** (`curl_transport.py`, khối sau hai lần `_post_json`).
- SSE stage: `send()` gọi `_raise_for_status(response, codex=codex)`; với fconv `codex=False` ⇒ chỉ chạy `_invalidate_sentinel_cache()`. Nhưng nhánh fconv mint fresh mỗi turn và **không bao giờ đọc** sentinel cache ⇒ invalidation đó là no-op, còn bundle Bearer bị từ chối vẫn nằm trong `TokenManager._bundle` + disk cache đến hết `refresh_interval` (default 1800s).
- Đối chiếu: nhánh codex đã đúng từ review round 10 (`_invalidate_access_credentials()` → `TokenManager.invalidate_access_token`, xóa luôn disk cache).

Fix (mirror hành vi codex, bất kể flag):
1. `_prepare_fconv_turn`: khi prepare/classic trả 401/403 → `_invalidate_access_credentials()` trước khi raise `ProtocolChanged`. Status khác (5xx…) không đụng cache. Conduit stage vẫn non-fatal theo spec §4 — nếu Bearer hỏng thì SSE call ngay sau đó sẽ 401 và tự invalidate.
2. `_raise_for_status(..., *, codex=False, fconv=False)`: `codex or fconv` ⇒ invalidate access credentials; legacy giữ nguyên hành vi cũ (chỉ sentinel cache). Call site truyền `fconv=fconv`.

## FINDING 4 (Medium) — PoW 500k vòng block event loop

**Verdict: ĐÚNG — đã fix.**

Verify: `solve_sentinel_pow()` (`token_manager.py::272`) loop tới `SENTINEL_PROOF_MAX_ATTEMPTS = 500_000` vòng json.dumps + base64 + sha3_512 đồng bộ, difficulty do server kiểm soát; `_prepare_fconv_turn()` gọi thẳng trong `async def` (`curl_transport.py`) ⇒ challenge khó chặn toàn bộ loop, timeout/cancellation không xử lý được.

Fix tại call site: `answer = await asyncio.to_thread(solve_sentinel_pow, ...)`. Logic PoW nguyên vẹn; cancellation của task vẫn unwound ngay (thread mồ côi chỉ chạy nốt budget của nó); import `asyncio` thêm vào `curl_transport.py`. Ghi chú: `bootstrap_proof_token()` cũng gọi solver nhưng difficulty cố định `"0"` (~16 vòng kỳ vọng) — rẻ, để nguyên.

## Diff tóm tắt

- `gpt/transport/curl_transport.py`: +import asyncio; PoW qua `asyncio.to_thread`; invalidate access credentials ở prepare-stage 401/403; `_raise_for_status` nhận `fconv=` và invalidate Bearer bundle cho `codex or fconv`.
- `gpt/transport/token_manager.py`: không đổi (invalidate_access_token đã có sẵn từ FIX-CODEX10).
- `tests/test_fconv_prepare.py`: FlowTokenManager đếm 2 loại invalidation; 3 test mới.

## Test

```
pytest -q tests/test_fconv_prepare.py tests/test_codex_sse.py \
  tests/test_sentinel_cache.py tests/test_sentinel_flow.py \
  tests/test_sentinel_sdk_mint.py tests/test_token_manager.py
→ 59 passed

pytest -q tests/test_codex_auth.py tests/test_session.py tests/test_fault_injection.py
→ 48 passed
```

Không full suite, không commit, không restart gateway, không đụng runtime.py / toolcall.py / api/server.py (theo quy tắc fix-agent). ruff/mypy không có trong venv hiện tại nên bỏ qua bước lint.
