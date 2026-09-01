# F/CONVERSATION RECIPE — SPEC TRIỂN KHAI CHÍNH XÁC TỪNG FIELD (2026-08-25)

Mục đích: biến recipe tổng quát trong `ws-stream-research-2026-08-25.md` thành
spec byte-chính-xác để agent implement ngày mai port LocalSentinel vào
`gpt/transport/{token_manager,curl_transport}.py`.

Nguồn (raw fetched 25/8/2026 từ `5yu4n/gptweb2api@main`, repo sống validated
live 200 ngày 23–24/8):

- `internal/chatgpt/sentinel.go` (411 dòng) — requirements/prepare, PoW, conduit
- `internal/chatgpt/client.go` (773 dòng) — envelope headers SSE + conversation body
- `internal/chatgpt/prepare.go` — broker context contract (tham khảo)
- `internal/chatgpt/resume.go` — tái dụng conduit trên resume
- `docs/DIRECT_TRANSPORT.md`, `docs/research/chatgpt-conversation-prepare.md`,
  `docs/research/codex-desktop-chat-transport.md`

Đối chiếu local: `gpt/transport/curl_transport.py::_build_headers` (~dòng 705),
`gpt/transport/token_manager.py::prepare_conduit` (dòng 490, hiện POST `{}` rỗng
vào `/backend-anon/f/conversation/prepare` → 422).

---

## 1. `POST /sentinel/chat-requirements/prepare`

### Wire shape

```
POST https://chatgpt.com/backend-api/sentinel/chat-requirements/prepare
Content-Type: application/json
Accept: application/json
Authorization: Bearer <access_token>
User-Agent: <UA>
OAI-Language: en-US
OAI-Device-Id: <uuid>
OAI-Session-Id: <uuid>
X-OAI-Turn-Trace-Id: <uuid>        # nếu có
ChatGPT-Account-ID: <uuid>          # chỉ khi biết account id

body: {"p": "<bootstrapProof>"}
```

Chỉ đúng 1 field `"p"` trong body. KHÔNG kèm sentinel headers ở bước này.

- Non-2xx → fallback **một lần** sang classic `POST /backend-api/sentinel/chat-requirements`
  cùng body `{"p": ...}` (hai thế hệ server cùng tồn tại).
- Base là `/backend-api` (authed), không phải `/backend-anon` như đường cũ của repo mình.

### BootstrapProof (`p`) lấy từ đâu, tính thế nào

KHÔNG lấy từ trang thật/globalThis — gptweb2api **tự solve local**, in-process,
không browser. Công thức (`sentinel.go::bootstrapProof`):

1. `seed = format(rand.random(), '.6f')` — float64 0..1 in 6 chữ số thập phân
   (Go: `strconv.FormatFloat(rand.Float64(), 'f', 6, 64)`).
2. `answer = solveSentinelAnswer(seed, difficulty="0", userAgent, deviceID)` — xem §2.
3. `p = "gAAAAAC" + answer` (prefix **C**, khác proof-token bước sau prefix **B**).
4. Cache kết quả **10 phút** (`sentinelBootstrapProofTTL`) vì answer có timestamp
   bên trong (element `[17]`), hết TTL thì re-solve với seed mới.

### Response fields (tên chính xác)

```json
{
  "token": "...",                  // hoặc thiếu — final requirements token
  "prepare_token": "...",          // hoặc thiếu — prepare-stage token
  "proofofwork": {                 // object, có thể null/vắng
    "required": true,
    "seed": "...",
    "difficulty": "05..."           // chuỗi hex-prefix, độ dài thay đổi
  },
  "proof_challenge": { ... }       // shape giống proofofwork — fallback nếu không có proofofwork
}
```

Parser đọc `token` rồi `prepare_token`; proof đọc `proofofwork` trước, thiếu mới
đọc `proof_challenge` (khớp cả 2 tên seed/difficulty: `seed`, `difficulty`).

### Ánh xạ header sau khi có token

| Response có | Header gửi đi |
|---|---|
| `token` | `OpenAI-Sentinel-Chat-Requirements-Token` |
| chỉ `prepare_token` | `OpenAI-Sentinel-Chat-Requirements-Prepare-Token` |

Không có cái nào → lỗi, không đi tiếp. Hai tên header này KHÁC nhau và chỉ
một được dùng cho từng response.

