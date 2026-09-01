# CODEX-SSE live probe — 2026-08-25

Mục tiêu: live-verify nhánh CODEX-SSE (`POST https://chatgpt.com/backend-api/codex/responses`,
gate `WEBGPT_CODEX_SSE`, `curl_transport.py`) bằng tối đa 2 request thật, đúng spec
`docs/reports/codex-sse-spec-2026-08-25.md`.

Probe: `~/Downloads/codex-sse-probe.py` (mỗi lần chạy = đúng 1 POST codex). Credentials
đọc **chỉ-đọc** từ disk cache `webgpt-token-cache.json` trong
`/home/light/Downloads/webgpt/profiles/personal/` qua chính `TokenManager._load_disk_cache`
của repo (`refresh_interval=10**9` → không đụng browser, không ghi gì vào profile;
không tạo bản copy profile nào). Headers dựng bằng **nguyên văn**
`CurlCffiTransport._build_headers(bundle, SentinelTokens(), codex=True)` của code mới merge
(originator `codex_cli_rs`, `OpenAI-Beta: responses=experimental`, Bearer AT,
full cookie jar 30 cookie gồm cf_clearance, UA Chrome/146, impersonate chrome146).
Body: `{"model":"gpt-5","instructions":"Say OK","input":[1 user input_text],"tools":[],
"tool_choice":"auto","store":false,"stream":true}`.

## Attempt 1 — 12:08:xx UTC

- Chuẩn bị: GET `/api/auth/session` qua curl_cffi (tương tự bước in-page của
  TokenManager) để lấy AT mới → **HTTP 403** (path này bị chặn khi gọi ngoài page;
  khớp ghi chú sẵn có của repo rằng vài endpoint chỉ chạy được in-page).
- POST codex với AT cache (iat 2026-08-24T12:28Z): **HTTP 401** sau 0.5s.
- Decode JWT cục bộ: AT **chưa hết hạn** — `exp = 2026-09-03T12:28Z` (TTL ~10 ngày,
  KHÔNG phải ~1h như spec §1 viết); issuer `https://auth.openai.com`,
  aud `https://api.openai.com/v1`, scope có `model.request/model.read`;
  trong claim `https://api.openai.com/auth` có `chatgpt_account_id`
  (`cf20af79-…`, redact) và `chatgpt_plan_type: None`.

## Attempt 2 — ~12:12 UTC

- Giữ nguyên AT (còn hạn) + thêm `Chatgpt-Account-Id` trích từ JWT (S5 dùng, S2 ghi optional).
- Kết quả: **HTTP 401**, body nguyên văn `{"detail":"Unauthorized"}`
  (content-type application/json, server cloudflare, KHÔNG có www-authenticate/cf-mitigated).

## Kết luận

- **RECIPE CHƯA SỐNG với credential hiện có** — gate `WEBGPT_CODEX_SSE` giữ OFF.
- Chẩn đoán lớp: **lớp token**, không phải CF/header-shape/endpoint:
  - Không phải 403 challenge: request xuyên qua Cloudflare sạch, server trả JSON
    FastAPI-style tức endpoint tồn tại và chấp nhận envelope headers (không cần sentinel — xác nhận điểm thắng spec §1).
  - Không phải expiry: JWT còn hạn tới 09-03.
  - 401 lặp lại cả khi có/không `Chatgpt-Account-Id` → AT web-session của
    TokenManager **bị từ chối làm credential cho codex backend**. Các nguồn S1/S5
    dùng token từ luồng OAuth riêng của Codex CLI (auth.json/PKCE), không phải AT
    session web — khoảng trống credential này là nguyên nhân khả dĩ cao nhất.
  - Yếu tố chưa loại trừ được (cần probe sau khi hết giới hạn request):
    account này chưa được enroll Codex/Plus cho endpoint (`plan_type: None` trong
    claim), hoặc cần thêm `session_id` header (S3) đi kèm.
- Hướng mở (theo thứ tự đáng thử): (1) mint token theo luồng OAuth Codex CLI
  (client riêng, lưu auth.json kiểu codex) thay vì tái dùng AT web; (2) kiểm tra
  enrollment Codex của account trên UI; (3) thử thêm `session_id` header.
- Vệ sinh: không restart service, không pytest, không commit; đúng 2 POST codex
  (12:08–12:12 UTC 2026-08-25), không gặp rate-limit nên không cần dừng sớm;
  không có bản copy profile trong /tmp (thiết kế probe đọc trực tiếp file cache
  0600, chỉ-đọc). Probe script giữ lại tại `~/Downloads/codex-sse-probe.py`.
