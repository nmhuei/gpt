# MULTI-ACCOUNT-POOL RESEARCH — thiết kế pool nhiều account cho gateway (2026-08-26)

Research agent, READ-ONLY code + web cộng đồng. Bối cảnh: batch CTF chết 2 lần
ngày 26/08 vì quota token-weighted cạn trên account "personal" duy nhất. Hạ tầng
nền đã có: failover A3, health tracker + cooldown 429 (W-A1A4A2), breaker toàn
cục (BACKOFF-BREAKER), usage_poller + preflight_quota (QUOTA-PREFLIGHT). Nghiên
cứu này trả lời: pool nhiều account THỰC THỤ cần gì thêm, và breaker/poller có
cần per-account không. Quyết định triển khai thuộc owner.

---

## 0. TL;DR

| Câu hỏi | Trả lời |
|---|---|
| Một account bị limit có kéo cả pool nghỉ? | **Browser path (prod hiện tại): CÓ** — breaker singleton toàn tiến trình chặn mọi `acquire()` 90–600s bất kể account (`factory.py:114` mặc định `global_rate_limit_breaker()`, ROADMAP row BACKOFF-BREAKER tự ghi "multi-account cùng bị chặn"). **Hybrid path: KHÔNG có breaker nào** (`hybrid.py` 0 hit rate-limit) — chỉ health-cooldown 900s/account. |
| Breaker cần per-account? | **YES cho browser path.** Class sẵn sàng từ đầu (param `rate_limit_breaker`, design intent ghi rõ trong docstring) — chỉ cần truyền instance riêng mỗi account khi wire multi-account. Giữ global làm emergency-brake opt-in. |
| UsagePoller cần instance per-account? | **YES — nhưng không cần rewrite.** Kiến trúc 1 poller ↔ 1 credential ↔ 1 breaker đã map đúng 1:1; chỉ chạy N instance staggered advising N breaker riêng. Điểm nghẽn thật là credential: wham/usage cần OAuth bearer riêng từng account (+ `ChatGPT-Account-Id`). |
| Rotation đề xuất | Filter-then-rank "least-pressure": bỏ account cooldown/breaker-open/quota-cạn → xếp hạng used_percent ↑, consecutive_failures ↑, inflight ↓. Round-robin thuần là baseline đã có; cộng đồng 2026 đã chuyển hẳn sang rank. |

---

## A. Hiện trạng code (đối chiếu READ-ONLY)

### A1. Đã có sẵn nhiều hơn expectation

| Thành phần | File | Trạng thái |
|---|---|---|
| Registry named accounts (profile dir, cred 0600, sticky default, backup .bak.1-3) | `gpt/auth/accounts.py` | Hoàn chỉnh, production-grade |
| MultiAccountWorkerFactory: round-robin across available, sticky default khi khoẻ, tag `_webgpt_account_name`, rollback start() | `gpt/transport/multi_account.py` | Hoàn chỉnh |
| Health tracker per-account (status/cooldown_until/consecutive_failures, injectable clock) + periodic_health_loop env-gated OFF | `gpt/transport/account_health.py` | Hoàn chỉnh |
| Cooldown 429 per-account 900s (`WEBGPT_ACCOUNT_COOLDOWN_SECONDS`) | `multi_account.py:15-22` | Hoạt động |
| Failover quên binding khi RateLimited/AuthRequired/CommitUnknown-reconciled, cap 1/request | `gpt/transport/failover.py` | Hoàn chỉnh |
| CLI `--account NAME` repeatable (`action=append`) | `gpt/debug.py:1314-1318` | Hoạt động |
| Conversation pinning theo account (record.account_name persist qua các turn) | gateway `_lease_session` | Hoạt động |

⇒ Pool về khung xương **đã tồn tại**. Chỉnh thêm 2 account vào unit
(`--account personal --account second`) là round-robin chạy ngay. Vấn đề nằm ở
lớp tín hiệu limit bên dưới.

### A2. Breaker là TOÀN CỤC-PROCESS và chỉ wire trên browser path