---

## 2. PoW SHA3-512 — thuật toán chính xác

Core (`sentinel.go::solveSentinelAnswer`):

```
config = sentinelFingerprint(userAgent, deviceID)   # mảng 18 phần tử, thứ tự CỐ ĐỊNH
for attempt in 0..500_000:
    config[3] = float(attempt)                       # counter
    config[9] = float(elapsed_ms)                    # ms trôi qua từ lúc bắt đầu
    encoded = json.dumps(config, separators=(",", ":"))   # compact, giữ kiểu số Go-style
    answer  = base64.b64encode(encoded.encode())
    digest  = hashlib.sha3_512((seed + answer).encode()).hexdigest()
    if digest[:len(difficulty)] <= difficulty:       # so SỔ TỪ ĐIỂN trên hex
        return answer                                 # KHÔNG có prefix gì thêm ở return này
```

Input hash = chuỗi nối trực tiếp `seed + base64(config)` (không dấu ngăn cách).
Difficulty check: prefix hex của digest phải **≤** difficulty theo so sánh
lexicographic từng ký tự (không phải numeric). Empty difficulty ⇒ vòng lặp
không bao giờ match (`len(target) > 0` guard trong Go) — coi như không giải được.

Ngân sách: tối đa **500.000 attempts** (`sentinelProofMaxAttempts`); khó khăn
quan sát thấy (DIRECT_TRANSPORT.md) giải trong *tens of milliseconds*.

Fallback bootstrap (chỉ đường `p`, không phải proof header): nếu cạn ngân sách,
trả `"wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + base64(json('"' + seed + '"'))`.
Server-challenge cạn ngân sách → raise lỗi (không fallback).

Prefix cuối cùng:

- Proof header `OpenAI-Sentinel-Proof-Token` = `"gAAAAAB" + answer`
- Body `{"p": ...}` = `"gAAAAAC" + answer`

### Mảng fingerprint 18 phần tử (thứ tự quan trọng, index 0-based)

| idx | Giá trị | Kiểu |
|---|---|---|
| 0 | `4000` | int |
| 1 | `"<ddd> <DD> <mmm> <yyyy> <HH>:<MM>:<SS> GMT<±><HH><MM>"` ví dụ `Mon Aug 25 2026 14:03:09 GMT+0700` | str (format `%a %d %b %Y %H:%M:%S GMT%z`-style; giờ LOCAL máy, offset tính từ `now.Zone()`) |
| 2 | `4294705152` | int |
| 3 | attempt counter | float, khởi tạo 0 |
| 4 | `userAgent` | str — PHẢI khớp UA header gửi kèm request |
| 5 | `None` | null |
| 6 | `"prod-a696433ddfe0489db6696cae8c5778c2128f26e8"` | str (build hash) |
| 7 | `"en-US"` | str |
| 8 | `"en-US"` | str |
| 9 | elapsed ms | float, khởi tạo 0 |
| 10 | `"webkitGetUserMedia−function webkitGetUserMedia() { [native code] }"` | str — ký tự giữa là **U+2212 MINUS SIGN**, không phải hyphen! |
| 11 | `"location"` | str |
| 12 | `"ontransitionend"` | str |
| 13 | `123.456` | float |
| 14 | `deviceID` | str — khớp `OAI-Device-Id` header |
| 15 | `""` | str rỗng |
| 16 | `8` | int |
| 17 | `now_microseconds / 1000` (= epoch millis, dạng float) | float |

JSON encode: Go `json.Marshal` in compact (no spaces); Python cần
`separators=(",", ":")` để ra cùng byte-shape. Số nguyên in không có `.0`;
float in có dạng `4000`→Go in `4000` cho int riêng, nhưng `float64(0)` in `0`.
Trong Go mảng là `[]any` nên `4000` (int) in `4000`, `float64(0)` in `0` —
Python `json.dumps(0)` cũng ra `0`. Counter `float64(attempt)` in `123` (nguyên)
khi giá trị nguyên. **[CẦN VERIFY]**: Go marshal `float64` luôn in dạng ngắn nhất
(`0`, `123`, `123.456`) — khớp Python dumps cho số nguyên-valued float;
nên test 1 lần bằng probe thật trước khi tin 100%.

### Code mẫu Python (port 1:1)

