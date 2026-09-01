# POLLER-LIFESPAN-POOL — wire per-account usage pollers vào gateway lifespan (2026-08-26)

Nối tiếp POOL-POLLER-PERACCT (đã merge) + USAGE-POLLER-WIRE: khi
`WEBGPT_BREAKER_SCOPE=auto` resolve ra pool thật (≥2 account), gateway giờ tự
chạy N poller per-account thay vì singleton global-breaker. Mọi đường khác giữ
hành vi cũ nguyên vẹn.

## Thay đổi

### `gpt/gateway/server.py` — chỉ khối lifecycle đã có

- Import thêm `PoolPressureBoard`, `create_account_pollers` từ
  `gpt.transport.usage_poller`.
- `__init__`: 2 attribute mới cạnh `_usage_poller` —
  `_account_usage_pollers: dict[str, UsagePoller]` (rỗng mặc định) và
  `pool_pressure_board: PoolPressureBoard | None`.
- `start_usage_poller()`:
  - OFF (`WEBGPT_USAGE_POLL_SECONDS` unset/≤0) → không construct gì, như cũ.
  - `pool_rate_limit_breakers` non-empty (chính xác điều kiện scope=auto +
    ≥2 account mà bước S mã hóa trong shape) → `create_account_pollers(...)`,
    start từng poller (staggered sẵn theo sorted-name), lưu dict + board.
  - Kết quả rỗng từ factory (<2 breaker) HOẶC pool empty → rơi xuống đường
    singleton `UsagePoller(global_rate_limit_breaker())` byte-identical.
  - Idempotent: call lần 2 không rebuild (guard cả hai container).
- `close()`: teardown mở rộng — stop mọi poller per-account (cancel + await),
  clear dict, drop board reference; vẫn chạy trước khi sessions/factories die,
  safe khi chưa từng start. Singleton teardown giữ nguyên.

Credential path của từng poller đi qua `make_account_token_provider` sẵn có
(`WEBGPT_CODEX_AUTH_JSON_<NAME>` → `WEBGPT_POOL_AUTH_DIR` → default); bundle
thiếu = poller idle lặng lẽ, thêm bundle sau vẫn sống.

### `tests/test_usage_poller.py` — +3 test (56 tổng, trước 53)

- **auto+2acct**: lifespan với `pool_rate_limit_breakers` 2 account → đúng 2
  instance, mỗi poller advise breaker riêng (`is` check), staggered start_delay
  alpha=0/beta=15s, board được wire, idempotent re-start, sau shutdown mọi task
  cancelled + dict/board/singleton sạch.
- **<2 acct** (sub-pool): fallback singleton global-breaker, không account
  poller, không board — teardown singleton không đổi.
- **OFF + pool populated**: cả ba container trống suốt lifespan — flag off
  thắng mọi thứ, zero overhead.

## Không đổi

Multi-account/breaker/curl_transport/runtime/toolcall/accounts/conftest;
vùng non-lifecycle của gateway server; hành vi env cũ; chưa nối
`pressures=board`/`cross_brake` vào `MultiAccountWorkerFactory` (follow-up
riêng vì nằm ngoài ràng buộc file tick này).

## Verify

`pytest tests/test_usage_poller.py tests/test_pool_breaker.py
tests/test_gateway_agent_loop.py tests/test_api_server.py -q` → 163 passed;
`ruff check` sạch trên cả 2 file; mypy chỉ còn 5 error pre-existing tại các
dòng không đụng tới (682/960/964/1943).
