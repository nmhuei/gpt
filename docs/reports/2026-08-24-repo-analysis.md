# Báo cáo phân tích & kiểm thử repo `gpt` (WebGPT)

**Ngày:** 2026-08-24 · **Nhánh:** `main` · **HEAD:** `efcd95b`
**Phạm vi:** 5 subagent phân tích song song (kiến trúc, auth, transport, orchestrator/gateway, core utils) + chạy toàn bộ test suite.

---

## 1. Tóm tắt điều hành

| Hạng mục | Kết quả |
|---|---|
| Full test suite (`pytest tests/`) | **378 passed · 0 failed · 0 skipped · 0 error** (6.34s) |
| Import-check các module chính | OK, không thiếu dependency |
| Trạng thái working tree | 20 file sửa (+1067/−238), 9 file mới chưa track — tính năng multi-account đang phát triển dở |
| Chất lượng code tổng thể | Cao ở lớp lõi (fail-closed, durability, error taxonomy); có nợ kỹ thuật về trùng lặp và hardcode |
| Rủi ro lớn nhất | Gateway **không có auth HTTP**; credential lưu **plaintext**; orchestrator chạy AI-sinh code không sandbox (**RCE by design**) |

Repo này là **WebGPT Toolkit v2.0.0** — "Evidence-gated ChatGPT Web session controller": điều khiển conversation ChatGPT Web qua trình duyệt thật (Playwright/CloakBrowser), phơi ra thành local API gateway tương thích OpenAI/Anthropic, kèm orchestrator tự động giải CTF bằng Claude Code CLI và pipeline phân tích PCAP 5 tầng.

---

## 2. Kiến trúc tổng thể

```
.env → AppConfig ──► AutoLoginManager (CloakBrowser, email/password/TOTP)
                        │ profiles tại ~/Downloads/webgpt/profiles/<name>
                        ▼
gpt/transport/   BrowserManager → ChatGPTWebSession (state machine)
                 ├── factory.py         (worker pool browser)
                 ├── hybrid.py          (1 page browser lấy token → pool curl_cffi)
                 ├── multi_account.py   (round-robin N account)
                 └── token_manager.py   (TokenBundle, refresh 30', SentinelTokens)
                        ▼
gpt/gateway/     CompletionRuntime (turn engine, tool-correction loop,
                 pending/reconcile durability) → Starlette server
                 /v1/chat/completions · /v1/responses · /v1/messages
                        ▲ ANTHROPIC_BASE_URL=127.0.0.1:18000
gpt/orchestrator/ MasterAgentOrchestrator → ClaudeCodeSessionRunner
                  → SwarmRaceSolver (race 8 chiến lược giải CTF)
```

Thiết kế cốt lõi: **không giả định endpoint nội bộ ChatGPT** — UI semantic driver là đường ổn định; protocol replay chỉ bật khi có fingerprint xác minh từ ≥2 experiment ("evidence-gated", fail-closed).

### Phân hệ chính

| Thư mục / file | Vai trò |
|---|---|
| `gpt/debug.py` (~1212 dòng) | CLI chính `gpt-web`: login/account/setup/probe/doctor/send/experiment/api-server/mcp-bridge |
| `gpt/auth/` | `AutoLoginManager` (auto-login zero-interaction), `totp.py` (PyOTP), `accounts.py` *(mới)* — AccountStore metadata-only |
| `gpt/transport/` | State machine session, worker pool, hybrid curl_cffi, multi-account round-robin |
| `gpt/drivers/` | UI driver (ổn định) + protocol replay driver (gated) |
| `gpt/gateway/`, `gpt/api/` | API facade đa protocol; `api/server.py` là bản legacy mà test fault-injection import trực tiếp |
| `gpt/orchestrator/` | Dispatch Claude Code CLI, race solver swarm |
| `gpt/reverse/` | DOM/network capture, redact, ProtocolLedger, replay |
| `pcap_analyzer/` | Pipeline 5 tầng: input → Zeek → Suricata/RITA → MITRE ATT&CK → report |
| `solve_fast.py`, `solve_v2_fast.py` *(root)* | Solver timing side-channel CTF độc lập (artifact một lần, chưa track git) |

