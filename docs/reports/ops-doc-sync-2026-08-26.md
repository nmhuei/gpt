# Ops doc sync — 2026-08-26

Đồng bộ bảng flag `WEBGPT_*` trong `docs/guides/AUTOMATION_OPS.md` (section 6)
và placeholder `.env.example` với các env mới merge trong code.

## Phạm vi grep

`gpt/config/settings.py` (không chứa env `WEBGPT_*` — chỉ `CHATGPT_/CDP_/API_…`),
`gpt/gateway/runtime.py`, `gpt/utils/promptcompat.py`, `gpt/transport/codex_auth.py`,
cộng thêm `gpt/transport/token_manager.py` (nơi chứa `WEBGPT_SENTINEL_SDK` /
`WEBGPT_SENTINEL_CACHE`).

## Thêm mới vào bảng AUTOMATION_OPS.md (9 flag)

| Flag | Mặc định | Trạng thái |
|---|---|---|
| `WEBGPT_MAX_TOOL_CALLS_PER_TURN` | `3` | ON — trần invoke/turn trước correction MULTI_TOOL; `1` = strict cũ (`gpt/gateway/runtime.py`) |
| `WEBGPT_FALSE_COMPLETION_BREAKER` | `12` | ON — breaker false-completion per-request (`runtime.py`) |
| `WEBGPT_NOOP_REPEAT_SKIP` | `5` | ON — metronome skip no-op lặp (`runtime.py`) |
| `WEBGPT_IMAGE_PLACEHOLDER` | bật (`1`) | Kill-switch: `0` = silent-drop ảnh như cũ (`gpt/utils/promptcompat.py`) |
| `WEBGPT_SENTINEL_SDK` | bật | Kill-switch: `0` = flow prepare/finalize/legacy cũ (`gpt/transport/token_manager.py`) |
| `WEBGPT_SENTINEL_CACHE` | bật | Kill-switch: `0` = mint mỗi turn (`token_manager.py`) |
| `WEBGPT_CODEX_AUTH_JSON` | unset (OFF) | Master switch module OAuth codex; dormant, chưa wire vào TokenManager/factory (`gpt/transport/codex_auth.py`) |
| `WEBGPT_CODEX_CLIENT_ID` | client codex-rs công khai | Chỉ tác dụng khi `CODEX_AUTH_JSON` đặt (`codex_auth.py`) |
| `WEBGPT_PROMPT_DEBUG_DIR` | unset (tắt) | Dump prompt redacted opt-in (`runtime.py`) |

Đã có sẵn từ trước, không đổi: `WEBGPT_MAX_CORRECTIONS` (row đã đúng — cap
protocol-shaped cứng = 2 khớp `_PROTOCOL_SHAPED_MAX_CORRECTIONS`),
`WEBGPT_FCONV_PREPARE`, `WEBGPT_CODEX_SSE`.

Lưu ý: "correction-tighten" mới trong runtime là hằng số code
(`_PROTOCOL_SHAPED_MAX_CORRECTIONS = 2`, escalation hints) — không phải env,
không cần entry. `<WEBGPT_MESSAGE>` là protocol marker của promptcompat, không
phải env flag.

## .env.example

Thêm 10 dòng placeholder commented: nhóm Behaviour switches (+4: tool-calls,
false-completion breaker, noop-skip, prompt-debug-dir), mục Kill-switches
rollback mới (+3), mục Codex OAuth token source mới (+2).

## Sửa khác

- Cập nhật dòng mở đầu section 6: ghi nhận đợt bổ sung 2026-08-26.