```python
import base64, hashlib, json, random, time

MAX_ATTEMPTS = 500_000

def _date_str() -> str:
    import locale
    now = time.localtime()
    # Go Format("Mon")/"Jan" là tiếng Anh cố định — hardcode bảng, đừng dùng locale
    days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    mons = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    off = time.timezone if now.tm_isdst == 0 else time.altzone  # giây west-positive
    sign = "-" if off <= 0 else "+"      # Go: offset dương (east) => "+"
    ...
```

*(bản chạy đầy đủ nằm ở sentinel.go 332–380; port nguyên văn, chú ý 2 điểm:
tên thứ tháng tiếng Anh hardcode, và `digest.startswith` so lexicographic
`digest[:len(d)] <= d`)*

---

## 3. `POST /backend-api/f/conversation/prepare` — body đủ field

### Headers đi kèm (postJSON + extraHeaders = integrity headers vừa kiếm được)

```
Content-Type: application/json
Accept: application/json
Authorization: Bearer <access_token>
User-Agent: <UA — cùng UA đã dùng cho PoW>
OAI-Language: en-US
ChatGPT-Account-ID: <nếu có>
OAI-Device-Id: <uuid — CÙNG device id với PoW fingerprint>
OAI-Session-Id: <uuid — cùng session id của turn>
X-OAI-Turn-Trace-Id: <uuid — cùng trace id của turn>
OpenAI-Sentinel-Chat-Requirements-Token: ...   # hoặc -Prepare-Token variant
OpenAI-Sentinel-Proof-Token: gAAAAAB...         # nếu PoW required
```

Codex Desktop còn gửi `x-conduit-token` cũ hoặc literal no-token marker ở bước
này; gptweb2api KHÔNG làm vậy mà vẫn 200 live. Marker literal chưa biết giá trị
**[CẦN VERIFY]**.

### Body — 14 field cố định + 1 điều kiện = 15

```json
{
  "action": "next",
  "client_contextual_info": {
    "app_name": "chatgpt.com",
    "has_web_push_capabilities": false,
    "web_push_notification_permission": "default"
  },
  "client_prepare_dispatch": "conversation",
  "client_prepare_source": "chatgpt_web_client",
  "client_prepare_state": "none",
  "conversation_mode": { "kind": "primary_assistant" },
  "local_function_names": [],
  "model": "auto",
  "parent_message_id": "client-created-root",
  "supported_encodings": ["v1"],
  "supports_buffering": true,
  "system_hints": [],
  "timezone": "Asia/Ho_Chi_Minh",
  "timezone_offset_min": 420,
  "conversation_id": "<36-char uuid>"
}
```

| Field | Kiểu | Ghi chú |
|---|---|---|
| `action` | str | luôn `"next"` |
| `client_contextual_info.app_name` | str | `"chatgpt.com"` |
| `client_contextual_info.has_web_push_capabilities` | bool | `false` |
| `client_contextual_info.web_push_notification_permission` | str | `"default"` |
| `client_prepare_dispatch` | str | `"conversation"` |
| `client_prepare_source` | str | `"chatgpt_web_client"` |
| `client_prepare_state` | str | `"none"` khi preflight chưa chạy xong; `"success"`/`"failure"` nếu stream bắt đầu sau khi preflight hoàn tất |
| `conversation_mode.kind` | str | `"primary_assistant"` |
| `local_function_names` | [] | rỗng |
| `model` | str | slug (`gpt-5`…) hoặc `"auto"` |
| `parent_message_id` | str | `"client-created-root"` cho hội thoại mới (literal, KHÔNG phải uuid); khi continue = parent uuid thật |
| `supported_encodings` | ["v1"] | 1 phần tử |
| `supports_buffering` | bool | `true` |
| `system_hints` | [] | rỗng |
| `timezone` | str | IANA name |
| `timezone_offset_min` | int | phút east-of-UTC (VN = +420); tính từ tz database, không hardcode |
| `conversation_id` | uuid str | CHỈ thêm khi continue hội thoại; vắng mặt khi mới |

Browser thật quan sát được (research doc 1/8) còn gửi `local_function_names`
length-1 (item string length 22) — gptweb2api gửi `[]` vẫn 200 live.

### Response

200 `application/json`, đúng 2 top-level field:

```json
{ "conduit_token": "<~350 chars>", "status": "ok" }
```

(status length-2 quan sát được — khả năng `"ok"`; giá trị chính xác chưa capture
**[CẦN VERIFY]**.)

---

## 4. Conduit token — dùng ở đâu

- Gửi làm header **`X-Conduit-Token`** trên `POST /backend-api/f/conversation`,
  đặt KÈM bộ sentinel headers + envelope đầy đủ (không thay thế gì).
- Tái dụng trên `POST /backend-api/f/conversation/resume`: body
  `{"conversation_id": ..., "offset": 0|1|2}`, headers = base envelope +
  `X-Conduit-Token` + cùng `X-OAI-Turn-Trace-Id` của turn gốc.
- **Non-fatal**: prepare fail → log, bỏ conduit, stream vẫn đi với integrity
  headers thôi (Codex Desktop behavior; gptweb2api làm y vậy và live OK).
- Response `/f/conversation` mang về `x-conduit-token` mới (cùng `x-oai-is-receipt`,
  `x-oai-is-update`, `x-oai-request-id`) — token mỗi turn mint mới, không reuse
  chéo turn trong code gptweb2api.
- TTL: tài liệu chỉ nói "short-lived request state"; **không con số nào** được
  ghi trong repo **[CẦN VERIFY]** — an toàn nhất là mint mỗi turn như gptweb2api.

---

## 5. SSE `POST /backend-api/f/conversation` — headers đầy đủ

Composite từ `webHeaders()` + per-turn + requirements provider + transport
(client.go Stream 271–308):

```
Accept: text/event-stream                      ← override Accept json
Accept-Language: en-US,en;q=0.9
Authorization: Bearer <access_token>
Cache-Control: no-cache
ChatGPT-Account-ID: <nếu có>
Content-Type: application/json
Cookie: __Secure-next-auth.session-token=<…>; <cf cookies nếu có>
OAI-Client-Build-Number: <nếu cấu hình>
OAI-Client-Version: <nếu cấu hình>
OAI-Device-Id: <uuid ổn định theo account>
OAI-Language: en-US
OAI-Session-Id: <uuid4 MỚI mỗi turn>
OpenAI-Sentinel-Chat-Requirements-Token: …     # hoặc -Prepare-Token
OpenAI-Sentinel-Proof-Token: gAAAAAB…          # khi PoW required
Origin: https://chatgpt.com
Pragma: no-cache
Referer: https://chatgpt.com/
User-Agent: <UA — cùng UA trong PoW fingerprint>
X-Conduit-Token: …                             # khi prepare thành công
X-OAI-Turn-Trace-Id: <uuid4 MỚI mỗi turn>
```

`OpenAI-Sentinel-Turnstile-Token` hiện bị server announce nhưng không enforce —
header bỏ trống; nếu một ngày bị ép mà thiếu → 403.

Identity constraint: **UA, Device-Id, Session-Id, Language phải GIỐNG NHAU tuyệt
đối qua cả 3 call (requirements → prepare → conversation) trong cùng 1 turn**
(comment sentinel.go 91–93). Session-Id/Trace-Id đổi mỗi TURN, Device-Id ổn định
theo account (derive `sha256(cookie)[:16]` đóng khung uuid-v4, hoặc random khi
OAuth-only).

### Cookies bắt buộc

- `__Secure-next-auth.session-token` — đường cookie-session kinh điển
  (normalizeCookie bọc giá trị trần vào tên này).
- Cloudflare cookies (`cf_clearance`…) nạp vào cookie-jar từ config; chỉ thật sự
  cần khi gặp challenge. Deployments OAuth-bearer thuần chạy live KHÔNG cần
  cookie nào (Cookie header chỉ set khi rỗng≠).
- Repo mình: `_build_headers` hiện coi `cf_clearance` là BẮT BUỘC — khi port
  có thể nới thành optional-nhưng-ưu tiên, vì gptweb2api chứng minh Bearer thuần đủ.

### Đối chiếu `_build_headers` hiện tại (curl_transport.py ~741–774)