---

## 3. Kết quả kiểm thử

### 3.1 Full suite

```
$ python3 -m pytest tests/ -q
378 passed in 6.34s
```

### 3.2 Chi tiết theo phân hệ (do subagent chạy riêng)

| Phân hệ | Bộ test | Kết quả |
|---|---|---|
| Auth (`accounts.py`, login flow CLI) | `test_accounts.py` + `test_debug_login.py` | **9 passed** (0.15s) |
| Transport (session, hybrid, dom_probe, multi-account) | `test_session.py`, `test_dom_probe.py`, `test_multi_account.py` | **14 passed** (0.22s; dual-backend asyncio+trio) |
| Orchestrator/Gateway fault injection | `test_fault_injection.py` | **15 passed** (0.25s) |
| Core utils (conversations, toolcall, prompt intent) | `test_conversations.py`, `test_tool_transpiler.py`, `test_prompt_intent_matrix.py` | **69 passed** (0.44s) |

Không có test nào fail, không thiếu dependency (Python 3.13.14, pytest 9.0.3).

**Lưu ý phủ sóng test:** fault-injection mock hoàn toàn tầng browser (`get_or_create_session` bằng AsyncMock) — xác nhận *phân loại lỗi* đúng chứ không xác nhận hệ thống *chịu* lỗi thật. Suite này import `gpt.api.server` (bản legacy), tức `gpt.gateway.server` mới (multi-account/mock-backend) chưa được phủ trực tiếp dù phần lớn logic chung.

---

## 4. Phân tích chi tiết từng phân hệ

### 4.1 Auth (`gpt/auth/`)

- **Login flow:** `AutoLoginManager.login()` — CloakBrowser persistent context (chống fingerprint), điền email/password/TOTP với typing delay giả lập người; phát hiện CAPTCHA/challenge **chỉ đọc** (không bao giờ bypass); phân cấp lỗi `InvalidCredentialsError`, `Invalid2FACodeError`, `CaptchaChallengeError`.
- **`accounts.py` (392 dòng, mới):** registry JSON metadata-only (atomic write, chmod 0600/0700, validate tên chống path traversal); credentials lưu file riêng `<name>.cred` dạng pipe-delimited **plaintext**, chmod 0600.
- **TOTP:** chấp nhận cả seed base32 lẫn mã 6-digit dùng ngay.
- **Khớp tài liệu:** `AUTH_AND_LOGIN_GUIDE.md` khớp code (profile root, manual login verify `/api/auth/session`, khuyến nghị stdin).

### 4.2 Transport (`gpt/transport/`)

- **`session.py` — state machine** `BOOTING→READY→…` với nhánh lỗi typed (`AUTH_REQUIRED`, `RATE_LIMITED`, `COMMIT_UNKNOWN`, `PROTOCOL_CHANGED`…). Điểm thiết kế hay nhất: sau khi submit nếu exception → `CommitUnknown` thay vì cho retry mù; `reconcile()` đọc history authoritative của web để xác nhận turn đã persist chưa.
- **`hybrid.py`:** 1 page browser lấy token dùng chung → pool bounded curl_cffi sessions (TLS impersonate Chrome, stream SSE). `reconcile()` của HTTP path luôn raise `CommitUnknown` — an toàn, tránh resend trùng.
- **`multi_account.py` (74 dòng):** round-robin lease qua `asyncio.Lock`, pin account theo tên, gắn `_webgpt_account_name`. **Chưa có**: rate-limit per-account, health-check/failover tự động.
- **Không retry/backoff tự động ở bất kỳ tầng nào** — triết lý fail-safe phân loại lỗi trước khi quyết định resend.
- Thread-safety asyncio đúng đắn (lock quanh cursor, idle map, token extract/refresh).

### 4.3 Orchestrator (`gpt/orchestrator/`)

