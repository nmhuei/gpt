# POOL-POLLER-PERACCT — bước M của pool research (2026-08-26)

Triển khai mục **C2 (Scope M)** trong
`docs/reports/multi-account-pool-research-2026-08-26.md`: UsagePoller
per-account + least-pressure selection + cross-brake, xây trên nền bước S
(POOL-PER-ACCT-BREAKER) đã merge (`WEBGPT_BREAKER_SCOPE` +
`MultiAccountWorkerFactory(breakers=...)`).

## Thay đổi theo file

### `gpt/transport/usage_poller.py`

- **`PoolPressureBoard`**: board nhỏ giữ `used_percent` mới nhất per account
  (`record` / `pressure(name)` / `has_all(names)` / `snapshot()`), lock-guarded.
  Thiếu dữ liệu = "unknown", không bao giờ bị hiểu là 0%.
- **Credential per-account** — `account_auth_json_path(name)`:
  precedence `WEBGPT_CODEX_AUTH_JSON_<NAME>` (upper-case, ký tự không
  alphanumeric gập thành `_`) → `<WEBGPT_POOL_AUTH_DIR>/<name>.auth.json` →
  mặc định `~/.config/webgpt/codex/<name>.auth.json`.
  File không tồn tại ⇒ provider trả None lặng lẽ (poller idle, KHÔNG đụng
  network/không import codex_auth, KHÔNG mute-latch — bundle thêm sau vẫn
  sống mà không cần restart). File tồn tại ⇒ đi qua `CodexAuthManager`
  đúng dormancy contract cũ.
- **`create_account_pollers(breakers) -> (dict[name→UsagePoller], board)`**:
  map name→breaker khác rỗng chính là output bước S khi
  `WEBGPT_BREAKER_SCOPE=auto` với ≥2 account nên shape của nó mã hóa cả hai
  điều kiện kích hoạt. `<2 breaker` hoặc `WEBGPT_USAGE_POLL_SECONDS` unset/≤0
  ⇒ trả rỗng, không construct gì (default OFF giữ nguyên).
  Mỗi poller advise CHỈ breaker account của nó (one-way `advise_pressure`);
  start staggered lệch nhau `poll_seconds/N` theo thứ tự tên sort để N poller
  không dội endpoint đồng loạt.
- **`UsagePoller`**: thêm 2 kwarg optional — `start_delay` (sleep một lần
  trước vòng lặp đầu) và `reading_listener` (callback sau mỗi scrape thành
  công; exception của listener không bao giờ làm rớt cycle). Default 0/None ⇒
  hành vi single-poller cũ byte-identical.

### `gpt/transport/multi_account.py`

- Kwarg keyword-only mới, tất cả default-off/no-op: `pressures`,
  `cross_brake`, `selection`.
- **Least-pressure selection** (opt-in): env `WEBGPT_POOL_SELECTION`
  = `round-robin` (mặc định, byte-identical) | `least-pressure`. Chỉ rank khi
  board có dữ liệu ĐỦ mọi candidate còn khả dụng (sau filter health +
  breaker-open); xoay cursor trước khi sort để tie công bằng; thiếu data bất kỳ
  ⇒ fallback rotation thuần. Sticky-default/pin semantics giữ nguyên — ranking
  chỉ sắp lại candidate đã lọc.
- **API `pressure(name)`** trên factory: passthrough đọc board, None khi chưa
  wire/chưa có reading.
- **Cross-brake hook**: `_mark_failure` giờ record REAL RateLimited (không tính
  AuthRequired/challenge) vào `cross_brake` trước early-return health — hoạt
  động cả khi không có health tracker.

### `gpt/transport/breaker.py`

- **`CrossBrake`** + `parse_cross_brake_spec` + `cross_brake_from_env`:
  đếm hit theo DISTINCT account trong sliding window; đạt K distinct ⇒
  `advise_pressure(100.0)` lên MỌI breaker của pool (window đang mở tự no-op
  nên không stack/kéo dài — dedup tự nhiên, đúng one-way contract).

## Env flags mới

| Flag | Mặc định | Ý nghĩa |
|---|---|---|
| `WEBGPT_POOL_SELECTION` | `round-robin` | `least-pressure` = rank theo used_percent thấp nhất (chỉ khi đủ data) |
| `WEBGPT_POOL_CROSS_BRAKE` | OFF | `1` = K=2/window 600s; hoặc `K@seconds` (vd `3@300`); giá trị rác ⇒ OFF (typo không thể vô tình bật brake) |
| `WEBGPT_POOL_AUTH_DIR` | `~/.config/webgpt/codex` | thư mục chứa `<name>.auth.json` |
| `WEBGPT_CODEX_AUTH_JSON_<NAME>` | — | override path per account, thắng mọi thứ |

## Wire-up gateway (follow-up)

Theo ràng buộc file của tick này, `gpt/gateway/server.py` chưa đổi. Điểm nối
khi bật: trong lifespan sau khi `pool_rate_limit_breakers` non-empty —
`pollers, board = create_account_pollers(server.pool_rate_limit_breakers)`
+ start từng poller, và
`MultiAccountWorkerFactory(..., pressures=board, cross_brake=cross_brake_from_env(pool_rate_limit_breakers))`.
Không wire ⇒ mọi đường default chạy y như pre-M.

## Tests

97 test trong `tests/test_usage_poller.py` + `tests/test_pool_breaker.py`
(trước đó ~76): multi-poller advise đúng breaker riêng; board record/has_all;
stagger offset; resolution credential (default/HOME, dir override, prefix
override, fold tên); missing-bundle idle im lặng zero-request; listener lỗi
không rớt cycle; selection tie-break/tie-rotate/partial-data fallback/thứ tự
sau breaker-filter/env-vs-kwarg; cross-brake K-N distinct + sliding window +
no-stack + parse table + env gate; factory thật raise RateLimited feed
cross-brake còn AuthRequired thì không; default paths byte-identical.
Regression: 210 test các suite lân cận pass. `ruff check` sạch trên cả 5 file.

## Không đổi

Hybrid/curl_transport/runtime/api-server/gateway-server, failover table,
health loop, hành vi `WEBGPT_USAGE_POLL_SECONDS`/`WEBGPT_BREAKER_SCOPE` cũ.
