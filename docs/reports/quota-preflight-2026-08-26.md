# QUOTA-PREFLIGHT + RESET-AWARE-COOLDOWN — implement report (2026-08-26)

Implement agent, trực tiếp (không subagent). Nguồn shape:
`docs/reports/quota-pattern-research-2026-08-26.md` (§A1 canonical từ
openai/codex `rate_limit_status_details.rs`). Không commit, không restart,
không gọi mạng thật trong dev/tests (toàn bộ HTTP fake).

## Files

| File | Thay đổi |
|---|---|
| `scripts/preflight_quota.py` | MỚI — CLI gate trước batch |
| `gpt/transport/breaker.py` | `advise_pressure(used_percent, *, seconds_until_reset=None)` + 2 hằng số mới |
| `gpt/transport/usage_poller.py` | parse full window snapshot (`RateLimitWindow`, `extract_rate_limit_window`), forward `seconds_until_reset`, injectable `wall_clock` |
| `tests/test_usage_poller.py` | mở rộng phần RESET-AWARE-COOLDOWN |
| `tests/test_preflight_quota.py` | MỚI |

Không đụng: curl_transport / codex_auth / runtime / toolcall /
gateway-server / api-server / token_manager (chỉ import read-only
`TokenBundle`; cache-file reader viết lại trong script).

## VIỆC 1 — QUOTA-PREFLIGHT CLI (`scripts/preflight_quota.py`)

Exit-code contract:

- **0 OK** — primary used_percent < 70 **và** secondary < 50 → batch mở được.
- **2 DEFER** — primary ≥ 70% **hoặc** secondary ≥ 50% → dời batch.
- **3 UNKNOWN** — không có bearer, lỗi transport, 401/403, status khác 200
  (gồm 404), payload không parse được, hoặc không có window đo được →
  coordinator tự quyết; script KHÔNG tự đoán path khác.

Luôn in đúng 1 dòng JSON summary ra stdout (`blocked`: null cho exit 3).
Bearer: `--token` > disk-cache TokenBundle `<profile_dir>/webgpt-token-cache.json`
(version 1, tuổi ≤ `--max-token-age`, mặc định 1800s). Header:
`authorization` + `accept` (+ `chatgpt-account-id`, `--user-agent` khi cấp).
argparse nằm hoàn toàn sau main-guard ⇒ `--help` zero side-effect (có test
subprocess chứng minh). URL mặc định wham/usage; override chỉ qua
`--url`/`WEBGPT_USAGE_URL`.

**CẢNH BÁO CHƯA LIVE-VERIFY**: endpoint/shape lấy từ source codex chính thức,
chưa bắn thật lần nào. Khi chạy thật mà gặp 404/401 → báo lại coordinator
để đổi path có chủ đích (ứng viên §A2: `/backend-api/codex/usage`) — không
sửa mù trong script. Lưu ý research §E: bearer web-session có thể bị backend
codex từ chối (401) → rơi vào exit 3, cần OAuth (`WEBGPT_CODEX_AUTH_JSON`)
hoặc fallback DOM Settings nếu muốn sống thật.

## VIỆC 2 — RESET-AWARE-COOLDOWN (row S)

Breaker giờ nhận thêm `seconds_until_reset` (poller tính =
`reset_at − wall_now`, None khi payload thiếu/rác):

- cooldown advisory = `min(cooldown_seconds, seconds_until_reset + 60s buffer)`
  → không mở cửa sổ lâu hơn mức cần sau khi window tự hồi phục;
- `seconds_until_reset < 90s` (kể cả âm/quá khứ) → **skip advisory open**
  (đợi reset rẻ hơn, không gate acquisitions);
- mọi chính sách cũ giữ nguyên khi không truyền reset (backward-compatible,
  kw-only); advisory vẫn không cộng backoff penalty, không đụng half-open probe.

Poller: `extract_rate_limit_window()` trả snapshot đầy đủ
(`used_percent` clamp [0,100], `reset_at`, `limit_window_seconds` optional);
`extract_used_percent()` giờ delegate qua nó (giữ hành vi cũ);
`UsageReading.seconds_until_reset` + `state()["last_seconds_until_reset"]`
cho observability; `wall_clock` injectable để test không phụ thuộc thời gian thật.

## Verification

```
.venv/bin/python -m pytest -q tests/test_usage_poller.py tests/test_preflight_quota.py
→ 75 passed

.venv/bin/python -m pytest -q tests/test_backoff_breaker.py
→ 9 passed

.venv/bin/python -m pytest -q tests/test_stream_polish.py   # consumer global breaker
→ 8 passed

ruff check (5 file trên)      → All checks passed!
mypy (3 file nguồn)           → 0 error ở file mình sửa (baseline repo
                                còn ~11–23 error có sẵn ở file khác)
scripts/preflight_quota.py --help (subprocess) → exit 0, không network
```

## Việc còn mở (cho coordinator)

1. Live-verify wham/usage bằng bearer thật 1 lần; nếu 404/401 quyết path thay thế.
2. Quyết dùng preflight ở đâu trong automation loop (trước mỗi batch CTF/SOAK).
3. Nếu web-session bearer bị 401: wire codex OAuth hoặc DOM Settings fallback (research §A4).
