# LIMIT-SIGNATURE-TAXONOMY + conftest env-scrub — 2026-08-26

Row S của quota-pattern-research: breaker không được nuôi bởi signal giả.

## Verdict

| Việc | Verdict |
|---|---|
| LIMIT-SIGNATURE-TAXONOMY | DONE — tầng phân loại chạy TRƯỚC khi chọn exception trong transport; chỉ pure-429 mới hoá thành `RateLimited` (signal duy nhất breaker ăn) |
| conftest env-scrub | DONE — thêm `WEBGPT_ACCOUNTS_FILE` + `WEBGPT_PROFILES_ROOT` vào `_scrub_host_env` |

## Thiết kế taxonomy (`gpt/transport/challenge.py`)

API mới: `LimitSignal` enum + `classify_limit_signal(status, body)` + `has_rate_limit_json_signature(snippet)`.

Độ ưu tiên khi classify một response ≥400:

1. **CHALLENGE** — marker Cloudflare/Turnstile trong body ở **bất kỳ** status nào (403/503/và cả envelope 429 mang HTML) → đường challenge recovery có sẵn (re-mint + retry đúng 1 lần), KHÔNG đụng breaker.
2. **PURE_RATE_LIMIT** — JSON parse được và có signature rate-limit rõ ràng (`rate_limit*` key, hoặc value chứa `rate limit`/`too_many_requests`/`usage_limit`), hoặc bare status=429 ("429 thuần") → `RateLimited` ⇒ breaker được nuôi hợp lệ.
3. **UNDETERMINED** — còn lại (403/503 trơn, body rỗng/không đọc được, HTML lạ) → giữ mapping legacy (`AuthRequired`/`ProtocolChanged`), không path nào trong số này đụng breaker.
4. Status <400 → NONE.

### Điểm cài đặt (`gpt/transport/curl_transport.py::_raise_for_status`)

- Gate peek-body + classify chèn **sau** nhánh `<400` và **bao quanh** nhánh `{401,403}` cũ — nhánh 401 (vùng FIX13-A vừa merge: invalidate + force-refresh + untrusted latch) **không đổi một byte**, 401 bỏ qua gate.
- CHALLENGE → raise `ChallengeDetectedError` (typed, kèm kind/status/url) thay vì `RateLimited`/`AuthRequired`/`ProtocolChanged`.
- 429 không marker → `RateLimited` như cũ; 403 trơn → `AuthRequired` như cũ; 503 trơn → `ProtocolChanged` như cũ.

Vì breaker chỉ trip trên `RateLimited` / state `RATE_LIMITED` (factory lease/bootstrap), việc chặn sai phân loại tại nguồn đủ để breaker không bao giờ thấy signal giả — **không cần sửa factory/breaker**.

## Test (targeted, tất cả qua `.venv/bin/python -m pytest -q`, không --timeout)

| File | Kết quả | Mới thêm |
|---|---|---|
| tests/test_cf_resilience.py | **27 passed** | 12 (unit taxonomy ×5; `_raise_for_status` gate ×5; full-flow 429-challenge remint-thành-công + persistent-429-challenge ×2) |
| tests/test_backoff_breaker.py | **12 passed** | 3 parametrize (lease turn ném `ChallengeDetectedError` 403/429/503 → breaker vẫn closed, trips==0, acquire tiếp theo vào bình thường) |
| tests/test_accounts.py | **14 passed** | 0 — nguyên xanh sau scrub |
| test_curl_transport + factory + session + hybrid | **22 passed** | regression guard |

## Files changed

- `gpt/transport/challenge.py` — LimitSignal + classifier (+json import)
- `gpt/transport/curl_transport.py` — import + gate trong `_raise_for_status`
- `tests/test_cf_resilience.py`, `tests/test_backoff_breaker.py` — tests mới
- `tests/conftest.py` — scrub 2 env registry

Không đụng: runtime/toolcall/gateway-server/api-server/protocol_adapters/accounts.py/factory/session/ui. Không commit, không restart, không live call. ruff/mypy không có trong venv hiện tại (module not found) — đã compile-check OK.