- `factory.py:114`: `self.rate_limit_breaker = rate_limit_breaker or
  global_rate_limit_breaker()` — **mọi ChatGPTWorkerFactory không truyền breaker
  đều dùng chung một singleton**. Wiring multi-account ở
  `gateway/server.py:536-570` và `api/server.py:648-660` tạo factory KHÔNG
  truyền breaker ⇒ N account = 1 breaker.
- Trip sites duy nhất: `factory.py:226` (bootstrap RL) và `factory.py:276`
  (turn ended rate limited). Gate: `factory.py:198` `before_acquire()` raise
  `BackendCoolingDown` TRƯỚC semaphore.
- **Hybrid path không có breaker chút nào**: grep `rate_limit|RateLimited` trong
  `hybrid.py` = 0; curl_transport tự raise `RateLimited`
  (`curl_transport.py:1663`) mà không trip/gate breaker gì cả.
- Unit prod đang chạy `--transport browser --account personal` (systemd unit)
  ⇒ hôm nay global breaker đang ACTIVE và trùng với account duy nhất.

### A3. Gap chọn account khi breaker per-account (chặn trước scope S)

1. `_pick_name()` (`multi_account.py:52-75`) lọc bằng health tracker
   (cooldown 429) nhưng **không biết breaker state** — với breaker per-account,
   một account đang open vẫn bị pick rồi cả request chết `BackendCoolingDown`
   dù account khác khoẻ.
2. `lease()` không catch `BackendCoolingDown` để thử account kế tiếp.
3. `available_names()` fallback "tất cả đang cooldown ⇒ dùng full list"
   (`account_health.py:80-93`) — hợp lý cho 1 account, nguy hiểm cho pool:
   tất-cả-cạn sẽ bị hammer liên tục thay vì fail-fast.
4. `maybe_failover()` xử lý RateLimited/AuthRequired/CommitUnknown —
   `BackendCoolingDown` KHÔNG thuộc bảng failover và không được map đặc biệt ở
   cả 2 server (grep 0 hit) ⇒ nổi lên như lỗi chung.

### A4. Quota signal per-account: poller có nhưng chưa wire

- `UsagePoller` (`usage_poller.py`) build xong, test xanh
  (`tests/test_usage_poller.py`), **không được instantiate ở server/script nào**
  ngoài test. Dormancy contract tốt (OFF mặc định, injectable
  token_provider/http_get/clock).
- Credential duy nhất: codex OAuth `WEBGPT_CODEX_AUTH_JSON` (một auth.json).
  DECISIONS 2026-08-26: web-session AT bị codex backend từ chối (401) ⇒ poller
  per-account **bắt buộc** OAuth bundle riêng từng account
  (`scripts/codex_oauth_login.py` đã có helper).
- Shape wham/usage + header `ChatGPT-Account-Id`:
  `docs/reports/quota-pattern-research-2026-08-26.md` §A1 (canonical từ source
  OpenAI codex). Payload shape đang tiến hoá (`additional_rate_limits: null`
  làm parser khác chết 26-08) ⇒ giữ parse defensive như hiện tại.
- `scripts/preflight_quota.py`: CLI 1 account (TokenBundle cache theo profile
  hoặc --token), exit 0/2/3 — chưa có chế độ quét registry.

### A5. Surface đọc breaker toàn cục cần policy khi tách

- `api/server.py:495-514` advisory `anthropic-ratelimit-*` headers +
  `api/server.py:503`, `gateway/server.py:349` stats snapshot đều đọc
  `global_rate_limit_breaker()`. Khi per-account: remaining=0 chỉ khi TẤT CẢ
  account open (hoặc theo account của conversation đang hỏi).

---

## B. Pattern cộng đồng 2025–2026 (fetch 2026-08-26)

### B1. So sánh rotation strategy của các project cùng domain