- **`session_runner.py` (358 dòng):** spawn `claude -p <prompt> --dangerously-skip-permissions --print`, env ép gateway local `127.0.0.1:18000`, timeout 300s. Vòng solve 5 attempt xoay chiến thuật (TRIAGE → ERROR_FEEDBACK → ALTERNATIVE_VECTOR → CLEAN_REBOOT → EXTREME_REASONING); trích code block cuối → chạy `solve.py`; regex bắt flag; thất bại → `NEEDS_HUMAN_REVIEW.md`.
- **`race_solver.py` (191 dòng):** race tối đa 8 worker, mỗi worker một góc tấn công (timing oracle Z-score/IQR, async fuzzing 100 req/s, differential, auth-bypass, PRNG cryptanalysis, AST RE, exploit synthesis), context cách ly qua `.swarm_scratch/worker_N`; winner đầu tiên set `stop_event`, copy artifact về gốc.
- **Fault handling:** `asyncio.gather(..., return_exceptions=True)` nuốt exception worker crash; `ensure_instance_live()` poll vô hạn mỗi 5s không deadline tổng.

### 4.4 Gateway (`gpt/gateway/`)

- **Endpoints:** `/health`, `/healthz`, `/readyz`, `/models`, `/v1/chat/completions`, `/v1/responses`, `/v1/messages` (+`count_tokens`) — mỗi route có cả bản có/không tiền tố `/v1`.
- **Runtime (1114 dòng):** render prompt (compact tại 200K chars) → `mark_pending` → lease session → send (120s generation timeout) → parse tool calls → correction loop tối đa 2 lượt (TOOL_REFUSAL, MULTI_TOOL, INCOMPLETE_FANOUT, FALSE_COMPLETION, MALFORMED_TOOL…) → commit/reconcile.
- **Trace middleware** ghi metrics mọi request `/v1/*`.

### 4.5 Core utils

- **`conversations.py` (318 dòng):** fingerprint SHA-256 canonical, prefix-matching request↔conversation web, persistence opt-in atomic + flock + TTL + LRU evict (max 64), fail-open có chủ ý khi file hỏng. Chất lượng cao.
- **`toolcall.py` (864 dòng):** transpiler 3 định dạng đầu vào (legacy sentinel JSON, DSML, XML chính) → OpenAI function calls; fail-closed nghiêm ngặt; mask markdown code trước khi dò sentinel; virtual Write tool dịch thành script Python base64 với **2 lớp validation workspace-escape** (gateway-side + client-side recheck), atomic write, syntax-check. Điểm fragile: parser regex thay vì XML parser thật; coerce số trên string param khi thiếu schema.
- **`debug.py` (1212 dòng):** CLI monolith đảm nhiệm quá nhiều subcommand; boilerplate BrowserManager lặp; nhưng xử lý credential cẩn thận (redact mọi JSON output, không log secret).

### 4.6 Solver scripts (`solve_fast.py` vs `solve_v2_fast.py`)

Timing side-channel khôi phục token 48 ký tự qua latency `/api/authenticate`:

| Khía cạnh | v1 (249 dòng) | v2 (661 dòng) |
|---|---|---|
| Thuật toán | Single-stage, control anchor `'!'*48` | Two-stage adaptive: sweep nhanh → top-K(6) → đo sâu |
| Connection pool | Session thread-local mặc định | HTTPAdapter pool tùy chỉnh + Retry(total=0) |
| URL sync | Mỗi request | Có lock + **auto reset checkpoint** khi container đổi |
| Sample lỗi | Bỏ qua (baseline lệch khi mạng xấu) | Retry vô hạn (rủi ro treo khi target chết) |

Trùng lặp nặng (~oracle/checkpoint/flag-regex copy gần nguyên văn); cả hai hardcode URL instance CTF vào source.

---

## 5. Sổ đăng ký rủi ro (risk register)

