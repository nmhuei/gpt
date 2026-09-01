# POOL-PER-ACCT-BREAKER (row S) — 2026-08-26

Triển khai breaker per-account cho multi-account pool. Trước đó
`RateLimitBreaker` là singleton toàn tiến trình: 1 account cạn quota mở cửa sổ
cooldown 90–600s và toàn pool nghỉ theo.

## Flag

```
WEBGPT_BREAKER_SCOPE = global (mặc định) | auto
```

- `global` / unset: hành vi cũ byte-for-byte — singleton
  `global_rate_limit_breaker()` điều phối mọi acquire.
- `auto`: khi wiring có **≥2 account profile** → mỗi account một instance
  `RateLimitBreaker.from_env()` riêng (dict name→breaker); single account vẫn
  ở trên singleton (breaker riêng không mang lại gì).
- Giá trị lạ → warning `breaker_scope_invalid_value` + fallback `global`
  (typo không được phép tắt emergency brake).

## Wiring (`gpt/gateway/server.py`)

- `WebChatAPIServer.__init__`: resolve scope qua `_breaker_scope_per_account()`;
  khi auto + pool ≥2 → tạo `per_account_breakers`, inject
  `factory_kwargs["rate_limit_breaker"]` vào từng leaf factory, truyền
  `breakers=` vào `MultiAccountWorkerFactory`, expose
  `server.pool_rate_limit_breakers`.
- `ChatGPTWorkerFactory` đã sẵn param từ trước; `HybridWorkerFactory`
  (`gpt/transport/hybrid.py`) nhận thêm kwarg optional backward-compat. Gate
  acquire của hybrid chỉ kích hoạt khi breaker được **inject tường minh**
  (scope=auto): open → `BackendCoolingDown` fail-fast trước khi đốt queue
  capacity; hết window → đúng 1 half-open probe, handout thành công =
  `record_success`; lỗi giữa chừng → `finish_probe` (không kẹt slot). Không
  inject ⇒ acquisition ungated như cũ.
- Advisory headers `_advisory_ratelimit_headers(breakers=...)`: middleware
  truyền `server.pool_rate_limit_breakers`. Tổng hợp per-account:
  remaining = min budget các breaker còn closed (đều closed → 100); tất cả
  open → 0 kèm reset = ceil window mở dài nhất. Không có pool breakers →
  snapshot global như cũ.

## Selection & retry (`gpt/transport/multi_account.py`)

- `MultiAccountWorkerFactory(breakers=...)`; `_pick_name(requested, exclude)`:
  - chỉ skip breaker đang **open**; `half_open` vẫn chọn được để probe hồi phục
    (single-probe slot tự chặn race);
  - sticky default đang open → rotate sang sibling khỏe;
  - không còn ứng viên → `None` ⇒ lease fail-fast.
- `lease()` khi CÓ breakers: tối đa N lần chọn (tức retry-next ≤N−1);
  `BackendCoolingDown` pha **acquire** (trước yield) → thử account kế;
  exception pha **body** (sau yield) → không retry, surface nguyên văn.
  Explicit pin (`account_name=`) không bao giờ bị reroute âm thầm.
- KHÔNG có breakers: code path cũ giữ nguyên verbatim (single-shot, không
  failover) — regression safety.

Không đổi `gpt/transport/breaker.py` (helper hiện có đủ: `snapshot().state`,
`before_acquire/trip/record_success/finish_probe`).

## Files

- `gpt/gateway/server.py` — flag resolver + wiring + header aggregation.
- `gpt/transport/multi_account.py` — breakers mapping, skip-open, retry-next.
- `gpt/transport/hybrid.py` — optional kwarg + explicit-breaker gate.
- `tests/test_pool_breaker.py` — mới (22 test).
- Không đụng runtime/toolcall/curl_transport/protocol_adapters/accounts/conftest.

## Tests

`tests/test_pool_breaker.py` — 22 passed. Full suite **1243 passed** (0 fail),
20s. Ruff sạch trên các file touched (1 UP037 còn lại là pre-existing tại
multi_account.py:50 từ baseline). Mypy sạch cho 3 file nguồn touched.

## Hạn chế / follow-up

- Nguồn trip per-account hiện tại: ChatGPTWorkerFactory (browser path) trip
  theo turn outcome; hybrid trip sẽ sống khi row A4 wire UsagePoller
  per-account (cần OAuth bundle từng account). Selection đã phản ứng với mọi
  thứ ghi state vào breaker per-account.
- Restart gateway để nạp (không restart trong tick này theo đề bài).
