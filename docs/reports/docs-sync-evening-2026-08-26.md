# Docs sync tối 2026-08-26 — env flag mới vào tài liệu ops

Đồng bộ 8 env flag mới (model routing phase 1/2 + multi-account pool) vào
`docs/guides/AUTOMATION_OPS.md` section 6 và `.env.example`. Không sửa code,
không commit.

## Trạng thái trước / sau

| Flag | AUTOMATION_OPS §6 trước | .env.example trước | Hành động |
|---|---|---|---|
| `WEBGPT_MODEL_ALIAS` | thiếu | thiếu | thêm cả hai |
| `WEBGPT_MODEL_FALLBACK` | thiếu | thiếu | thêm cả hai |
| `WEBGPT_POOL_SELECTION` | thiếu | thiếu | thêm cả hai |
| `WEBGPT_POOL_CROSS_BRAKE` | thiếu | thiếu | thêm cả hai |
| `WEBGPT_POOL_AUTH_DIR` | thiếu | thiếu | thêm cả hai |
| `WEBGPT_CODEX_AUTH_JSON_<NAME>` (pattern) | thiếu | thiếu | thêm cả hai |
| `WEBGPT_BREAKER_SCOPE` | thiếu | thiếu | thêm cả hai |
| `WEBGPT_USAGE_POLL_SECONDS` | thiếu | đã có (dòng ~79) | thêm bảng ops |

## Thay đổi

- `docs/guides/AUTOMATION_OPS.md` — **+8 dòng** bảng §6: 7 flag mới hoàn toàn +
  `WEBGPT_USAGE_POLL_SECONDS` (trước chỉ nằm ở `.env.example`). Mỗi entry ghi
  tên · mặc định · ý nghĩa · khuyến nghị ON/OFF, lấy trực tiếp từ nguồn code:
  - alias/fallback: `gpt/transport/curl_transport.py` (~200-330)
  - selection: `gpt/transport/multi_account.py` (~24-46)
  - cross brake: `gpt/transport/breaker.py` (~310-420, mặc định K=2 window=600s)
  - breaker scope: `gpt/gateway/server.py` (~88-112)
  - pool dir + per-account override: `gpt/transport/usage_poller.py` (~60-70, 408-443)
- `.env.example` — **+7 commented placeholder**: 2 khối mới "Model routing"
  (`WEBGPT_MODEL_ALIAS`, `WEBGPT_MODEL_FALLBACK`) và "Multi-account pool /
  breaker scope" (`BREAKER_SCOPE`, `POOL_SELECTION`, `POOL_CROSS_BRAKE`,
  `POOL_AUTH_DIR`, `CODEX_AUTH_JSON_<NAME>`). `USAGE_POLL_SECONDS` đã có sẵn.

## CLAUDE.md XDG layout

Xác nhận ĐÃ CÓ — dòng 28 ("Runtime data layout (XDG, migrated 2026-08-26):
profiles/logs → `~/.local/share/webgpt/`, registry →
`~/.config/webgpt/accounts.json`, ..."). Không chỉnh.

## Ghi chú

- Ngữ nghĩa mặc định giữ đúng code: garbled input của `POOL_CROSS_BRAKE`,
  `MODEL_ALIAS/FALLBACK` fail-loud hoặc OFF; `POOL_SELECTION` giá trị lạ →
  round-robin; `BREAKER_SCOPE` lạ → warning + global.
- `WEBGPT_MODEL_ALIAS` vẫn cần entry DECISIONS.md trước khi bật trên unit
  (comment MODEL-ROUTING-PHASE1 trong curl_transport.py) — đã ghi vào cột
  "Khi nào chỉnh".