| Project | Ngôn ngữ | Rotation | Ghi chú |
|---|---|---|---|
| router-for-me/CLIProxyAPI | Go | **Round-robin** thuần, pool `auths/` OAuth hot-load | Popular nhất; v6.10 tách usage-stats ra tool ngoài (CPA Usage Keeper, Quota Inspector query Management API per-account) |
| cs50victor/tokenproxy (Rust port của CLIProxyAPI) | Rust | **Filter→rank**: loại disabled/auth-failed/usage-limited/cooling-down/capability-mismatch → rank continuation-affinity > health > priority > smoothed latency > recent-failures | Đại diện hướng mới 2026: KHÔNG round-robin mù |
| FL-Penly/proxy-gate | Go | Drain-aware scoring + inflight tracking + conversation pinning; **wham polling per-account**; pool dir fsnotify hot-reload; admin UI + CLI enable/disable per email | Gần nhất với nhu cầu nhà mình (ChatGPT OAuth pool + wham) |
| wei-shaw/claude-relay-service | Node | Auto rotation + smart failover; sticky session qua header `session_id`; **cooldown TTL phân lớp theo lỗi** (503/5xx/auth/timeout, override per-account); UI hiển thị "unroutable reason" + remaining cooldown ETA + nút reset | Tham chiếu hay nhất cho cooldown taxonomy + observability |
| Octo-Lex/ChatGPT-Web2API (cùng domain ChatGPT Web, sống ~200 ngày) | Python/CDP | **1 profile = 1 account**, KHÔNG multi-account trong 1 process; scale bằng nginx round-robin NHIỀU INSTANCE | Popup rate-limit → dismiss + backoff ≤3; persistent ⇒ 429 chuẩn + Retry-After parse từ text popup; pacing 12s giữa request |
| gngpp/ninja (archived 04/2026) | Rust | token pool + **IP proxy pool** built-in | Chứng minh nhu cầu per-IP từ thời 2024-2025 |

### B2. Cooldown chéo (cross-cooldown)

Không thấy project nào mở cooldown CHUNG tự động khi 1 account dính 429 —
hướng phổ biến là per-account TTL theo loại lỗi (CRS), kèm nhận diện dạng lỗi
IP/challenge xử lý bằng đường riêng (nhà mình: challenge.py + DECISIONS §quota
"403/503 HTML challenge KHÔNG phải quota"). CRS là project duy nhất cung cấp
per-account static proxy IP — lý do ghi nguyên văn: lo nhiều account cùng 1 IP
bị ban. ⇒ Cross-brake nên là opt-in có điều kiện (≥K/N account dính 429 THẬT
trong cửa sổ T), không phải mặc định.

### B3. Rủi ro ban / fingerprint / IP khi đa đăng nhập

- Vector bị nêu rõ nhất: **payment method trùng** + tạo account dồn dập
  (bswen tổng hợp 2026-03); same-IP/fingerprint/phone ít được chứng minh định
  lượng nhưng là nỗi sợ vận hành số 1 trong cộng đồng reverse-proxy (CRS build
  hẳn tính năng proxy-per-account vì nó).
- ToS OpenAI: không có điều khoản cấm tường minh "one account per user", nhưng
  pooling consumer accounts phục vụ automation gần chắc chắn trái tinh thần;
  chính README proxy-gate cũng phải note điều này. Quyết định chấp nhận rủi ro
  thuộc owner.
- Kinh nghiệm nội bộ đã đúc kết: **1 account KHÔNG mở 2 fingerprint browser
  song song (session invalidate)** (DECISIONS customgpt-pilot) — profile
  CloakBrowser per-account hiện tại đã tách fingerprint đúng hướng.
- Nhịp dùng là tín hiệu hành vi mạnh nhất: chính sách ≤8–10 msg/ngày/account,
  burst ≤25/3h, preflight bắt buộc (DECISIONS 2026-08-26) PHẢI áp cho TỪNG
  account trong pool, không phải trung bình cả pool.

---

## C. Thiết kế đề xuất cho repo này

### C1. Scope S — POOL-PER-ACCT-BREAKER (~1 ngày, giải ngay sự cố hôm nay)

Mục tiêu: 1 account cạn quota ⇒ chỉ account đó nghỉ, pool còn lại phục vụ.

Đổi:

1. `gpt/gateway/server.py` (~536-556) + `gpt/api/server.py` (~648-659): trong
   vòng lặp tạo `account_factories`, thêm
   `factory_kwargs["rate_limit_breaker"] = <breaker-per-account>`.
   - Flag: `WEBGPT_BREAKER_SCOPE` = `auto` (default) | `global` | `per_account`.
     `auto`: ≥2 account → per_account; 1 account → global (giữ nguyên hành vi
     hiện tại, zero-risk rollback).
