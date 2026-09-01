# Verify (read-only): Config precedence + Auth surface — 2026-08-25

Phạm vi: config/env precedence, rò rỉ credential vô ý, env-pollution trong tests,
auth login flow, multi-account health/default wiring. Bỏ qua các issue bảo mật lớn
đã được owner chấp nhận (gateway no-auth, RCE-by-design, plaintext cred — xem
`docs/automation/DECISIONS.md`). Không in giá trị secret; chỉ tên biến/file/dòng.

## 1. Config precedence (environ > .env > default)

`load_config` (`gpt/config/settings.py:99-139`): **đạt**. Cả 10 biến cấu hình + 3
credential đều đi qua `resolve()` (dòng 109-126), không có chỗ nào trong settings.py
đọc `os.environ` trực tiếp. Bảng biến hỗ trợ:

| Biến | Default | Ghi chú |
|---|---|---|
| CHATGPT_EMAIL / CHATGPT_PASSWORD / CHATGPT_TOTP_KEY | None | credential |
| CDP_PORT | 9222 | `_parse_int`, sai giá trị → default |
| API_PORT | 8000 | |
| BROWSER_HEADLESS | true | |
| PROFILE_DIR | ~/Downloads/webgpt/cloak-profile | |
| DEFAULT_MODEL | gpt-5-5-thinking | |
| DEFAULT_EFFORT | high | |
| MAX_WORKERS | 3 | |

Lệch precedence tìm thấy (ngoài settings.py):

1. **~40 điểm đọc `os.environ` trực tiếp bỏ qua `.env`** — toàn bộ là flag vận hành
   `WEBGPT_*` (env-only). Chấp nhận được về thiết kế, NHƯNG `.env.example` quảng bá
   3 biến như thể ghi vào `.env` có hiệu lực với code Python, trong thực tế không:
   - `WEBGPT_MAX_PROMPT_CHARS` — đọc env-only tại `gpt/gateway/runtime.py:997`
   - `WEBGPT_CLAUDE_BIN` — env-only tại `gpt/orchestrator/session_runner.py:196`
   - `WEBGPT_GATEWAY_PORT` — chỉ shell script đọc (`scripts/webgpt-claude.sh:12`,
     `scripts/run-claude-code-benchmark.sh:27`, …); không script nào source `.env`
     (grep `source .env` = 0 kết quả) → giá trị trong `.env` chỉ có tác dụng nếu
     operator tự export.
   → Đề xuất: sửa comment `.env.example` mục "Claude Code bridge" thành
   "export vào shell (eval), không tự động nạp từ .env".
2. **Legacy alias đảo precedence**: `gpt/debug.py:76-83` đọc
   CHATGPT_USERNAME / CHATGPT_2FA_SECRET / CHATGPT_2FA từ environ SAU khi đã thử
   config → với nhóm tên legacy này `.env` thắng environ (ngược với precedence chính).
   Chỉ là fallback khi CLI+config trống — chủ đích, nhưng nên ghi chú.
3. **`.env` resolve theo cwd** (`settings.py:106`: `Path.cwd()/.env`) chứ không theo
   repo root — chạy `gpt-web` từ thư mục khác sẽ âm thầm mất config. Tests đang dựa
   vào hành vi này (chdir sang tmp_path).

## 2. Credential surface vô ý

### (a) Log / trace / debug — KHÔNG thấy in password/totp/token
- `AppConfig.masked_summary()` chỉ in boolean (`settings.py:35-45`).
- Toàn bộ JSON output CLI đi qua `_print_json` → `default_redactor.redact_json`
  (`gpt/debug.py:295-300`).