| # | Mức | Vấn đề | Vị trí |
|---|---|---|---|
| 1 | 🔴 Cao | **Gateway không có auth HTTP/API key** — chỉ cần `--host 0.0.0.0` là toàn bộ proxy ra browser ChatGPT đã đăng nhập lộ ra mạng; không rate-limit | `gpt/gateway/server.py` |
| 2 | 🔴 Cao | **RCE by design**: `--dangerously-skip-permissions` + auto-trust `~/.claude.json` + thực thi `solve.py` AI sinh không sandbox — challenge có prompt-injection trong file sẽ được chạy thoải mái | `orchestrator/session_runner.py`, `race_solver.py` |
| 3 | 🔴 Cao | **Credentials plaintext** trên disk (password + TOTP seed cùng file `.cred`) — không keyring/encryption-at-rest | `auth/accounts.py` |
| 4 | 🟠 TB | SSL verify tắt (`CERT_NONE`) toàn bộ polling; prompt còn ép AI viết `verify=False` | `session_runner.py`, solver scripts |
| 5 | 🟠 TB | Hardcode đường dẫn cá nhân `/home/light/.local/bin/claude`, `/home/light/.claude.json`, port 18000 — không portable | orchestrator |
| 6 | 🟠 TB | Loop vô hạn không deadline: `ensure_instance_live()`, `measure_token()` (v2) | orchestrator, solver |
| 7 | 🟠 TB | Fork gần-copy `gateway/server.py` ↔ `api/server.py` (~1630 dòng giống nhau) — drift tiềm ẩn; test suite phủ bản legacy | gateway/api |
| 8 | 🟠 TB | `_conversation_locks`, `_response_sessions` tăng trưởng không giới hạn (memory leak chậm) | gateway/server |
| 9 | 🟡 Thấp | `reboot_session()` xóa `~/.claude/projects` theo substring match — rủi ro xóa nhầm project khác | session_runner |
| 10 | 🟡 Thấp | `stop_event` swarm chỉ dừng giữa các vòng — worker đang chạy turn 300s vẫn chạy hết | race_solver |
| 11 | 🟡 Thấp | File rác trong tree: `diff.txt`, `scratch/`, `gpt.egg-info` committed; solver CTF nằm root | repo |
| 12 | 🟡 Thấp | Shim module giai đoạn restructure (root ↔ `utils/`) chưa dọn; README lệch pyproject (requirements thiếu `curl_cffi`); không CI | repo |
| 13 | 🟡 Thấp | Toàn hệ thống auto-login ChatGPT vi phạm ToS — rủi ro ban account/pháp lý (docs tự nhận thức) | chung |

---

## 6. Điểm mạnh nổi bật

1. **Fail-closed nhất quán**: mọi trạng thái mơ hồ (commit unknown, malformed tool, prose lẫn tool call) đều chặn thay vì đoán.
2. **Pending/reconcile durability**: two-phase commit cho turn gửi lên web — hiếm thấy ở project cùng loại.
3. **Error taxonomy typed** 16 kiểu exception → status/retryable, header `x-should-retry`.
4. **Secret hygiene tốt ở runtime**: không log password/TOTP/token; debug dump chmod 0600 + redaction.
5. **Evidence-gated protocol replay**: không tin endpoint nội bộ cho đến khi ≥2 experiment xác minh fingerprint.
6. **Test discipline tốt**: 378 pass, dual-backend anyio (asyncio+trio), fault injection behavioral.

## 7. Khuyến nghị ưu tiên

1. Thêm **API-key middleware + rate limit** cho gateway, cảnh báo/nhắc khi bind ngoài loopback.
2. Chạy orchestrator trong **container/sandbox** riêng; loại bỏ auto-trust workspace.
3. Mã hóa `.cred` qua keyring hoặc tối thiểu age/GPG; cân nhắc chuyển profile root khỏi `~/Downloads`.
4. Hợp nhất `gateway/server.py` với `api/server.py` (hoặc chuyển test sang bản mới).
5. Đặt deadline tổng cho `ensure_instance_live` và retry cap cho `measure_token` v2.
6. Dọn shim modules sau khi restructure xong; bỏ v1 solver hoặc extract common oracle; thêm CI chạy pytest.
