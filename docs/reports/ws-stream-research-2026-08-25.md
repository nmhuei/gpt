# WS-STREAM RESEARCH — ChatGPT conversation stream: WebSocket hay SSE? (2026-08-25)

Nguồn chính: web research 8 calls (≤12 budget) + code đọc từ `5yu4n/gptweb2api`
(repo Go bridge còn sống, pushed 2026-08-24, validated live 200 ngày 23–24/8)
+ static inspection Codex Desktop của họ (2026-08-01).

## TL;DR

**Stream chat authed của ChatGPT Web vẫn là SSE trên `POST /f/conversation` —
KHÔNG phải WebSocket.** WebSocket chỉ mang sự kiện lifecycle (topic
`"conversations"`: `conversation-created`, `conversation-turn-complete`),
không mang text delta. Conduit-probe hôm nay chết vì **thiếu body của
prepare** — body đó giờ đã biết chính xác, và cả đường pure-HTTP browserless
đã được project khác chạy 200 thật tuần này.

## Câu hỏi 1 — WS hay SSE? Endpoint/auth/frame?

### Bằng chứng: SSE vẫn là đường chính thống

1. **Codex Desktop embedded ChatGPT client** (gptweb2api static inspection
   package `OpenAI.Codex_26.721.11231.0`, 2026-08-01) — client ChatGPT "thật"
   nhúng trong app desktop, base `https://chatgpt.com/backend-api`:
   - `GET /models`
   - `POST /sentinel/chat-requirements/prepare` ⭐ endpoint MỚI
   - optional `POST /f/conversation/prepare` (mang conduit token cũ hoặc
     literal no-token marker)
   - `POST /f/conversation` → **SSE** (`text/event-stream`)
   - `POST /f/conversation/resume` — resume stream (endpoint mới cho repo)
   - Nói rõ: *"This is not the Codex Responses transport."*
   - Prepare fail → KHÔNG fatal: client fallback "integrity-only stream
     headers" (đi conversation không conduit).
   - Nguồn: `github.com/5yu4n/gptweb2api/blob/main/docs/research/codex-desktop-chat-transport.md`

2. **gptweb2api research doc** (`docs/research/chatgpt-conversation-prepare.md`,
   updated 2026-08-01): capture shape thật — prepare trả 200 JSON đúng 2 field:
   `conduit_token` (string ~350 chars) + `status`; POST `/f/conversation` trả
   `text/event-stream`. Không hề có wss nào trong flow.

3. **Burp capture nội bộ 24/8 khớp**: WS topic `"conversations"` chỉ phát
   `conversation-created` / `conversation-turn-complete` — không thấy delta.
   Giải thích nay rõ: delta đi trên SSE của POST; WS là kênh notification
   lifecycle (khớp flag `resume_with_websockets`).

4. **gptweb2api PROJECT_STATUS (2026-08-23)**: *"browserless direct transport
   is the default path… Live validation with operator-owned OAuth credentials:
   models catalog, streaming and non-streaming chat all return **200** through
   the pure HTTP path."* LocalSentinel làm toàn bộ integrity round trip
   in-process, KHÔNG mở browser (~22 MB RSS vs Chrome tab). Turnstile hiện
   **announced nhưng không enforced** với operator accounts.

### WS nào tồn tại (và vì sao không phải cứu cánh)

