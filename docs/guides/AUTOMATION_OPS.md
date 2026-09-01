# AUTOMATION OPS — Hạ tầng chạy tự động 24/7 (WebGPT)

Tài liệu khôi phục vận hành: kiến trúc automation hiện có, cách khởi động lại
session agent khi chết, các log/report cần đọc, và cách tắt toàn bộ.

## 1. Kiến trúc tổng quan

```
┌──────────────────────────────────────────────────────────────┐
│  systemd --user (sống cùng user session, không phụ thuộc tmux)│
│                                                              │
│  webgpt-gateway.service   → gateway HTTP :18000 (đã có sẵn)  │
│                                                              │
│  webgpt-watchdog.timer    ─ mỗi 5 phút (:0/5, Persistent)     │
│    └─ webgpt-watchdog.service (oneshot)                      │
│         └─ scripts/webgpt-watchdog.sh                        │
│            curl /health; fail 3 lần liên tiếp                │
│            → systemctl --user restart webgpt-gateway.service │
│                                                              │
│  webgpt-auto-review.timer ─ daily 04:17 (Persistent)          │
│    └─ webgpt-auto-review.service (oneshot)                   │
│         └─ scripts/auto_review.sh                            │
│            pytest + ruff + git status/log                    │
│            → docs/reports/auto-review/auto-review-*.md       │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  Session Claude trong tmux (agent tương tác, sống qua SSH cut)│
│    cron-in-session: dispatch subagent mỗi 20 phút             │
└──────────────────────────────────────────────────────────────┘
```

### Thành phần

| Thành phần | Đường dẫn | Lịch | Vai trò |
|---|---|---|---|
| Gateway service | `~/.config/systemd/user/webgpt-gateway.service` | — | API gateway reverse trên `127.0.0.1:18000` |
| Watchdog script | `/home/light/GitHub/gpt/scripts/webgpt-watchdog.sh` | mỗi 5 phút (`*:0/5`) | Kiểm tra `/health`; fail 3 lần liên tiếp (state file `/tmp/webgpt-watchdog-fail-count`) thì restart gateway |
| Watchdog timer/service | `~/.config/systemd/user/webgpt-watchdog.{timer,service}` | oneshot | Gọi watchdog script |
| Auto-review script | `/home/light/GitHub/gpt/scripts/auto_review.sh` | daily 04:17 | Chạy pytest/ruff/git, sinh report markdown |
| Auto-review timer/service | `~/.config/systemd/user/webgpt-auto-review.{timer,service}` | oneshot | Gọi auto-review script |
| Cron-in-session | bên trong session Claude (tmux) | mỗi 20 phút | Dispatch subagent tự động làm việc |

Ghi chú:
- `Persistent=true`: nếu máy tắt đúng giờ chạy, timer sẽ bù lần chạy nhỡ ngay khi user session khởi động lại.
- Watchdog luôn exit 0 để timer không báo failed; toàn bộ sự kiện ghi vào log file.
- Auto-review giữ tối đa **50** report mới nhất trong `docs/reports/auto-review/`, tự xoá file cũ hơn.
- Không đụng tới unit `webgpt-gateway.service` khi chỉnh sửa hạ tầng này — watchdog chỉ restart nó qua lệnh systemctl.

## 2. Khi session Claude chết — khôi phục thế nào

Session agent chính chạy trong **tmux** để sống qua disconnect SSH/mất terminal.

### Bước 1 — kiểm tra/ tạo session tmux

```bash
tmux ls                                   # xem session còn không
tmux new -s agent                         # tạo session tên "agent" nếu chưa có
tmux attach -t agent                      # vào lại session đang tồn tại
```

### Bước 2 — chạy lại Claude với context cũ

Trong tmux, ở thư mục repo:

```bash
cd /home/light/GitHub/gpt
claude --continue        # nối tiếp phiên gần nhất của project này
# hoặc: claude --resume  # chọn thủ công một phiên trong danh sách
```

Chạy claude *bên trong* tmux là điểm mấu chốt: tmux giữ tiến trình sống khi
terminal/SSH bị ngắt; chỉ cần `tmux attach -t agent` là quay lại đúng màn hình.

### Bước 3 — bật lại cron-in-session (nếu cần)

