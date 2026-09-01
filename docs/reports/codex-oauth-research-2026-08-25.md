# Codex OAuth Research — 2026-08-25

> Research report: cơ chế login của openai/codex (codex-rs), khả năng mint token ngoài CLI cho gateway TokenManager. Status: DONE.

## 1. auth.json — vị trí & cấu trúc

Vị trí: `$CODEX_HOME/auth.json` (mặc định `~/.codex/auth.json`). Có thể lưu qua keyring (`AuthKeyringBackendKind`) thay vì file.

Cấu trúc (`AuthDotJson`, nguồn: `codex-rs/login/src/auth/storage.rs`):

```jsonc
{
  "auth_mode": "chatgpt",            // hoặc "apikey"
  "OPENAI_API_KEY": null,             // chỉ dùng khi auth bằng API key
  "tokens": {
    "id_token": "<JWT>",              // parse thành IdTokenInfo
    "access_token": "<JWT>",
    "refresh_token": "<opaque>",
    "account_id": "optional"
  },
  "last_refresh": "2026-08-25T00:00:00Z",
  "agent_identity": null,
  "personal_access_token": null
}
```

`id_token` JWT claims (namespace `https://api.openai.com/`): `email`,
`profile.chatgpt_plan_type` ("free"|"plus"|"pro"|"business"|"enterprise"|"edu"),
`auth.chatgpt_user_id`, `chatgpt_account_id`, `chatgpt_account_is_fedramp`.
File chmod 0600 (unix).

## 2. PKCE flow thủ công (authorize URL + token exchange)

Issuer: `https://auth.openai.com`. Client ID: `app_EMoamEEZ73f0CkXaXp7hrann`
(override env `CODEX_APP_SERVER_LOGIN_CLIENT_ID`). Nguồn: `codex-rs/login/src/server.rs`,
`pkce.rs`, `auth/manager.rs`.

Bước thủ công bằng HTTP thuần:

1. **PKCE**: `code_verifier` = 64 random bytes → base64url không padding;
   `code_challenge` = base64url(SHA256(verifier)).
2. **Local callback**: CLI bind server `http://localhost:1455/auth/callback`
   (fallback 1457); `state` = 32 random bytes base64url. Khi mint tay có thể
   dùng chính port 1455 hoặc tự host redirect.
3. **Authorize URL** (mở trong browser đã đăng nhập ChatGPT):
   ```
   https://auth.openai.com/oauth/authorize
     ?response_type=code
     &client_id=app_EMoamEEZ73f0CkXaXp7hrann
     &redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback
     &scope=openid%20profile%20email%20offline_access%20api.connectors.read%20api.connectors.invoke
     &code_challenge=<challenge>
     &code_challenge_method=S256
     &id_token_add_organizations=true
     &codex_cli_simplified_flow=true
     &state=<state>
     &originator=codex_cli_rs
   ```
4. **Token exchange** sau khi nhận `code` từ callback:
   ```
   POST https://auth.openai.com/oauth/token
   Content-Type: application/x-www-form-urlencoded

   grant_type=authorization_code&code=<code>&redirect_uri=<redirect_uri>
   &client_id=<client_id>&code_verifier=<verifier>
   ```
   Response JSON: `id_token`, `access_token`, `refresh_token` (+ `expires_in`).
   Lưu ý entitlement check: thiếu Codex entitlement → error
   `is_missing_codex_entitlement_error`.
5. Có thêm **API-key exchange** (grant `urn:ietf:params:oauth:grant-type:token-exchange`,
   `requested_token=openai-api-key`, subject = id_token) nếu muốn đổi sang API key.

## 3. TTL / refresh pattern

(`codex-rs/login/src/auth/manager.rs`)

- **Access token**: JWT có `exp`; refresh khi còn **< 5 phút**
  (`CHATGPT_ACCESS_TOKEN_REFRESH_WINDOW_MINUTES = 5`) trước khi request.
- **Refresh call**: `POST https://auth.openai.com/oauth/token` với
  **JSON body** `{client_id, grant_type: "refresh_token", refresh_token}`
  (khác exchange ở trên dùng form-urlencoded).
- **Rotation**: response trả `refresh_token` mới → phải persist lại vào
  auth.json cùng `last_refresh`. Server phát hiện **reuse** refresh token cũ
  (`REFRESH_TOKEN_REUSED_MESSAGE`) → single-use rotation, race giữa nhiều
  process phải serialize.
- **Refresh-token TTL**: `TOKEN_REFRESH_INTERVAL = 8` ngày — nếu
  `last_refresh` cũ hơn 8 ngày coi như hết hạn, bắt buộc login lại.
- Lỗi phân loại: expired / reused / revoked / account-mismatch → đều yêu cầu
  re-login; lỗi transport là transient (retry được).

## 4. Account enrollment — plan & yêu cầu UI

- **Codex nằm trong mọi bậc ChatGPT**, kể cả Free (limit thấp); Go/Plus/Pro nâng
  hạn mức. Không cần bật gì trong UI ngoài việc **Sign in with ChatGPT** qua
  OAuth — entitlement được check ngay ở bước login
  (`is_missing_codex_entitlement_error` khi thiếu).