- `wss://chatgpt.com/backend-api/codex/responses` — WS transport của **Codex
  agent** (openai/codex-rs issue #13406, Mar 2026), cùng path với SSE endpoint.
  Không liên quan stream chat thường; account ChatGPT-auth có thể bị đóng
  WS ngay lập tức code **1008 "policy"** (issue #13039). Repo mình đã chặn
  codex/responses ở token layer — WS cùng path không thoát được tầng đó.

### Công thức authed hoàn chỉnh (từ sentinel.go/client.go của gptweb2api)

Bước 1 — requirements/prepare (MỚI, khác endpoint cũ repo đang dùng):
```
POST /backend-api/sentinel/chat-requirements/prepare
body: {"p": <bootstrapProof(userAgent, deviceID)>}
→ {"token" | "prepare_token", "proofofwork": {required, seed, difficulty}}
   (fallback classic /sentinel/chat-requirements nếu non-2xx)
Header khi CHỈ có prepare_token: openai-sentinel-chat-requirements-prepare-token
   (khác tên header so với token cuối)
```

Bước 2 — PoW local nếu `required`: SHA3-512 brute-force tìm answer sao cho
`sha3_512(seed + answer)` ≤ difficulty (lexicographic); proof header
`openai-sentinel-proof-token` dạng prefix `gAAAAAB`. Python có sẵn
`hashlib.sha3_512`. Field order trong proof string quan trọng.

Bước 3 — conduit (non-fatal):
```
POST /backend-api/f/conversation/prepare   (authed = backend-api, không phải backend-anon)
body: {
  "action": "next",
  "client_contextual_info": {"app_name": "chatgpt.com",
    "has_web_push_capabilities": false,
    "web_push_notification_permission": "default"},
  "client_prepare_dispatch": "conversation",
  "client_prepare_source": "chatgpt_web_client",
  "client_prepare_state": "none",          // hoặc success/failure sau preflight
  "conversation_mode": {"kind": "primary_assistant"},
  "local_function_names": [],
  "model": "<model>|auto",
  "parent_message_id": "client-created-root",
  "supported_encodings": ["v1"],
  "supports_buffering": true,
  "system_hints": [],
  "timezone": ..., "timezone_offset_min": ...,
  // + conversation_id nếu continue
}
→ {"conduit_token": "..."}  (~350 chars, short-lived request state)
```

Bước 4 — conversation:
```
POST /backend-api/f/conversation   Accept: text/event-stream
headers: openai-sentinel-{chat-requirements,proof}[-turnstile]-token
         + x-conduit-token (+ oai-session-id, oai-device-id,
           oai-client-version/build-number, x-oai-turn-trace-id…)
         + X-OAI-IS state (opaque integrity state, response update qua
           X-OAI-IS-Update — gptweb2api ghi nhận đây là reliability gap còn lại)
```

## Câu hỏi 2 — Các project bridge còn sống dùng đường nào?

| Project | Đường | Trạng thái |
|---|---|---|
| `5yu4n/gptweb2api` (Go, pushed 24/8) | f/conversation **SSE** + requirements/prepare + PoW local + conduit | Live 200 streaming 23–24/8, browserless mặc định |
| `realasfngl/ChatGPT` | `/backend-anon/f/conversation/prepare` → conduit → 4 sentinel headers | Anon path (không authed) |
| `xtekky/gpt4free` README (live check) | g4f provider ecosystem, không dùng f/conversation trực tiếp | Vẫn maintained nhưng khác kiến trúc |
| codex CLI (`openai/codex-rs`) | `wss://chatgpt.com/backend-api/codex/responses` WS | Chỉ Codex agent; 1008 policy với một số account |
| `jaredboynton/chatgpt-web-opencode` | search result nói "f/conversation prepare→PoW→SSE, not Codex Responses WS"; repo đã 404 khi verify | Không xác minh được |

Không tìm thấy project nào streaming chat qua WebSocket ngoài codex/responses.

## Câu hỏi 3 — Đối chiếu repo + effort

Repo hiện trạng:
- `gpt/transport/curl_transport.py`: **0 hit** conduit/x-conduit-token — POST
  thẳng `/f/conversation` chờ SSE, không prepare → trùng hệ lỗi 403 probe.
- `gpt/transport/token_manager.py`:
  - `prepare_conduit()` (dòng ~487): post **body rỗng `{}`** in-page vào
    `/backend-anon/f/conversation/prepare` → 422 "Field required" (đúng như
    probe thấy). Body chuẩn nay biết (15 field ở trên) + phải là `backend-api`.
  - Sentinel mint đang in-page SentinelSDK (cần page). Chưa có PoW solver
    local — nhưng Python `hashlib.sha3_512` built-in nên port được nguyên.
  - Chưa biết `bootstrapProof` ("p") format — cần đọc thêm sentinel.go dòng
    ~306-360 (field order matters) khi implement.
- `gpt/reverse/js_probe.py` + `recorder.py`: đã có instrumentation WebSocket
  đầy đủ nếu cần capture frame WS sau này.
- curl_cffi **0.16.1 có native `WebSocket`/`AsyncWebSocket`** (docs:
  `session.ws_connect(url, impersonate="chrome", cookies=..., headers=...)`) —
  TLS fingerprint giữ nguyên trên handshake WS, cookies session kế thừa. Nếu
  mai OpenAI ép WS thật thì cắm được ngay vào curl_transport mà không cần
  playwright. Nhưng hôm nay không cần.

Effort revive pure-HTTP authed (không WS):
1. Port recipe LocalSentinel vào token_manager/curl_transport: endpoint
   requirements/**prepare** + 2 tên header token variant + PoW sha3 local +
   prepare body 15 field + x-conduit-token + X-OAI-IS passthrough.
   Ước **1.5–2 ngày** dev + probe quota nhỏ để verify từng bước (mỗi bước
   1 request: requirements → prepare → conversation).
2. Rủi ro chính: bootstrapProof format chưa có source chắc chắn (đọc
   sentinel.go trước); X-OAI-IS state machine chưa mô hình hoá (gptweb2api
   cũng gọi đây là gap); turnstile có thể bị enforce bất kỳ lúc nào → giữ
   fallback in-page SDK mint như hiện tại.

## Câu hỏi 4 — Kết luận

**WS KHÔNG đáng đầu tư** — không có bằng chứng stream chat dùng WS; WS duy
nhất tồn tại là codex/responses (agent transport, đã block token layer ở
repo, có nguy cơ 1008 policy). Ngược lại, **f/conversation SSE đang SỐNG**
bằng chứng 200 thật 23–24/8 từ project độc lập; cái repo mình thiếu chỉ là
body prepare (đã có exact shape) và endpoint sentinel mới. Ưu tiên: port
recipe gptweb2api (effort ~2 ngày, rủi ro thấp hơn CODEX-OAUTH vì không phụ
thuận OAuth flow desktop, và giữ browser transport làm fallback). CODEX-OAUTH
vẫn giữ làm track song song dài hạn nhưng không phải điều kiện tiên quyết.

## Nguồn

- https://github.com/5yu4n/gptweb2api — docs/research/{chatgpt-conversation-prepare,codex-desktop-chat-transport}.md, internal/chatgpt/{sentinel,client,broker}.go, docs/PROJECT_STATUS.md (raw fetched 25/8)
- https://deepwiki.com/realasfngl/ChatGPT — anon conduit flow
- openai/codex-rs issues #13406 (WS connect chatgpt.com/backend-api/codex/responses), #13039 (1008 policy close)
- https://curl-cffi.readthedocs.io/en/latest/websockets.html — native WS API
- Nội bộ: `docs/reports/conduit-probe-2026-08-25.md`, `docs/reports/2026-08-24-live-protocol-findings.md`, `gpt/transport/token_manager.py`, `gpt/transport/curl_transport.py`
