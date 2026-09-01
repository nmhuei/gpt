# OAI-DID FALLBACK — TokenManager device-id sourcing fix (2026-08-26)

## Scope

Fix nhỏ cho FCONV-E2E-WIRE fail (xem `fconv-e2e-wire-2026-08-26.md`): bundle
`oai_device_id` rỗng trên mọi cold start hybrid vì extractor chỉ biết tên
cookie `oai-device-id` / `oai_device_id`, trong khi ChatGPT thật đặt cookie
**`oai-did`**.

## Thay đổi

### `gpt/transport/token_manager.py`

1. `_extract_all_unlocked` (:538+): thứ tự ưu tiên mới cho device id —
   cookie `oai-did` → `oai-device-id` → `oai_device_id` → localStorage
   (`oai-device-id` || `oai_device_id`). localStorage chỉ được evaluate khi
   không có cookie nào khớp (tiết kiệm một round-trip page.evaluate; đúng
   tinh thần "protocol over DOM").
2. Fallback mint + persist (mirror `scripts/fconv_replay.py:220-230`): nếu
   vẫn rỗng — trước tiên tái dùng `oai_device_id` của bundle trước đó
   (`self._bundle`, gồm cả giá trị load từ disk cache) để không xoay identity
   giữa các turn; nếu chưa có gì thì mint `uuid4`. Giá trị minted đi vào
   `TokenBundle.oai_device_id` và được persist tự động qua `_write_disk_cache`
   vào file token-cache hiện có (`webgpt-token-cache.json`, field
   `oai_device_id` đã tồn tại từ trước — không đổi schema, mode 0600 giữ
   nguyên). Lần extract sau (kể cả instance mới sau restart, trong TTL
   refresh_interval) đọc lại được; quá TTL, re-extract browser trống → reuse
   previous-bundle value.

### Tests

- `tests/test_token_manager.py`: fake context thêm cookie thật `oai-did`;
  assertion test 1 đổi sang ưu tiên cookie; thêm 2 test mới:
  - `test_device_id_prefers_legacy_cookie_over_local_storage` (oai-did vắng →
    cookie legacy thắng storage);
  - `test_missing_device_id_is_minted_and_persisted` (không có gì → UUID4 hợp
    lệ, ghi đúng giá trị vào cache file, instance thứ hai re-extract đầy đủ mà
    vẫn nhận cùng id).
- `tests/test_token_cache_disk.py`: assertion `stored["oai_device_id"]` đổi
  theo ưu tiên mới ("device-from-cookie"); bỏ import `os` unused (F401 có sẵn).

## Kết quả

```
tests/test_token_manager.py tests/test_token_cache_disk.py  → 18 passed
+ tests/test_count_tokens_align.py                          → 28 passed
ruff check (3 file)                                         → All checks passed!
```

mypy không cài trong venv (bỏ qua như hiện trạng).

## Ràng buộc

Không commit, không đụng file ngoài 3 file trên, không restart gateway,
không bắn live. Re-run FCONV-E2E-WIRE sau fix còn là việc tiếp theo theo
report gốc.