- **Plus đủ dùng** Codex; hạn mức dạng layered: allowance theo plan + cửa sổ
  5 giờ + weekly cap + credits (nếu workspace hỗ trợ). ChatGPT Work và Codex
  dùng chung một agentic usage pool.
- **API key là tuyến tính riêng**: nếu auth bằng `OPENAI_API_KEY` thay vì
  ChatGPT OAuth thì billing qua OpenAI Platform theo giá API chuẩn — không
  liên quan subscription Plus.
- Kết luận cho gateway: dùng tài khoản ChatGPT Plus cá nhân + PKCE flow ở
  §2 là đủ, không cần thao tác UI nào khác.

## 5. Kết luận: mint codex-token ngoài CLI cho gateway

**Khả thi kỹ thuật**: flow ở §2 chỉ là HTTP thuần (authorize URL + form POST +
JSON refresh) — không cần CLI, không cần browser automation sau khi có refresh
token. OpenAI **chấp nhận chính thức** việc OSS client (Pi, OpenCode...) tự
auth bằng tài khoản của mình ("Sign in With ChatGPT" qua client OSS).

**Rủi ro fraud-detection** (sự kiện sub2api bị flag 21/08/2026, Tibo Sottiaux —
Codex lead): điều bị flag là mô hình **đăng ký cá nhân route qua proxy rồi
re-serve như shared API traffic cho nhiều người** — "not supported", bị hệ
thống fraud-prevention tự động flag. Bài báo không công bố signal cụ thể, nhưng
pattern rủi ro = pooling/nhiều downstream user. Việc **mint token cho gateway
local dùng đúng 1 tài khoản của mình** về bản chất giống chạy codex CLI/OSS
client → rủi ro thấp hơn nhiều, nhưng vẫn nên:

1. Serialize refresh (rotation single-use — refresh token cũ bị coi là reused
   nếu 2 process refresh song song → revoke cả chuỗi token).
2. Giữ User-Agent/originator nhất quán, tránh fan-out concurrency lớn từ nhiều
   IP trên cùng access token.
3. Coi đây là đường "personal use" — tuyệt đối không mở endpoint public cho
   người ngoài dùng chung token này (đó chính là sub2api).

So với TokenManager hiện tại (browser-extract cookie/access_token từ
chatgpt.com), codex-token path có ưu điểm: JWT có `exp` rõ ràng, refresh bằng
HTTP thuần không cần browser, TTL dài hơn (8 ngày cho refresh chain).

## 6. Đề xuất task ROADMAP

**Tên task**: `codex-auth-token-source` — thêm nguồn credential Codex OAuth
cho TokenManager, tách khỏi browser.

- **File mới**: `gpt/transport/codex_auth.py`
  - `CodexAuthStore`: load/save `$CODEX_HOME/auth.json` (schema `AuthDotJson`
    ở §1), chmod 0600.
  - `CodexTokenRefresher`: async refresh qua `POST https://auth.openai.com/oauth/token`
    JSON body `{client_id: app_EMoamEEZ73f0CkXaXp7hrann,
    grant_type: refresh_token, refresh_token}`; refresh khi exp còn <5 phút;
    persist rotation + `last_refresh`; hard-stop nếu `last_refresh` >8 ngày;
    `asyncio.Lock` chống refresh-reuse race.
  - Headless PKCE bootstrap (in URL, nhận code paste tay hoặc callback server
    port 1455) — chỉ chạy 1 lần để lấy refresh_token đầu tiên.
  - Adapter expose interface tương thích bundle của `TokenManager` để gateway
    chọn nguồn credential: `browser` (hiện tại) | `codex`.
- **Test**: `tests/test_codex_auth.py` — parse auth.json fixture; mock httpx
  cho refresh (success/rotation/expired/reused); assert lock serialize;
  assert 8-day expiry gate.
- **Effort**: M (≈1–2 ngày). Rủi ro thấp, không đụng orchestration semantics
  (không cần cập nhật DECISIONS.md, chỉ thêm dòng STATE).

## Nguồn

- Repo code (fetch trực tiếp 25/08/2026):
  - https://github.com/openai/codex/blob/main/codex-rs/login/src/token_data.rs
  - .../codex-rs/login/src/auth/storage.rs (`AuthDotJson`)
  - .../codex-rs/login/src/auth/manager.rs (CLIENT_ID, REFRESH_TOKEN_URL,
    TTL constants)
  - .../codex-rs/login/src/pkce.rs, server.rs (authorize URL, exchange,
    local callback port 1455/1457)
- Web:
  - https://www.explainx.ai/blog/codex-usage-limits-sub2api-sign-in-chatgpt-august-2026
    (sub2api flag 21/08/2026)
  - https://uibakery.io/blog/openai-codex-pricing (plan inclusion + API key
    billing tách riêng)
  - https://simplemetrics.xyz/is-codex-included-with-chatgpt-plus/ (Plus có
    Codex, giới hạn theo plan)
  - https://inventivehq.com/blog/codex-cli-pricing-explained (Free/Go/Plus/Pro/
    Business bundle, khi nào dùng API key)
