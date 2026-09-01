# USAGE-INTROSPECTION — usage poller → breaker one-way advice (2026-08-26)

ROADMAP row `USAGE-INTROSPECTION` (TODO M): biến breaker rate-limit từ
PHẢN ỨNG (trip sau khi dính 429) thành DỰ BÁO — poll usage endpoint codex
(`rate_limit.primary_window`) để chủ động mở cooldown ngắn bảo vệ pool
multi-account TRƯỚC khi bị chặn.

## Files

| File | Thay đổi |
|---|---|
| `gpt/transport/usage_poller.py` | MỚI — poller định kỳ, default OFF |
| `gpt/transport/breaker.py` | Thêm `advise_pressure()` + `USAGE_PRESSURE_THRESHOLD = 85.0` + counter `_advisory_opens`/property `advisory_opens`; KHÔNG đổi logic closed→open→half-open cũ |
| `tests/test_usage_poller.py` | MỚI — 23 test fake-HTTP |

Không đụng: codex_auth.py, curl_transport, token_manager, runtime,
*server*, protocol_adapters, toolcall. Không bật env ở đâu (.env/.env.example/
CI đều sạch — đã grep xác nhận). Chưa commit, chưa restart gateway, không
live call.

## Thiết kế

**One-way coupling**: poller CHỈ gọi `breaker.advise_pressure(used_percent)`
với MỌI reading hợp lệ (kể cả dưới ngưỡng) — threshold là policy của breaker,
poller không lọc. Poller không thể force-open/extend/reset breaker.

**Breaker policy (`advise_pressure`)** — trả True chỉ khi thực sự mở window:
- Chỉ hành động khi breaker FULLY CLOSED (`_deadline == 0`) và
  `used_percent >= 85`.
- Mở cooldown NGẮN đúng kích thước trip đầu tiên (`cooldown_seconds`),
  như một protective window.
- Không bao giờ compound backoff penalty (chỉ probe-fail thật mới được),
  không extend window đang mở, không đụng half-open probe in-flight
  (record_success/finish_probe giữ nguyên ngữ nghĩa).
- Advisory open không tăng `trips`.

**Poller (`UsagePoller`)**:
- Env `WEBGPT_USAGE_POLL_SECONDS` (default 0 = TẮT hoàn toàn: `start()`
  no-op, không task, không network, không đọc credential).
- URL mặc định `https://chatgpt.com/backend-api/wham/usage`, override bằng
  `WEBGPT_USAGE_URL`. GET blocking chạy trong `asyncio.to_thread`
  (curl_cffi, timeout 15s), injectable cho test.
- Bearer: lazy-import `CodexAuthManager` từ codex_auth (module đó vẫn
  nguyên trạng dormant khi `WEBGPT_CODEX_AUTH_JSON` unset); lỗi auth → idle
  chu kỳ đó, im lặng.
- Parse defensive `extract_used_percent()`: thiếu/mis-shape
  `rate_limit.primary_window`, `used_percent` không phải số (bool/str/None)
  → skip chu kỳ; clamp [0,100]. `reset_at`/`window_minutes` đọc nhưng
  IGNORE (chưa dùng — xem phần cần quyết).
- 401/403 → mute vĩnh viễn trong lifetime process (mỗi cycle sau là no-op
  tức thì, 0 request, không spam log). 5xx/transport error → chỉ skip,
  tick sau thử lại.
- Loop sleep-first (không burst lúc boot); `stop()` cancel an toàn;
  `state()` cho observability.

## Test evidence

```
$ .venv/bin/python -m pytest tests/test_usage_poller.py tests/test_backoff_breaker.py -q
38 passed in 0.25s
```

(23 test mới + 15 regression breaker cũ — không test nào cũ bị sửa.)
Phủ: success→advise mở window thật; below-threshold không advise; 9 shape
payload hỏng → skip; 401/403 → mute + 0 request thêm; 503/transport error →
retry chu kỳ sau; token provider fail → không chạm network; default OFF +
env rác → fallback OFF; loop start/poll/stop idempotent; codex_auth unset →
dormant không file/network. Ruff sạch trên cả 3 file. Lưu ý: mypy không có
trong `.venv` (module not found) — chưa chạy được typecheck.

## Cần coordinator quyết thêm

1. **URL usage endpoint**: chưa xác minh live shape `/backend-api/wham/usage`
   (không live call theo ràng buộc). Cần 1 lần capture thật để chốt path +
   field (hiện chỉ tin shape `rate_limit.primary_window.used_percent` từ
   ROADMAP row M).
2. **`reset_at`**: đang bỏ qua. Có nên tính cooldown dài hơn khi reset gần
   (ví dụ window sắp reset < cooldown → không cần mở)?
3. **Ngưỡng 85% + thời lượng cooldown advisory** dùng `cooldown_seconds`
   chung với trip thật — nếu muốn ngắn hơn (ví dụ 30–60s) cần thêm env riêng
   kiểu `WEBGPT_USAGE_PRESSURE_COOLDOWN_SECONDS`.
4. **Điểm wire cuối**: ai gọi `start()/stop()` (factory hay api-server
   lifecycle)? Hiện chủ ý chưa wire vào runtime nào — feature dormant hoàn
   toàn đến khi có quyết định.
