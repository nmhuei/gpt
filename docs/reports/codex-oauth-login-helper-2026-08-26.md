# CODEX-OAUTH-LOGIN-HELPER — Implementation Report (2026-08-26)

Task: CLI helper mint bundle Codex OAuth đầu tiên (unblock CODEX-SSE branch).
Status: **DONE** — offline tests 100%, không chạy flow thật, không commit, không restart gateway.

## Files

- `scripts/codex_oauth_login.py` (mới) — argparse + `__main__` guard (`--help` zero side-effect, bài học FAILURES 2026-08-25).
- `tests/test_codex_oauth_login.py` (mới) — 34 test, HTTP exchange monkeypatch 100%, không network.
- `gpt/transport/codex_auth.py` — bổ sung duy nhất: hàm public `bundle_from_token_payload(payload, now=None)` validate response token-endpoint thành `CodexAuthBundle` (`expires_in` ưu tiên hơn JWT `exp`, bắt buộc có cả access+refresh). Không đụng logic cũ → test cũ nguyên xanh (30 + 7 integration).

## Design

- Flow đúng research §2 (`codex-oauth-research-2026-08-25.md`): verifier = 64 bytes b64url no-padding; challenge = base64url(SHA256(verifier)); authorize URL đủ params (`originator=codex_cli_rs`, `codex_cli_simplified_flow=true`, scope có `offline_access`); state = 32 bytes b64url và được verify khi owner paste lại callback URL chứa `state`.
- Script KHÔNG mở browser, KHÔNG bind port 1455 — trang browser báo connection-refused là đúng chủ đích; owner copy URL thanh địa chỉ dán lại.
- Exchange form-urlencoded tới `https://auth.openai.com/oauth/token` (stdlib urllib, timeout 30s, HTTP error trả về chứ không raise).
- Ghi file qua `save_auth_json` (atomic tmp 0600 + fsync + `os.replace`) — seed thêm `OPENAI_API_KEY/agent_identity/personal_access_token = null` để khớp shape `AuthDotJson` của codex-rs.
- An toàn: từ chối ghi đè auth.json có sẵn nếu thiếu `--force`; KHÔNG BAO GIỜ print token (test bắt được bug leak thật: lần đầu echo nhầm return value của `save_auth_json` = toàn bộ document → đã sửa); lỗi luôn clean 1 dòng, exit code 0/1/130/2.

## HƯỚNG DẪN OWNER — mint lần đầu (5 bước, copy-paste được)

1. Chạy helper:
   ```bash
   .venv/bin/python scripts/codex_oauth_login.py --auth-json ~/.codex/auth.json
   ```
2. Copy URL `/oauth/authorize?...` mà script in ra, mở bằng browser đã đăng nhập ChatGPT (tài khoản Plus cá nhân là đủ), bấm Approve.
3. Browser bị redirect tới `http://localhost:1455/auth/callback?code=...&state=...` và hiện trang lỗi kết nối — ĐÚNG NHƯ DỰ KIẾN (không ai listen port 1455). Copy TOÀN BỘ URL từ thanh địa chỉ.
4. Dán URL đó vào prompt `Step 3/3` của script (Enter). Script exchange + ghi `~/.codex/auth.json` mode 0600. Nếu muốn non-interactive: chạy lại với `--code '<URL vừa copy>'`. Ghi chú: mỗi `code` chỉ dùng 1 lần — sai phải quay bước 1.
5. Bật nguồn credential cho gateway bằng cách set env trong `.env` (KHÔNG commit):
   ```bash
   echo 'WEBGPT_CODEX_AUTH_JSON=/home/light/.codex/auth.json' >> .env
   ```
   Gateway cần restart để nhận flag (việc restart thuộc task wire CODEX-SSE, không phải bước này).

Lưu ý vận hành: nếu refresh chain DEAD (reuse/8 ngày idle) chỉ cần chạy lại helper với `--force`; tuyệt đối không share `auth.json`.

## Tests (34)

PKCE S256 pairing + randomness (3) · authorize URL param shape (1) · paste-back parsing URL/bare-code/query + lỗi sạch (6) · main success: schema 0600, form fields, challenge↔verifier e2e binding, không print token, client-id từ env (5) · state mismatch chặn (1) · `--force` guard giữ nguyên file cũ (1) · HTTP 400 / transport / thiếu refresh_token → lỗi sạch không traceback (3) · Ctrl-C 130 / EOF hint `--code` (2) · `--help` zero side-effect với hook poison + parser defaults (2) · `bundle_from_token_payload`: expires_in thắng JWT exp, fallback exp, alias account, 7 shape sai (11).

Kết quả: `pytest -q tests/test_codex_oauth_login.py tests/test_codex_auth.py tests/test_codex_auth_integration.py` → **71 passed** (chạy 3 lần liền ổn định; 1 lần fail đơn lẻ ở integration là flake, pass khi chạy riêng và mọi lần sau).