2. `gpt/transport/multi_account.py`:
   - `_pick_name()`: sau khi lọc health, bỏ tiếp factory có breaker đang
     open/half-open (thêm method nhẹ `accepting_leases() -> bool` lên cả
     `ChatGPTWorkerFactory` lẫn `HybridWorkerFactory` — hybrid luôn True cho
     đến khi có breaker riêng).
   - `lease()`: wrap vòng thử ≤N account: catch `BackendCoolingDown` từ
     factory được pick → thử tên kế tiếp trong pool; hết pool → raise lại
     (giữ fail-fast).
3. Stats/header: snapshot breaker per-account vào `/stats` (map name→snapshot);
   advisory ratelimit headers: remaining=0 chỉ khi tất cả account open.
4. Tests: `tests/test_multi_account.py` (skip-open-account, retry-next),
   `tests/test_backoff_breaker.py` (2 breaker độc lập).

KHÔNG đổi: hybrid path (chưa có breaker để tách), failover table, health loop.

### C2. Scope M — POOL-USAGE-PER-ACCT + least-pressure selection (2–3 ngày)

Prereq vận hành: mỗi account có OAuth bundle riêng (chạy
`scripts/codex_oauth_login.py` per account). Không có bundle ⇒ account đó
không có reading, selection coi như unknown-percent.

1. `gpt/transport/codex_auth.py`: hỗ trợ pool dir
   `~/.codex/accounts/<name>.auth.json` hoặc env
   `WEBGPT_CODEX_AUTH_JSON_<NAME>` (upper-case name); giữ dormant contract.
   Thêm header `ChatGPT-Account-Id` nếu payload/account_id yêu cầu (§A1
   research cũ).
2. `UsagePoller`: KHÔNG sửa logic — chạy N instance (1/account), mỗi instance
   `token_provider` riêng + advise vào breaker RIÊNG của account đó; stagger
   start (offset `poll_seconds/N`) tránh burst đồng loạ. Wire lifespan ở
   gateway/api server, env gate `WEBGPT_USAGE_POLL_SECONDS` giữ nguyên ý nghĩa.
3. `multi_account.py._pick_name()`: strategy env
   `WEBGPT_POOL_STRATEGY` = `sticky_default` (hiện tại) | `round_robin` |
   `least_pressure` (default MỚI khi ≥2 account): rank theo
   `used_percent` thấp nhất → `consecutive_failures` → inflight count (đếm
   leased per-account trong factory, rẻ).
4. Cross-brake opt-in: `WEBGPT_POOL_CROSS_BRAKE=K/N@T` (vd `2/3@600s`) — ≥K
   account dính 429 THẬT (không tính challenge) trong T giây ⇒ mở cửa sổ
   ngắn trên MỌI breaker (nghi throttle tầng IP). Default OFF.
5. `scripts/preflight_quota.py`: thêm `--all` quét registry, xuất JSON
   per-account {name, exit_code, primary%, secondary%, reset_at} cho
   coordinator chọn tập account trước batch.
6. Observability: `/stats` gộp per-account {breaker_state, used_percent,
   reset_at, cooldown_until, inflight} — mô phỏng "unroutable reason" panel
   của CRS.

### C3. Scope L — POOL-MANAGER (khi pool ≥3 account chạy ổn)

- Per-account proxy/IP binding: BrowserManager hiện KHÔNG có param proxy
  (grep trống) — phải thêm launch args CloakBrowser. CẢNH BÁO: IP datacenter
  tươi thường xấu hơn residential quen thuộc với Cloudflare; cân nhắc chỉ khi
  có bằng chứng throttle tầng IP thật.
- DOM Settings fallback đọc % usage khi thiếu OAuth (§A4 research cũ — cần 1
  lần capture selector thật).
- Batch scheduler chọn tập account theo headroom dự báo (usage_poller readings
  + reset_at) — nối với automation loop STATE/ROADMAP.
- Account lifecycle dashboard (login lại khi refresh chain chết — DECISIONS
  CODEX-AUTH).

### C4. Thứ tự thực hiện