Sau khi claude lên tiếng bình thường, yêu cầu nó tái lập lịch dispatch subagent
mỗi 20 phút như trước (cron-in-session là cơ chế nằm trong phiên Claude,
không phải crontab hệ thống — chết cùng phiên).

### Bước 4 — kiểm tra gateway

```bash
curl -sf -m 2 http://127.0.0.1:18000/health
systemctl --user status webgpt-gateway.service
tail -20 ~/.local/share/webgpt/logs/watchdog.log
```

Gateway không cần khởi động tay: watchdog timer sẽ tự restart nếu health fail
3 lần liên tiếp.

## 3. Log & report cần đọc để biết tình hình

| Cần biết | Xem |
|---|---|
| Gateway còn sống không | `curl -sf -m 2 http://127.0.0.1:18000/health` |
| Lịch sử health/restart | `~/.local/share/webgpt/logs/watchdog.log` |
| Kết quả test/lint mới nhất | file mới nhất trong `/home/light/GitHub/gpt/docs/reports/auto-review/` (`ls -1t .../auto-review-*.md \| head`) |
| Timer còn hoạt động | `systemctl --user list-timers \| grep webgpt` |
| Lần chạy timer gần nhất / lỗi | `journalctl --user -u webgpt-watchdog.service -n 50`, `journalctl --user -u webgpt-auto-review.service -n 50` |
| Trạng thái gateway chi tiết | `journalctl --user -u webgpt-gateway.service -n 100` |

## 4. Tắt toàn bộ automation

```bash
# Tắt + vô hiệu hoá cả hai timer (không đụng gateway service)
systemctl --user disable --now webgpt-watchdog.timer webgpt-auto-review.timer

# Xác nhận
systemctl --user list-timers | grep webgpt   # phải trống

# Xoá cron-in-session (nếu phiên Claude còn sống): yêu cầu phiên huỷ lịch dispatch subagent.
# Nếu muốn dọn hẳn file unit:
rm ~/.config/systemd/user/webgpt-watchdog.service \
   ~/.config/systemd/user/webgpt-watchdog.timer \
   ~/.config/systemd/user/webgpt-auto-review.service \
   ~/.config/systemd/user/webgpt-auto-review.timer
systemctl --user daemon-reload
```

Dừng gateway (chỉ khi thực sự cần):

```bash
systemctl --user stop webgpt-gateway.service
```

Lưu ý: nếu chỉ disable timer mà không stop gateway, watchdog cũng ngừng chạy
(timer tắt) nên gateway không còn ai tự restart — hãy nhớ điều đó khi tắt.

## 5. Chạy tay (không chờ timer)

```bash
/home/light/GitHub/gpt/scripts/webgpt-watchdog.sh   # 1 lần kiểm tra health
/home/light/GitHub/gpt/scripts/auto_review.sh       # 1 lần auto-review
```

## 6. Flag môi trường gateway (`WEBGPT_*`)

Các flag mới thêm gần đây (2026-08-24/25, bổ sung đợt 2026-08-26) — đặt trong
unit systemd hoặc `.env` (trước khi restart gateway). Mặc định đã hợp lý; chỉ chỉnh khi có lý do rõ.

