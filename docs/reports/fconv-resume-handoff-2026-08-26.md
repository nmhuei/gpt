# FCONV-RESUME-HANDOFF — implementation report (2026-08-26)

ROADMAP row M, spec `docs/reports/sse-resume-research-2026-08-26.md` §Đề xuất.
Status: IMPLEMENTED, fake-tested only. KHÔNG bắn live, KHÔNG commit, KHÔNG
restart (đúng phạm vi giao).

## What shipped

1. **Parser giữ event thay vì drop** — `gpt/transport/curl_transport.py`
   `_consume_v1_record`: nhánh ignore cũ
   `{delta_encoding, resume_conversation_token, message_marker, …}` tách
   `resume_conversation_token` ra riêng. Khi turn opt-in (capture dict được
   truyền xuống `_consume_record`), token + conversation_id của event được lưu;
   khi không có capture dict (mọi caller cũ/test cũ) tuple trả về không đổi.
2. **Follow handoff** — helper mới `_follow_fconv_resume_segment` +
   vòng lặp trong `_stream_sse`:
   - POST `{conversation_url}/resume`, body `{conversation_id, offset}`,
     offsets thử 0→1→2, CHỈ advance khi 404; status khác 2xx → dừng chain,
     log warning, turn vẫn hoàn tất với text đã stream (không bao giờ raise).
   - Header `X-Conduit-Token` bị THAY bằng token captured; phần còn lại của
     envelope (Cookie/Bearer/x-oai-turn-trace-id…) giữ nguyên từ envelope turn.
   - Cap 64 follow/turn (`_FCONV_RESUME_MAX_FOLLOWS`) + loop-guard: segment
     trả lại CÙNG token → dừng (parity gptweb2api token-loop guard).
   - Chỉ nối khi `[DONE]`-mà-vẫn-còn-token (`complete` gate) và có envelope
     (envelope_headers là marker ngữ cảnh fconv — path khác token stays inert).
   - Decoder continuity: cùng instance `SSEDecoder` + cùng closure `absorb`
     qua mọi segment; `finish()` giữa các segment chống dính partial-event
     cross-body; delta phát theo đúng thứ tự arrival qua `on_delta`.
3. **Flag** — `WEBGPT_FCONV_RESUME`, default OFF (truthy set
   `1/true/yes/on`). OFF = byte-for-byte hành vi cũ: event drop im lặng, không
   POST resume nào, `TurnResult.metadata` rỗng.
4. **TurnResult.metadata** — field mới `metadata: dict[str, Any]` (default
   empty) trong `gpt/utils/types.py`; khi có follow thì chứa
   `{"fconv_resume": {hops, conversation_id, token}}`. Field cuối cùng với
   default → mọi construction positional/kwargs hiện có không đổi.
5. **Plumbing** — call-site `send()` tách nhánh codex/fconv để truyền
   `envelope_headers=headers` vào `_stream_sse`; model-fallback retry path cố
   tình không truyền (fallback phải never-fail, giữ hành vi cũ).

## Files

| File | Thay đổi |
|---|---|
| `gpt/transport/curl_transport.py` | constants `_FCONV_RESUME_{FLAG,OFFSETS,MAX_FOLLOWS}`; `_consume_record`/`_consume_v1_record` thêm kwarg `capture=None`; nhánh capture trong v1 parser; `_stream_sse` (+`envelope_headers`, follow loop, metadata); helper `_follow_fconv_resume_segment`; `_fconv_resume_enabled()`; call-site `send()`. VÙNG CẤM không đụng: `_prepare_fconv_turn`, routing alias :204-292, image upload, bearer. |
| `gpt/utils/types.py` | `TurnResult.metadata` field mới. |
| `tests/test_fconv_resume.py` | NEW — 12 test (mục dưới). |

Không đụng runtime/servers/toolcall/accounts/conftest/multi_account/
usage_poller/token_manager.

## Tests (12, all green; fake session/scripted SSE, zero network)

1. ON: event captured → 1 handoff POST đúng URL/body `{conversation_id,
   offset:0}`, `X-Conduit-Token` = token captured (thay conduit prepare),
   trace-id/envelope giữ nguyên; text nối liền, delta order preserved;
   metadata hops=1.
2. Offset advance CHỈ khi 404: offset 0 → 404, offset 1 → 200 stream.
3. 403 (non-404) → chain dừng sau 1 lần thử, turn vẫn completed với text cũ.
4. Token lặp lại → loop-guard dừng sau 1 hop.
5. Stream liền mạch ([DONE], không token) → 0 POST, metadata rỗng.
6. Cap 64: 79 continuation mỗi cái phát token mới → đúng 64 POST rồi dừng,
   hop cuối dùng token thứ 63, metadata hops=64.
7. OFF: event vẫn đến nhưng drop như cũ — 0 POST, text không mất mảnh,
   metadata rỗng.
8. Unit `_consume_record`: có capture dict → lưu token+conversation_id;
   không có (caller legacy positional) → tuple bất biến.
9. Truthy flag matrix: `0/""/false/off/unset` đều OFF.

Regression sweep (targeted): 41 (stream/resume/curl) + 116 (kèm
conversations/session) + 73 (api_server/protocol_adapters) — all passed.
ruff clean trên 3 file; mypy 0 error trong 2 file source (23 error còn lại là
pre-existing ở file khác do follow-import).

## Flag semantics (tóm tắt vận hành)

- `WEBGPT_FCONV_RESUME` unset/mọi giá trị ngoài truthy set → hành vi cũ nguyên
  vẹn byte-for-byte (drop event như trước, không request nào phát sinh).
- ON: chỉ active trên nhánh authed fconv khi server thật sự phát
  `resume_conversation_token`; mọi lỗi handoff (HTTP lạ, exception, hết offset)
  đều hạ cấp thành warning + giữ kết quả đã stream — không bao giờ làm hỏng
  turn. Live replay (≤ vài request thật) là bước kế tiếp, chưa thực hiện theo
  phạm vi.

## Not done / next

- Chưa live-verify (cấm bắn live theo task); cần replay sau khi row
  FCONV-NOTOKEN-REPLAY chốt đường sống.
- 401 trên handoff chưa invalidate credential (research gptweb2api có làm);
  chủ ý bỏ để giữ scope tối thiểu — xem xét khi live data có.