| Header | Local hiện có | gptweb2api gửi | Action khi port |
|---|---|---|---|
| Accept event-stream | ✅ | ✅ | giữ |
| Authorization Bearer | ✅ | ✅ | giữ |
| Content-Type json | ✅ | ✅ | giữ |
| Cookie | ✅ (cf_clearance bắt buộc) | optional jar | nới |
| Origin/Referer | ✅ | ✅ | giữ |
| User-Agent | ✅ | ✅ + tham gia fingerprint | giữ, đảm bảo cùng UA nạp PoW |
| oai-language | ✅ | ✅ | giữ |
| oai-device-id | ✅ bắt buộc | ✅ ổn định theo account | giữ |
| openai-sentinel-chat-requirements-token | ✅ (in-page SDK mint) | ✅ hoặc Prepare variant | thêm nhánh prepare_token |
| openai-sentinel-proof-token | ✅ (in-page) | ✅ local PoW | thêm solver local |
| openai-sentinel-turnstile-token | ✅ (in-page) | bỏ (không enforce) | giữ optional |
| **oai-session-id** | ❌ | ✅ mỗi turn | THÊM |
| **x-oai-turn-trace-id** | ❌ | ✅ mỗi turn | THÊM |
| **oai-client-version/build-number** | ❌ | ✅ nếu có | THÊM (giá trị [CẦN VERIFY] — lấy từ page thật) |
| **accept-language** | ❌ | ✅ | THÊM |
| **cache-control/pragma no-cache** | ❌ | ✅ | THÊM |
| **x-conduit-token** | ❌ | ✅ | THÊM (cả prepare step trước đó) |
| **chatgpt-account-id** | ❌ | ✅ nếu có | THÊM khi bundle có account id |

### Phụ lục — conversation body (`buildConversationPayload`, client.go 585–639)

Khác prepare-body ở chỗ có `messages` và vài field riêng:

```json
{
  "action": "next",
  "messages": [{"id": "<uuid>", "author": {"role": "user"},
                 "content": {"content_type": "text", "parts": ["..."]},
                 "metadata": {"selected_github_repos": [], "selected_all_github_repos": false,
                              "serialization_metadata": {"custom_symbol_offsets": []}},
                 "create_time": <epoch-float>}],
  "parent_message_id": "client-created-root",
  "model": "auto",
  "client_prepare_state": "none",
  "timezone_offset_min": 420,
  "timezone": "Asia/Ho_Chi_Minh",
  "history_and_training_disabled": true,
  "conversation_mode": {"kind": "primary_assistant"},
  "enable_message_followups": true,
  "system_hints": [],
  "supports_buffering": true,
  "supported_encodings": ["v1"],
  "client_contextual_info": {"app_name": "chatgpt.com"},
  "paragen_cot_summary_display_override": "allow",
  "force_parallel_switch": "auto",
  "conversation_id": "<chỉ khi continue>"
}
```

---

## DANH SÁCH [CẦN VERIFY]

1. **No-token marker literal** Codex Desktop gửi làm `x-conduit-token` trên
   prepare khi chưa có conduit — không có trong Go source của gptweb2api
   (họ bỏ qua bước này và vẫn 200).
2. **TTL số của conduit_token** — repo chỉ nói "short-lived"; mint-per-turn là
   thiết kế an toàn đã chứng minh.
3. **Giá trị chính xác `status`** trong response prepare (length-2, nghi `"ok"`).
4. **Byte-format số float khi JSON-encode fingerprint** — Go marshal vs Python
   dumps phải ra cùng bytes cho `float64(attempt)` nguyên giá trị; test 1 probe
   thật trước khi tin.
5. **Giá trị `prod-a69643…` (idx 6) và các hằng số fingerprint khác** — snapshot
   8/2026, có thể rotate theo build; nếu chuẩn bị suddenly 403 thì soi lại đây.
6. **Ký tự U+2212** trong chuỗi webkitGetUserMedia (idx 10) — phải copy đúng
   byte; dễ sai khi gõ tay.
7. **X-OAI-IS state machine** — cả hai project đều chưa mô hình hoá; gptweb2api
   gọi đây là reliability gap còn lại. Chưa port được, chấp nhận thiếu.
8. **Turnstile enforcement** — hiện off với operator accounts; bất kỳ lúc nào
   có thể bật → giữ fallback in-page SDK mint của repo như kế hoạch.
9. **OAI-Client-Version/Build-Number giá trị** — cần lấy từ page thật (biến
   global trên chatgpt.com); gptweb2api để trống cũng live OK nhưng nên điền.