- `AutoLoginManager` chỉ log nhãn bước ("Entering password...", "Submitting computed
  2FA TOTP code...") — không giá trị (`gpt/auth/authenticator.py:283,313,380`).
- Trace bus structural-only theo hợp đồng (`gpt/utils/tracing.py:24-29`); prompt-debug
  dump redact bằng `redact_string` trước khi ghi (`gpt/gateway/runtime.py:1126`).
- `WEBGPT_DEBUG_RAW_OUTPUT=1` log raw model output text (không phải credential),
  `WEBGPT_DEBUG_PROTOCOL=1` log shape message (`gpt/gateway/server.py:2141-2162`).
- `cmd_env_export` in `ANTHROPIC_API_KEY` — đúng chức năng (eval vào shell); default
  là placeholder `sk-webgpt-local` (`session_runner.py:187`), không phải key thật.

### (b) File ghi credential ra disk — mode
- Token cache (`gpt/transport/token_manager.py:292-327`): `os.open(0o600)` + chmod +
  `os.replace` nguyên tử — **đúng claim T4-PERSIST**, contents never logged.
  Lưu ý: hiện **không active** — constructor duy nhất (`gpt/transport/hybrid.py:242`)
  không truyền `cache_dir` → cache disk tắt. Claim "0600" đúng về implementation,
  dormant về runtime.
- Account registry + `.cred`: write-then-chmod (`gpt/auth/accounts.py:134-140`,
  `201-203`) — cửa sổ ngắn ở perms umask (thường 0644) trước khi chmod 0600.
  Tương tự conversations store (`gpt/conversations.py:445-455`) và prompt-debug dump
  (`runtime.py:1151-1163`). Trên máy single-user rủi ro thấp; muốn kín thì dùng
  `os.open(..., 0o600)` kiểu token_manager.
- Profile dirs: `ensure_profile_dir` / `AccountStore.ensure` chmod 0700 — đạt.
- Prompt-debug dir: 0700 ở `_write_prompt_debug`; `_write_response_debug`
  (`runtime.py:1191-1216`) KHÔNG chmod dir 0700 nếu nó là người tạo dir đầu tiên — minor.
- **FINDING CHÍNH: `.env` của máy đang ở mode 0664** (`-rw-rw-r--`) và chứa
  CHATGPT_EMAIL/PASSWORD/TOTP_KEY → group/other đọc được. `.gitignore` chặn git
  (đạt — chỉ `.env.example` được track) nhưng permission file nên `chmod 600`.

### (c) `.env.example` — sạch
Cả 3 dòng credential để trống. Chứa đường dẫn thật `/home/light/Downloads/webgpt/...`
cho WEBGPT_ACCOUNTS_FILE/WEBGPT_PROFILES_ROOT (machine-specific, không phải secret).

### (d) Tests — không cred thật
Toàn bộ sample giả (`user@example.com`, `secret_pass`, JWT example nổi tiếng trong
`test_redaction.py:8`, `sk-testsecret...` fake trong `test_tracing.py:169`).

## 3. Env pollution — scrub coverage

Hai fixture autouse scrub cùng 13 biến (`tests/test_config_settings.py:6-17`,
`tests/test_debug_login.py:8-18`): CHATGPT_{EMAIL,PASSWORD,TOTP_KEY,USERNAME,2FA,
2FA_SECRET}, PROFILE_DIR, CDP_PORT, API_PORT, BROWSER_HEADLESS, DEFAULT_MODEL,
DEFAULT_EFFORT, MAX_WORKERS.

→ **Đủ phủ tập biến mà settings.py + luân login-credential đọc** (tức trọn bộ export
của shell máy: CHATGPT_*/CDP_PORT/API_PORT/BROWSER_HEADLESS/DEFAULT_MODEL/
DEFAULT_EFFORT/MAX_WORKERS/PROFILE_DIR). Không còn biến nào của load_config lọt.

Gap cần hành động:
1. **5 biến chưa được scrub ở đâu cả** — `cmd_env_export` (`debug.py:779-783`) đọc
   ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY, CLAUDE_DEFAULT_MODEL,
   CLAUDE_CODE_MAX_CONTEXT_TOKENS, CLAUDE_CODE_MAX_OUTPUT_TOKENS. Hiện chưa có test
   đi qua đây nên latent; thêm vào scrub list khi viết test cho `env`.
2. **Không có `tests/conftest.py`** — danh sách 13 biến bị nhân bản thủ công mỗi file;
   file test mới dễ quên. Đề xuất: dời lên conftest autouse toàn cục.
3. `WEBGPT_DEFAULT_ACCOUNT` (override default account, `accounts.py:326`) không nằm
   trong scrub list nào; `test_accounts.py` tự setenv per-test (monkeypatch tự undo).
   Nếu sau này shell export biến này, test dựng server có thể bị pin account âm thầm.
4. Latent: `get_config()` cache `_GLOBAL_CONFIG` trong process (`settings.py:142-149`)
   — scrub env không vô hiệu hóa cache cũ; hôm nay chỉ `test_config_settings.py`
   dùng `load_config()` trực tiếp nên chưa gây sự cố.

## 4. Auth flow (`gpt/auth/authenticator.py`)

Ba fix đều có implementation + test:

| Fix | Implementation | Test |
|---|---|---|
| SSO exclusion | `_SSO_EXCLUSION` trong 3 tuple selector (dòng 84-110); `_click_submit_button` phát hiện rời domain → go_back → thử candidate kế (492-527); `_is_allowed_auth_host/_left_auth_domain` (462-478) | `test_authenticator_fixes.py::test_continue_with_google_rejected_and_submit_button_used`, `::test_off_domain_redirect_is_rolled_back...`, `::test_submit_selector_tuples_exclude_sso...` |
| Nav-verify login click | Vòng Step-2 (221-245): mỗi click phải navigate qua `_wait_for_navigation` (480-490) mới break | `::test_dead_login_button_click_falls_back_to_next_selector` |
| MFA poll 60s | `mfa_input_timeout=60` (135), poll `wait_for` thay `is_visible` (340-373), detect qua URL pattern `_MFA_URL_RE` + grace deadline | `::test_late_mfa_challenge_still_fills_totp`, `::test_mfa_url_pattern_detection` |

Coverage gap nhỏ (không blocker): nhánh `seen_mfa_url` mà OTP không xuất hiện →
LoginError (369-373) và `otp_ready` mà thiếu secret → Invalid2FACodeError (376-377)
chưa có test; nhánh "không selector nào navigate → tiếp tục trên trang cũ" (240-244)
chưa có test.

Hang-path audit AutoLoginManager — **không có vòng lặp vô hạn**:
- Mọi loop đều có deadline (email 10s, password 35s, MFA 60s, landing =
  `timeout_seconds`, `_wait_for_navigation` có deadline; `while True` ở 485 vẫn check
  deadline trong thân).
- Tổng thời gian tệ nhất với default ≈ goto 45s + email 10s + password 35s + MFA 60s +
  landing 180s + pause ≈ ~6 phút.
- Lệch giữa hai caller: `cmd_account_login` bọc ngoài `asyncio.wait_for(timeout)`
  (`debug.py:180-183`) nhưng **`cmd_login` KHÔNG bọc** (`debug.py:473`) → `--timeout`
  chỉ giới hạn phase landing, tổng wall-time có thể vượt xa `--timeout` (vẫn bounded).
- **Resource leak**: nhánh fallback BrowserManager (`authenticator.py:185-191`) không
  bao giờ được `stop()` — khối `finally` (398-403) chỉ đóng khi `context` khác None,
  mà ở nhánh này `context=None` → browser process sống sót sau khi `login()` return
  (chỉ xảy ra khi package cloakbrowser thiếu).

## 5. Multi-account health/default (W-A1A4A2) — trạng thái: ĐÃ WIRE

- `resolve_default_account`: `gpt/auth/accounts.py:317-332` — precedence
  `WEBGPT_DEFAULT_ACCOUNT` env > registry `default_account`; tên không tồn tại bị bỏ qua.
- Cooldown 429/AuthRequired: `MultiAccountWorkerFactory._mark_failure`
  (`gpt/transport/multi_account.py:77-99`) → `AccountHealthTracker.mark_result`
  với `WEBGPT_ACCOUNT_COOLDOWN_SECONDS` (default 900, `multi_account.py:18-22`);
  tracker injectable clock (`gpt/transport/account_health.py:38-93`).
- Hook server: **cả hai server đều wire**
  - `gpt/gateway/server.py:338-375` — dựng per-account factories, tracker khi
    `_env_flag("WEBGPT_HEALTH_CHECK_ENABLED")` (mặc định OFF), default account qua
    `resolve_default_account(AccountStore())`; lifespan gọi `start_account_health_loop()`
    tại `:2299`; interval `WEBGPT_HEALTH_CHECK_INTERVAL` default 300s (`:529-537`);
    `_lease_session` pin `record.account_name` (`:587-593`).
  - `gpt/api/server.py` mirror cùng pattern (`:329-363`).
- Sticky-default semantics: healthy → luôn chọn default, không rotate
  (`multi_account.py:59-67`); cooldown hết hạn → round-robin pool còn lại; tất cả
  cooldown → fallback full list (`account_health.py:80-93`).
- Lưu ý vận hành: khi `WEBGPT_HEALTH_CHECK_ENABLED` bật, health loop chủ động kiểm
  tra từng account (mở browser headless mỗi vòng — tốn tài nguyên); khi OFF,
  `health=None` → default được tin tưởng vô điều kiện và failure-cooldown không chạy.

## Kết luận nhanh

- Precedence load_config: đạt; 3 lệch precedence bên ngoài (mục 1) — 1 actionable
  (sửa `.env.example`), 2 cần ghi chú.
- Rò rỉ vô ý: 0 chỗ in secret vào log/trace; findings thực tế = `.env` mode 0664
  (chmod 600), write-then-chmod window ở accounts/conversations/prompt-debug (minor),
  token-cache 0600 đúng chuẩn nhưng đang dormant.
- Env scrub: đủ cho shell hiện tại; bổ sung 5 biến ANTHROPIC_*/CLAUDE_* + cân nhắc
  conftest.py chung và WEBGPT_DEFAULT_ACCOUNT.
- Auth: 3/3 fix có test; cmd_login thiếu outer wait_for + leak BrowserManager fallback.
- Multi-account: đã wire cả gateway + api; health loop mặc định OFF do env-gated.