| Flag | Mặc định | Tác dụng | Khi nào chỉnh |
|---|---|---|---|
| `WEBGPT_RATELIMIT_COOLDOWN_SECONDS` | `90` | Breaker rate-limit toàn tiến trình: lần 429 đầu mở cửa sổ cooldown, mọi acquire fail nhanh thay vì resend đốt quota | RL dày → tăng; `0` tắt breaker (trở lại failover-resend như cũ) |
| `WEBGPT_RATELIMIT_MAX_COOLDOWN_SECONDS` | `600` | Trần backoff: probe half-open lại dính RL thì cửa sổ ×2, tối đa giá trị này | Muốn trần ngắn hơn khi RL thường hồi <10 phút |
| `WEBGPT_PROMPT_BUDGET_CHARS` | `0` (tắt) | Trim payload trước khi send xuống ngưỡng chars — root cause chống RL (≤10k pass ~94%, >10k ~35%) | Bật `10000` khi gateway bị RL do payload lớn; KHÔNG hạ dưới budget tool-schema |
| `WEBGPT_LOGIN_DEADLINE_SECONDS` | timeout + 180s grace | Tổng deadline auto-login (`gpt-web login`), vượt → SystemExit(2) | Máy chậm / 2FA chờ lâu → tăng |
| `WEBGPT_RESPONSE_SESSION_CAP` | `512` | LRU bound bản đồ response_id→session (chống leak RAM) | Gần như không cần đụng |
| `WEBGPT_HYBRID_EVENT_QUEUE_CAP` | `512` | Cap hàng đợi event của transport hybrid (chống ùn RAM khi client đọc chậm) | Client cực chậm + turn rất dài → tăng |
| `WEBGPT_CANONICAL_MEMO_MAX` | `256` | LRU memo canonical-messages + fingerprint (mỗi entry giữ bản copy transcript) | Máy ít RAM → hạ (vd 64); 256 entry có thể pin hàng chục MB với history claude-code dài |
| `WEBGPT_DEBUG_MAX_FILES` | `500` | Số file tối đa mỗi vị trí prompt-debug/trace giữ lại (prune cũ nhất, có log) | Debug sâu → tăng; disk nhỏ → giảm |
| `WEBGPT_HISTORY_CACHE_MAX` | `128` | LRU bound cache turn trong `ChatGPTWebSession` (RAM #1 audit 2026-08-25) | `0` = vô hạn như cũ (chỉ dùng ngắn hạn) |
| `WEBGPT_WORKER_AFFINITY` | bật | Ghép conversation→worker (bỏ navigate open() mỗi turn miss, tiết kiệm 2-6s/miss) | Rollback: `0`/`false`/`no`/`off` trả về LIFO thuần |
| `WEBGPT_MAX_CORRECTIONS` | `2` | Số vòng sửa tool-call malformed tối đa mỗi turn (lý do protocol-shaped bị chặn cứng ở 2 con) | Turn hay sửa nhiều bước → tăng 3-4 (đã từng chạy 4 trên unit) |
| `WEBGPT_MAX_TOOL_CALLS_PER_TURN` | `3` | Trần số invoke parse được trong 1 turn web trước khi dính correction MULTI_TOOL (P1-3 bounded multi-tool) | CLI hay gộp Read+Edit+Write một lượt → giữ 3; muốn strict một-call-per-turn như cũ → `1` |
| `WEBGPT_FALSE_COMPLETION_BREAKER` | `12` | Breaker false-completion per-request: N lần lặp cùng lý do FALSE_COMPLETION thì ngừng vòng correction thay vì lặp mãi | Correction loop dày vì model cứng đầu → hạ; muốn kiên nhẫn hơn → tăng |
| `WEBGPT_NOOP_REPEAT_SKIP` | `5` | Metronome no-op: N lần liên tiếp commit nội dung giống hệt thì skip (chống lặp vô dụng) | Để mặc định; task thật sự cần thử lại y hệt nhiều lần → tăng |
| `WEBGPT_STREAM_DEADLINE_SECONDS` | queue + generation + 30s | Trần thời gian tổng một stream SSE, chống SDK treo mãi | Override tay phải ≥ queue + (1+MAX_CORRECTIONS)×generation + slack, nếu không kill nhầm vòng correction |
| `WEBGPT_TOOL_PROTOCOL` | `xml` | Định dạng tool protocol dạy cho Web: `xml` \| `json-fn` \| `both` \| `soft` | Unit đang chạy `soft` (chuẩn đã chứng minh); đổi giữa chừng không được — schema phải ổn định suốt session (409) |
| `WEBGPT_CODEX_SSE` | `0` (tắt) | Opt-in nhánh gửi authed `/backend-api/codex/responses` (Responses API qua SSE, không cần sentinel/turnstile — chỉ Bearer access token + cookie jar sau CF); truthy: `1/true/yes/on` | Chưa bật được: liveprobe 2026-08-25 dính 401 (recipe chờ token codex OAuth riêng) — chỉ bật sau khi POST thật verify thành công |
| `WEBGPT_FCONV_PREPARE` | `0` (tắt) | Opt-in chuỗi authed f/conversation mới: prepare → PoW → conduit_token trước khi POST (thay sentinel in-page); OFF giữ nguyên hành vi cũ byte-for-byte | Đang implement / chờ live-verify — chưa bật trên unit; chỉ bật để A/B khi recipe đã chứng minh sống |
| `WEBGPT_IMAGE_PLACEHOLDER` | bật (`1`) | Phần ảnh không hỗ trợ trong payload render thành placeholder text ("không xem được ảnh") thay vì âm thầm drop | Kill-switch rollback: `0`/`false`/`no`/`off` trả về silent-drop như cũ |
| `WEBGPT_SENTINEL_SDK` | bật | Mint sentinel qua SentinelSDK in-page (inject sdk.js) | Rollback: `0` quay đúng flow prepare/finalize/legacy cũ, không inject sdk.js |
| `WEBGPT_SENTINEL_CACHE` | bật | Cache TokenBundle sentinel giữa các turn (tránh mint lại mỗi turn) | `0` = tắt cache, mint mỗi turn như trước |
| `WEBGPT_CODEX_AUTH_JSON` | chưa đặt (OFF) | Path tới `auth.json` codex (~/.codex/auth.json): master switch module token-source OAuth codex — refresh có flock chống đua single-use rotation. Module đang dormant, CHƯA wire vào TokenManager/factory | Giữ OFF đến khi tích hợp xong; chỉ đặt path khi test riêng module này |
| `WEBGPT_CODEX_CLIENT_ID` | client công khai codex-rs | Override client id cho refresh grant OAuth | Chỉ có tác dụng khi `CODEX_AUTH_JSON` đã đặt |
| `WEBGPT_PROMPT_DEBUG_DIR` | chưa đặt (tắt) | Thư mục dump prompt đã redact gửi lên Web (mode 0600, ngoài TraceBus — opt-in debug/evidence) | Bật khi debug sâu hoặc lấy bằng chứng; nhớ tắt sau vì chứa nội dung prompt |
| `WEBGPT_MODEL_ALIAS` | chưa đặt (OFF) | Map model CLI yêu cầu → slug ChatGPT Web + effort pin tùy chọn: JSON `'{"claude-sonnet-4-5": "gpt-5-5-thinking:low"}'` hoặc cặp `'from=to[:effort],...'`; sai cú pháp fail-loud ValueError | Chỉ bật sau khi có entry DECISIONS.md; dùng khi CLI gửi model id không có trong registry |
| `WEBGPT_MODEL_FALLBACK` | `warn` | Chính sách khi backend hạ slug khác request: `warn` = log WARNING + telemetry; `retry-once` = gửi lại turn một lần bằng model default, stream prefix marker để CLI phân biệt; giá trị lạ fail-loud | Giữ `warn`; cân nhắc `retry-once` khi downgrade xác nhận xảy ra thường xuyên trên unit |
| `WEBGPT_BREAKER_SCOPE` | `global` | Phạm vi breaker rate-limit: `auto` wire breaker RIÊNG mỗi account khi pool ≥ 2 account (một account hết quota không đỗ cả pool); giá trị lạ → warning + rơi về global | Đặt `auto` khi chạy pool đa account; giữ `global` cho pool 1 account |
| `WEBGPT_POOL_SELECTION` | `round-robin` | Cách chọn account chưa pin trong multi-account pool: `least-pressure` xếp theo usage đọc mới nhất nhưng CHỈ khi mọi candidate đều có reading (thiếu dữ liệu → tự rơi về rotation); giá trị lạ → round-robin | Chỉ có tác dụng khi poller usage per-account đang chạy đủ credential; giữ mặc định nếu thiếu credential |
| `WEBGPT_POOL_CROSS_BRAKE` | chưa đặt (OFF) | Cross brake: K account ĐỘC LẬP dính 429 THẬT trong cửa sổ trượt → advise mọi breaker pool mở 1 cửa sổ ngắn. Giá trị: truthy (`1/true/yes/on`) = K=2 window=600s; số nguyên = chỉ K; `K@giây` = cả hai (vd `3@300`); giá trị rác = OFF (typo không thể vô tình bật phanh) | Bật khi nghi RL mức IP/edge thay vì quota từng account |
| `WEBGPT_POOL_AUTH_DIR` | `~/.config/webgpt/codex` | Thư mục chứa bundle codex OAuth mỗi account `<name>.auth.json` — nguồn credential cho poller usage per-account; thiếu file = poller account đó idle im lặng | Chỉ đổi khi muốn tách nơi lưu credential pool khỏi layout XDG chuẩn |
| `WEBGPT_CODEX_AUTH_JSON_<NAME>` | chưa đặt | Override path auth.json riêng lẻ theo account: tên viết HOA, ký tự non-alnum gập thành `_` (vd `WEBGPT_CODEX_AUTH_JSON_WORK2`); thắng path suy ra từ pool dir | Chỉ khi cần đặt file credential của một account ngoài layout chuẩn |
| `WEBGPT_USAGE_POLL_SECONDS` | `0` (tắt) | Chu kỳ (giây) poll `wham/usage` cho breaker advisory: reading áp lực ≥ 85% → `advise_pressure` mở cửa sổ breaker (one-way); unset/≤ 0 = không dựng poller nào; bearer lazy từ codex_auth, 401/403 → mute vĩnh viễn; pollers per-account stagger lệch pha tránh burst endpoint | Bật (vd `300`) khi muốn breaker chủ động trước khi chạm quota; cần credential codex OAuth cho từng account |

Placeholder đầy đủ kèm chú thích: xem `.env.example` (mục "Gateway tuning flags").

## 7. Troubleshooting

### 7.1 Gateway rate-limit lặp lại

Triệu chứng: hàng loạt turn fail **nhanh bất thường** (vài trăm ms thay vì
resend full prompt); trong `journalctl` của gateway xuất hiện
`unhandled_gateway_error` kèm traceback
`BackendCoolingDown: backend cooling down after rate limit (retry allowed in Ns)`
(body HTTP lúc đó chỉ là 500 `internal_error` — chi tiết nằm ở log), và ngay
trước đợt đó là chuỗi 429 `rate_limit` từ backend.

Cơ chế đọc (breaker `gpt/transport/breaker.py`, trạng thái toàn tiến trình):

1. Hit rate-limit **đầu tiên** (turn kết thúc RATE_LIMITED hoặc bootstrap dính login wall) → breaker chuyển **OPEN**: mọi `acquire()` trong cửa sổ cooldown fail ngay với `BackendCoolingDown`, không tạo worker mới.
2. Hết cửa sổ → **half-open**: đúng MỘT request probe được qua.
3. Probe khoẻ → breaker **CLOSED**, backoff reset về mức gốc.
4. Probe lại dính RL → cửa sổ tiếp theo **×2** (90→180→360→…, trần 600s). Trip mới trong lúc đang mở không cộng dồn.

Cách tra log:

```bash
journalctl --user -u webgpt-gateway.service -n 300 | grep -i "cooling down"
# mỗi dòng báo còn bao nhiêu giây nữa được thử lại; đếm mật độ để biết RL
# kéo dài bao lâu, so trips qua message "retry allowed in" tăng gấp đôi.
```

Xử lý: nếu RL do payload to → bật `WEBGPT_PROMPT_BUDGET_CHARS=10000`
(root cause); muốn breaker nới lỏng → tăng
`WEBGPT_RATELIMIT_*_SECONDS`; tắt hẳn breaker (`COOLDOWN=0`) chỉ khi cần
so sánh A/B hành vi cũ.

### 7.2 CLI thấy usage = 0

Đã fix (PARITY-P0-1): route `/v1/messages` giờ ước lượng token **chars÷4**
cục bộ vì backend web không trả usage thật — `message_start` mang input
estimate, `message_delta` cuối mang output tích luỹ, non-stream trả đủ object
usage. Nhờ đó auto-compact của Claude Code hoạt động lại. Test chốt:
`test_anthropic_non_stream_usage_is_estimated_not_zero` và
`test_anthropic_stream_message_delta_accumulates_output_usage` trong
`tests/test_api_server.py`.

Nếu tái diễn, kiểm tra theo thứ tự:

1. Bắn thử non-stream: `curl -sf http://127.0.0.1:18000/v1/messages ... | jq .usage` — `input_tokens` phải > 0 và ~len(prompt)/4.
2. Stream: event `message_delta` cuối cùng phải có `usage.output_tokens > 0`.
3. Diff gần nhất có xoá wiring không: tìm `StreamUsageEstimator` /
   `_request_prompt_text` trong cả `gpt/gateway/server.py` lẫn `gpt/api/server.py`
   (3 điểm stream + điểm non-stream mỗi file).
4. Lưu ý phạm vi: route OpenAI `/v1/chat/completions` chưa wire prompt_text
   (chunk usage cuối vẫn 0) — đó không phải regression của fix Anthropic này.

## 8. Vận hành batch: preflight quota & cooldown RateLimited

### 8.1 Preflight quota trước mọi batch — BẮT BUỘC

Chính sách (DECISIONS 2026-08-26): duy trì ≤8–10 msg/ngày/account, burst ≤25
trong cửa sổ 3h; trước khi bắn batch CTF/SOAK phải đọc hạn mức qua:

```bash
.venv/bin/python scripts/preflight_quota.py            # bearer tự lấy từ disk cache
.venv/bin/python scripts/preflight_quota.py --token <access_token>
```

Bearer nguồn: `--token` > disk-cache TokenBundle
`<profile_dir>/webgpt-token-cache.json` (version 1, tuổi ≤ `--max-token-age`,
mặc định 1800s). Endpoint mặc định `/backend-api/wham/usage`, override qua
`--url`/`WEBGPT_USAGE_URL`. Script luôn in đúng 1 dòng JSON summary ra stdout.

Ý nghĩa exit code:

| Exit | Nghĩa | Hành động |
|---|---|---|
| **0** | OK — primary < 70% **và** secondary < 50% | Mở batch |
| **2** | DEFER — primary ≥ 70% **hoặc** secondary ≥ 50% | DỜI batch, không bắn |
| **3** | UNKNOWN — không có bearer / lỗi transport / 401·403·404 / payload hỏng | Coordinator tự quyết; script không tự đoán path khác |

Lưu ý chưa live-verify: endpoint/shape lấy từ source codex chính thức nhưng
chưa bắn thật lần nào. Bearer web-session có thể bị backend codex từ chối
(401 → exit 3) — khi đó cần OAuth thật (`WEBGPT_CODEX_AUTH_JSON`, mint bằng
`scripts/auth/codex_oauth_login.py`) hoặc fallback DOM Settings. Gặp 404/401 lạ
khi chạy thật → báo coordinator đổi path có chủ đích (ứng viên:
`/backend-api/codex/usage`), KHÔNG sửa mù trong script.

### 8.2 Quy tắc cooldown khi gặp RateLimited

Breaker rate-limit toàn tiến trình (`gpt/transport/breaker.py`): trip đầu mở
cửa sổ cooldown 90s, half-open probe dính RL tiếp thì ×2 (180→360→…), trần
600s (`WEBGPT_RATELIMIT_*_SECONDS`). Quy tắc cho automation loop — **cooldown
1 chu kỳ**:

- Gặp RateLimited → NGỪNG bắn, chờ TRỌN cửa sổ cooldown breaker đang mở.
- Không retry trong cửa sổ (mọi request chỉ fast-fail `BackendCoolingDown`
  500, đốt deadline chứ không đi tới upstream).
- Hết cửa sổ → probe lại ĐÚNG **1 lần**; probe dính RL tiếp = penalty ×2 và
  lặp lại quy tắc từ đầu. Không bắn batch khi breaker còn OPEN.
- Kinh nghiệm MED-BATCH4 2026-08-26: hai chu kỳ cooldown chờ đúng quy tắc
  (07:56+492s và 11:32→11:41) vẫn BLOCKED — hạn mức token-weighted chưa nhả;
  khi đó dừng hẳn, quay lại sau vài giờ hoặc sau `reset_at`.

### 8.3 Probe T1 nhỏ PASS ≠ upstream khoẻ với turn lớn

Quota ChatGPT tính **theo trọng số token**, không theo số tin nhắn
(quota-pattern-research §B4). Một probe T1 nhỏ pass (vài trăm token) chỉ chứng
minh đường HTTP sống — KHÔNG chứng minh account còn đủ hạn mức cho turn lớn.
Bằng chứng: VERIFY-R10 probe T1 đơn PASS 3.06s lúc 07:49, nhưng MED-BATCH4
bắn ngay sau đó 0/5 — POST HTTP-200 rồi stream RateLimited /
`commit_unknown_reconciled_absent` nuốt conversation âm thầm vì quota từ 07:55
chưa nhả. Hệ quả vận hành:

- Gate trước batch nên kèm ít nhất một turn cỡ thật (payload ~10k chars) hoặc
  coi exit 3 của preflight là DEFER, không phải "được phép thử".
- Batch chết hàng loạt kiểu commit-unknown/RateLimited sau khi probe nhỏ pass
  → chẩn đoán đầu tiên là hạn mức token-weighted, KHÔNG phải bug gateway.

## 9. Runbook flip Hybrid production (`--transport browser` → `--transport hybrid` + fconv)

Hồ sơ đủ điều flip: T1/T2/T3 PASS trên đường fconn thuần qua hybrid
(`docs/reports/fconv-t23-wire-2026-08-26.md` — T2/T3; `docs/reports/fconv-e2e-wire-rerun-2026-08-26.md`
— T1). Quy tắc rollback theo DECISIONS 2026-08-25: thang verify **đỏ sau 1 lần
retry** → khôi phục unit về browser ngay.

### 9.1 Điều kiện tiên quyết

```bash
# 1) Full pytest xanh tại thời điểm flip
cd /home/light/GitHub/gpt && .venv/bin/python -m pytest -q

# 2) Không batch CTF/SOAK nào đang chạy (check tmux session agent + state)
tmux ls
cat /home/light/GitHub/gpt/docs/automation/STATE.md

# 3) Backup unit hiện tại
cp ~/.config/systemd/user/webgpt-gateway.service \
   ~/Downloads/webgpt-gateway.service.pre-hybrid-$(date +%Y%m%d-%H%M%S)
```

### 9.2 Flip từng bước

Sửa unit `~/.config/systemd/user/webgpt-gateway.service`, đúng 2 chỗ:

1. Trong `ExecStart`: đổi `--transport browser` → `--transport hybrid`
   (giữ nguyên mọi flag khác, kể cả `WEBGPT_TOOL_PROTOCOL=soft`).
2. Thêm dòng `Environment=WEBGPT_FCONV_PREPARE=1` vào khối `[Service]`.

Sau đó:

```bash
systemctl --user stop webgpt-gateway.service
cp <unit-backup-vừa-tạo> ~/Downloads/webgpt-gateway.service.pre-hybrid-latest   # (nếu chưa có bản latest)
$EDITOR ~/.config/systemd/user/webgpt-gateway.service    # sửa ExecStart + thêm Environment
systemctl --user daemon-reload
systemctl --user start webgpt-gateway.service
```

Ghi chú: watchdog chỉ restart gateway khi health fail 3 lần liên tiếp (~15 phút),
flip trong vài phút không bị giành quyền.

### 9.3 Xác nhận sau flip

```bash
# 1) Journal không lỗi bootstrap (không login wall / AuthRequired / traceback)
journalctl --user -u webgpt-gateway.service -n 100

# 2) Thang verify T1 rồi T2 (mỗi level đốt turn quota thật — chạy đúng thứ tự này)
.venv/bin/python scripts/cert/verify_hybrid_flip.py --base-url http://127.0.0.1:18000 --level t1 --timeout 120
.venv/bin/python scripts/cert/verify_hybrid_flip.py --base-url http://127.0.0.1:18000 --level t2 --timeout 120

# 3) Evidence đường fconv trong trace (prepare chain chạy thật, không fallback)
grep -E 'f/conversation|prepare' ~/.local/share/webgpt/logs/trace.jsonl | tail -20
```

PASS = cả hai level in `[Tx] PASS` + trace có dấu `f/conversation`/`prepare`.
Đỏ bất kỳ level nào → retry đúng 1 lần; vẫn đỏ → mục 9.4 ngay, không chẩn đoán dài.

### 9.4 ROLLBACK (đỏ 1 retry theo DECISIONS)

```bash
cp ~/Downloads/webgpt-gateway.service.pre-hybrid-latest \
   ~/.config/systemd/user/webgpt-gateway.service
systemctl --user daemon-reload
systemctl --user start webgpt-gateway.service

# T1 xác nhận đường browser sống lại
.venv/bin/python scripts/cert/verify_hybrid_flip.py --base-url http://127.0.0.1:18000 --level t1 --timeout 120
```

Sau rollback: ghi sự kiện + lý do đỏ vào `docs/automation/FAILURES.md`.

### 9.5 Ghi chú profile

Registry account (`~/.config/webgpt/accounts.json`) resolve `personal` về profile
production `~/.local/share/webgpt/profiles/personal` — đã login tay 2026-08-26.
Mode `--account NAME` LUÔN trỏ profile gốc; E2E/test với profile hot-copy phải dùng
`--profile-dir` + `--allow-authenticated` và xoá 3 symlink
`Singleton{Lock,Cookie,Socket}` trong bản copy trước khi launch (chi tiết:
`docs/reports/fconv-e2e-wire-rerun-2026-08-26.md`).
