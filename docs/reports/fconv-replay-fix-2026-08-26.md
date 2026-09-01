# fconv_replay — device-id fallback fix (2026-08-26)

## Vấn đề
Sáng nay ladder tới bước 3 thành công (prepare 200, conduit_token 352 chars)
nhưng bước 4 crash tại `CurlCffiTransport._build_headers`:
`AuthRequired: missing required credentials: oai-device-id`. Nguyên nhân:
profile chưa login lúc đó nên `TokenManager.refresh_if_needed()` không tìm
thấy `oai-device-id` (localStorage `oai_device_id` / cookie `oai-device-id`,
xem `gpt/transport/token_manager.py:537-547`) → `bundle.oai_device_id = None`.

## Patch — `scripts/fconv_replay.py`
Sau `refresh_if_needed()` (và sau guard local-mock), nếu
`bundle.oai_device_id` rỗng:

- Sinh UUID4 chuẩn format ChatGPT (`str(uuid.uuid4())`, lowercase hyphenated).
- `TokenBundle` là `@dataclass(frozen=True)` ⇒ rebuild bằng
  `dataclasses.replace(bundle, oai_device_id=generated)`.
- In `[WARN] device-id không có trong profile — dùng UUID mới (<uuid>)
  (server có thể gán lại)`.

Không đổi gì transport/production; chỉ vá script replay.

## Verify
- `.venv/bin/python scripts/fconv_replay.py --help` → exit 0.
- Dry-run plan in đúng 4 bước ladder + verdict criteria → exit 0.
- `ruff check scripts/fconv_replay.py` → All checks passed.
- Không test file nào reference `fconv_replay` (grep tests/ rỗng).

## Lưu ý coordinator
Đã login lại profile personal (~/.local/share/webgpt/profiles/personal) — có
thể device-id đã có sẵn trong localStorage sau khi page JS chạy; fallback chỉ
kích hoạt khi vẫn rỗng. Chưa bắn `--live` (cần export `WEBGPT_FCONV_PREPARE=1`
bên coordinator). Chưa commit.
