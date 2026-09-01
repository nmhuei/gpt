# CODEX-AUTH-TOKEN-SOURCE — Implementation Report (2026-08-26)

Task: tạo nguồn credential Codex OAuth tách khỏi browser, flag-gated default OFF.
Status: **DONE** — chưa wire vào factory/token_manager (đúng phạm vi).

## Files

- `gpt/transport/codex_auth.py` (mới, duy nhất) — không đụng `token_manager.py`,
  không sửa `__init__.py`, không bật flag ở bất kỳ đâu.
- `tests/test_codex_auth.py` (mới) — 30 test, fake HTTP 100%, **976 passed** toàn repo.

## Design

- **Flag gate**: `WEBGPT_CODEX_AUTH_JSON=<path>` vừa là công tắc vừa là đường dẫn
  auth.json. Unset → mọi entry point raise `CodexAuthDisabled`, zero I/O.
- **Bundle**: `CodexAuthBundle` frozen dataclass (`access_token`, `refresh_token`,
  `id_token`, `account_id`, `last_refresh_epoch`, `expires_at`). Expiry lấy từ JWT
  `exp`; response refresh có `expires_in` thì ưu tiên hơn.
- **Codec**: schema khớp `codex-rs AuthDotJson`; khi ghi chỉ đè các field mình sở hữu
  (`tokens.*`, `last_refresh`, `auth_mode` fallback), giữ nguyên key lạ
  (`agent_identity`, `personal_access_token`…) để codex CLI dùng chung file được.
  Atomic write: tmp 0600 + fsync + chmod + `os.replace` (pattern của
  `TokenManager._write_disk_cache`).
- **Refresh**: POST JSON `{client_id, grant_type: "refresh_token", refresh_token}`
  tới `https://auth.openai.com/oauth/token`. Client id mặc định
  `app_EMoamEEZ73f0CkXaXp7hrann`, override bằng `WEBGPT_CODEX_CLIENT_ID`.
- **Rotation single-use**: toàn bộ critical section read→HTTP→write chạy trong
  worker thread dưới `fcntl.flock(LOCK_EX)` trên `<auth.json>.lock` (0600), và
  **re-read auth.json trong lock** — process bị chờ sẽ adopt snapshot mới của
  winner thay vì tiêu nốt refresh token cũ (tránh reuse-detection revoke cả chuỗi).
  In-process có thêm `asyncio.Lock` để N coroutine gộp thành 1 lần refresh.
- **DEAD state**: refresh trả 400/401/403 (vd `invalid_grant`) → latch terminal
  in-memory, mọi call sau fail-fast không đụng disk/network — không có retry vòng.
  `last_refresh` cũ hơn 8 ngày (`TOKEN_REFRESH_INTERVAL`) → DEAD trước cả khi gọi
  HTTP. Network lỗi / 5xx / payload 200 dị thường → `CodexAuthTransient`
  (retry-later hợp lệ, class không tự retry).
- **get_access_token()**: serve token còn hạn; refresh khi còn ≤60s sống
  (skew theo task spec, khác cửa sổ 5 phút của codex-rs).
- HTTP mặc định qua curl_cffi sync session (chạy trong `asyncio.to_thread`),
  injectable `http_post` cho test.

## Mapping với research (codex-oauth-research-2026-08-25)

| Research | Implement |
|---|---|
| §1 auth.json `$CODEX_HOME`, 0600, schema AuthDotJson | `DEFAULT_AUTH_PATH ~/.codex/auth.json`; codec giữ key lạ; atomic 0600 |
| §3 refresh JSON body + client_id | `_post_refresh` JSON body; env override client_id |
| §3 rotation single-use → phải serialize | flock toàn section + re-read trong lock |
| §3 access JWT exp; refresh <5' | parse_jwt_exp; skew 60s theo task spec |
| §3 chain TTL 8 ngày | gate DEAD trước HTTP khi phải refresh |
| §3 expired/reused/revoked → re-login; transport transient | 400/401/403 → DEAD; network/5xx → Transient |
| §5.1 serialize chống đua 2 process | test mô phỏng process khác rotate giữa critical section |

## Tests (30)

Gate/default-OFF (3) · load validation missing/corrupt/apikey (4) · skew &
freshness (5) · rotation persist + 0600 + tmp-clean + lock-file mode (4) ·
DEAD 400/401/403 parametrize + no-retry + không ghi đè file (2+3 case) ·
8-day gate (2) · transient 500/network/payload (3) · concurrency dedup +
cross-process re-read adopt (2) · codec unit JWT/last_refresh (2) ·
invalidate/state API (2).

## Chưa chắc chắn / lưu ý

- ruff/mypy không cài trong `.venv` hiện tại → chưa chạy được static check
  (đã py_compile OK); cần `pip install -e ".[dev]"` rồi chạy lại.
- Không có live probe: URL/client_id/shape lấy thẳng từ research (fetch mã
  nguồn codex-rs 25/08); nếu OpenAI đổi contract sẽ lộ ở lần integration đầu.
- Nếu response refresh KHÔNG kèm `refresh_token` mới (hiếm), module giữ lại
  grant cũ — an toàn nhất có thể nhưng có thể chết ở lượt kế tiếp.
