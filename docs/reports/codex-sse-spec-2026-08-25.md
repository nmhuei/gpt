# CODEX-SSE spec triển khai — 2026-08-25

Nhánh transport mới: `POST https://chatgpt.com/backend-api/codex/responses` cho account
authenticated (f/conversation bị 403 request-shape). Nguồn đã xác minh (≥2 độc lập/mục):

- **S1** github.com/Kitjesen/chatgpt-to-api (02/2026, hoạt động với Plus)
- **S2** deepwiki.com/ink1ing/anti-api (Codex provider, CLIProxyAPI-format)
- **S3** github.com/openai/codex `codex-rs/core/src/client*.rs` (client chính thức)
- **S4** openai.com/index/unrolling-the-codex-agent-loop (01/2026, blog chính thức)
- **S5** github.com/Securiteru/codex-openai-proxy

## 1. Endpoint + method + headers bắt buộc

```
POST https://chatgpt.com/backend-api/codex/responses   [S1,S2]
Content-Type: application/json                         [S1,S5]
Accept: text/event-stream                              [S1,S2]
Authorization: Bearer <bundle.access_token>            [S1,S5] — cùng AT của TokenManager hiện có (refresh qua session-token, TTL ~1h)
OpenAI-Beta: responses=experimental                    [S1,S2]
originator: codex_cli_rs                               [S1,S3]
Chatgpt-Account-Id: <account_id>                       [S2 ghi OPTIONAL; S5 lấy từ auth.json] — omit nếu bundle chưa có
Cookie: full jar (cf_clearance + oai-sc …)             — giữ nguyên như _build_headers vì vẫn nằm sau Cloudflare chatgpt.com
User-Agent: CLOAKBROWSER_USER_AGENT (Chrome/146)       — giữ nguyên, phải khớp impersonate chrome146
session_id header: S3 dùng build_session_headers — OPTIONAL, bỏ ở v1
```

KHÔNG cần: sentinel requirements/proof/turnstile, oai-device-id, x-conduit-token,
oai-session-id/correlation headers của trang web — đây là điểm thắng so với f/conversation [S1].

## 2. Body schema (Responses API)

```json
{
  "model": "gpt-5.2",            // slug có DẤU CHẤM; "gpt-5-2" bị từ chối [S1]. Map từ request.model.id, fallback "gpt-5"
  "instructions": "<system>",    // system/developer message tách ra đây, KHÔNG để trong input [S2,S5]
  "input": [
    {"type":"message","role":"user",
     "content":[{"type":"input_text","text":"<WEBGPT_MESSAGE…>"}]}   // [S2,S5]
  ],
  "tools": [],                   // flat Responses-API format khi có tool declarations
  "tool_choice": "auto",         // [S3,S5]
  "store": false,                // BẮT BUỘC với OAuth/AT; store:true lỗi [S3, S-search openclaw#67740]
  "stream": true                 // BẮT BUỘC: stream:false → HTTP 400 [S1]
}
```
Multi-turn: history map thành input items (`output_text` cho assistant trước đó;
tool-call → `function_call` + `function_call_output`) [S2].

## 3. SSE events trả về

Chuỗi chuẩn Responses API streaming [S4,S3,S1]:

| Event | Dùng làm gì |
|---|---|
| `response.created` | bắt đầu stream |
| `response.output_item.added` | metadata item |
| `response.output_text.delta` | **text tăng dần — field `delta`** ← map thẳng vào on_delta |
| `response.function_call_arguments.delta/.done` | tool call streaming |
| `response.output_item.done` | item hoàn tất |
| `response.completed` | turn done; `response` chứa usage + output[] đầy đủ |

Lưu ý test: `response.completed.response.output` CÓ THỂ rỗng dù text đã stream qua
deltas (hermes-agent#5678) — assemble text CHỈ từ `response.output_text.delta`,
không fallback snapshot từ completed.

## 4. Map kiến trúc repo

Cắm vào `gpt/transport/curl_transport.py::CurlCffiTransport` (hoặc subclass
`CodexSseTransport` cùng file — không tạo file mới để tái dùng session/impersonate).

- **Tái nguyên văn**: `__init__` (AsyncSession impersonate chrome146),
  `_build_headers()` mở rộng nhánh codex (bỏ 3 sentinel header + oai-device-id,
  thêm originator/OpenAI-Beta), `_post_conversation`, `_http_challenge_kind`,
  `_remint_credentials`, `_raise_for_status`, `close()`.
- **Thay mới**: `_build_conversation_payload` → builder Responses API (map SendRequest:
  text → input item user, system prefix → instructions, conversation_id KHÔNG gửi —
  endpoint stateless, mỗi turn gửi lại full history như hiện tại).
- **Parser**: `_stream_sse` giữ khung SSEDecoder/on_delta/emitted_upto, thay
  `_consume_record` bằng consumer codex: `type=="response.output_text.delta"` → append
  `payload["delta"]`; `type=="response.completed"` → complete=True; `[DONE]` không xuất
  hiện. TurnResult.turn_id = `response.id`.
- **Kill-switch**: env `WEBGPT_CODEX_SSE`, **default `"0"` (OFF)** — chỉ bật sau khi
  live-verify 1 POST thành công với Plus thật (theo hybrid-auth-research §4 rủi ro).
  Khi ON: send() chọn URL/payload/parser codex; OFF: hành vi hiện tại nguyên vẹn.
  Pattern theo `_flag_enabled()` sẵn có.
- AuthRequired/RateLimited/ProtocolChanged giữ nguyên semantics (401/403 → invalidate +
  raise; 400 shape-lỗi → ProtocolChanged kèm body snippet).

## 5. Test plan (`tests/test_codex_transport.py`)

Fake `AsyncSession` (pattern tests/test_session.py) capture (url, headers, json):
1. assert url == codex/responses; headers chứa đúng originator/OpenAI-Beta/Bearer AT/
   Content-Type/Accept và KHÔNG chứa openai-sentinel-*;
2. assert body: `stream is True`, `store is False`, input[0].content[0].type ==
   "input_text", system text nằm trong instructions chứ không trong input;
3. mock SSE bytes fixture (các event §3, gồm function_call_arguments.delta) → assert
   TurnResult.text ghép đúng thứ tự delta, status completed, on_delta nhận từng chunk
   đúng field `delta`;
4. edge: completed mà output rỗng → vẫn lấy text từ deltas (regression hermes);
5. blocked-dấu hiệu: HTTP 403/401 → AuthRequired + sentinel-cache invalidate được gọi;
   HTTP 400 → ProtocolChanged với snippet body (phân biệt với 403 CF);
6. kill-switch: WEBGPT_CODEX_SSE unset/"0" → payload f/conversation cũ như hiện tại.