S → verify bằng cách set 2 account + ép 429 giả trên 1 account (unit test +
live: đốt có kiểm soát 1 account nhỏ) → M khi có ≥2 OAuth bundle → L chỉ khi
pool thực dụng. Scope S standalone đã giải quyết đúng sự cố "1 account cạn kéo
chết batch": batch còn account thay thế.

---

## D. Trả lời trực tiếp 2 câu hỏi nghiên cứu

**Breaker per-account thay global?** YES cho browser path. Lý do kiến trúc:
cooldown toàn cục quy N account về throughput của 1 account — mất toàn bộ giá
trị pool; trong khi 429 do cạn quota token-weighted là tín hiệu ACCOUNT-scoped
(bằng chứng: hôm nay account personal cạn trong khi chưa test account nào
khác; §B7 research cũ quota "ma" cũng per-account). Tín hiệu IP-scoped thật
(challenge 403/503) đã có đường riêng challenge.py + cross-brake opt-in ở M.
Class breaker sinh ra đã hỗ trợ injection (docstring "can safely be shared …
that sharing is what makes the cooldown GLOBAL") — việc per-account là quay về
design intent, chi phí diff nhỏ.

**UsagePoller instance per-account?** YES — 1 instance/account advising
breakers tương ứng. Không viết orchestrator mới: class đã injectable trọn vẹn
(token_provider/http_get/clock/breaker), dormancy contract giữ nguyên; điểm
công sức thật là credential per-account (codex_auth pool paths) chứ không
phải poller loop.

---

## E. Rủi ro (ghi rõ — quyết định thuộc owner)

1. **ToS/ban-wave**: pooling nhiều consumer account cho automation gateway gần
   chắc trái tinh thần ToS dù không có điều khoản cấm tường minh; vector phát
   hiện được cộng đồng nêu: payment trùng, tạo account dồn dập, nhịp dùng bất
   thường, đa login cùng IP. Mitigation khả dụng: pacing per-account nghiêm
   (chính sách DECISIONS hiện có), fingerprint tách per-profile (đã có),
   KHÔNG dùng chung 1 account cho 2 browser song song (DECISIONS), giới hạn
   pool nhỏ (2–3) trước khi nghĩ scale.
2. **Same-IP đa tài khoản**: rủi ro tồn tại nhưng chưa có bằng chứng định
   lượng; per-proxy L-scope tự mang rủi ro CF reputation. Khuyến nghị kỹ thuật
   trung lập: giữ 1 residential IP quen thuộc, pool nhỏ, theo dõi tỷ lệ
   challenge/tài khoản làm tín hiệu sớm.
3. **Quota "ma"/shape drift**: used_percent đôi khi tự drain/trôi (§B7 research
   cũ); payload thêm field mới làm parser khác chết (26-08). Giữ clamp +
   defensive parse + không park vĩnh viễn dựa 1 reading.
4. **All-exhausted behavior**: fallback "dùng full list khi tất cả cooldown"
   sẽ hammer cả pool; scope S/M cần chế độ fail-fast 503 rõ ràng cho client
   thay vì retry vô hạn (client Claude Code đã có backoff riêng).
5. **Credential sprawl**: N auth.json + N cred file = bề mặt secret lớn hơn;
   tất cả 0600 trong ~/Downloads/webgpt (đã là pattern hiện tại).

## F. Nguồn

Code (fetch 2026-08-26): các đường dẫn file nêu trên; systemd unit
`~/.config/systemd/user/webgpt-gateway.service`.

Web (toàn bộ fetch 2026-08-26):
1. github.com/router-for-me/CLIProxyAPI (README)
2. github.com/cs50victor/tokenproxy (README)
3. github.com/FL-Penly/proxy-gate (README)
4. github.com/wei-shaw/claude-relay-service (README)
5. github.com/Octo-Lex/ChatGPT-Web2API (README)
6. github.com/gngpp/ninja (README, archived 2026-04-09)
7. docs.bswen.com multiple-openai-accounts-ban-risk (2026-03-22)
8. docs/reports/quota-pattern-research-2026-08-26.md (research nội bộ, §A1/§B7/§D)

Hạn chế: WebSearch tool trả kết quả hạn chế trong phiên; phân tích ban-risk
dựa trên README các project + 1 nguồn tổng hợp, không có dữ liệu định lượng
về tỷ lệ ban same-IP.
