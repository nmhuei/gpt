# SPEC — FastPathTransport (transport thế hệ mới, tối thiểu hóa latency/turn)

- Trạng thái: DRAFT (chưa hiện thực)
- Ngày: 2026-08-24
- Phạm vi: chỉ spec. Không kèm code production.
- Bằng chứng đầu vào:
  - `docs/reports/sentinel-sdk-probe-2026-08-24.md` — `POST /backend-anon/f/conversation` với đủ 3 header sentinel mint từ `globalThis.SentinelSDK.token('chatgpt')` → 200 SSE thật; path authenticated ngoài browser vẫn 403 reputation.
  - `gpt/transport/curl_transport.py` — parser SSE v1 JSON-patch đã hoạt động trong `_consume_record` / `_consume_v1_record`.
  - `gpt/transport/token_manager.py` — cache sentinel theo TTL (`expire_after ≈ 540s`, margin 60s → TTL hiệu dụng 480s); `invalidate_sentinel()` đã có nhưng curl_transport mới là nơi gọi khi 401/403.

## 0. Mục tiêu và tiêu chí thành công

Mục tiêu duy nhất: **tối thiểu tuyệt đối thời gian gửi prompt + tài nguyên** cho một turn nhỏ (<100 ký tự output), không phá contract hiện có của gateway.

Chỉ số chốt (đo từ trace metrics, xem §5):

| Chỉ số | Định nghĩa |
|---|---|
| TTFD | time-to-first-delta: từ lúc `send()` được gọi đến delta văn bản đầu tiên |
| TTT | tổng thời gian turn nhỏ (send → TurnResult) |
| Overhead/turn | phần TTT không do server sinh ra (DOM, mint token, dispatch) |
| RSS | bộ nhớ thường trú toàn process (browser + python) |

## 1. Kiến trúc 3 mode

```
                       ┌────────────────────────────┐
gateway server ───────▶│  FastPathTransportSelector │
transport="fastpath"   └─────────┬──────────────────┘
                                 │ chọn theo auth_status + env
        ┌────────────────────────┼─────────────────────────┐
        ▼                        ▼                         ▼
 (a) AnonHTTPSession     (b) InPageAuthSession      (c) DOM fallback
 curl_cffi POST           page.evaluate(fetch       ChatGPTWebSession
 /backend-anon/...        stream) trong 1 page      hiện tại (giữ nguyên)
 + sentinel mint qua      authenticated
 1 page dùng chung
```

### 1.a. Mode `anon-http`

- Một browser page "minter" duy nhất, dùng chung (tái sử dụng thiết kế `HybridWorkerFactory`: 1 page + `TokenManager`).
- Mỗi turn: `curl_cffi.AsyncSession(impersonate="chrome")` POST thẳng tới `/backend-anon/f/conversation` với envelope header đầy đủ (xem §4.2) — KHÔNG đụng DOM, KHÔNG fill composer.
- Token sentinel: mint in-page bằng SentinelSDK (inject `sdk.js` → `await SentinelSDK.token('chatgpt')` → tách `{p,t,c}`), cache theo TTL như hiện tại.
- Không gửi `Authorization` bearer (path anon). Cookies (`cf_clearance`, `_puid`) + `oai-device-id` vẫn lấy từ context trình duyệt.
- Giới hạn đã biết: quota anon theo IP/fingerprint; conversation id do server cấp, không có history API chính thống → `reconcile()` phải raise `CommitUnknown` (giống `CurlCffiSession.reconcile`).

### 1.b. Mode `auth-inpage`

- Một page authenticated duy nhất giữ ấm suốt đời session. Mỗi turn = **một** `page.evaluate()` chạy async fetch streaming trong page context:

```js
async ({url, payload}) => {
  const resp = await fetch(url, {method:'POST', credentials:'include',
    headers:{'content-type':'application/json','accept':'text/event-stream'},
    body: JSON.stringify(payload)});
  // đọc stream, đẩy từng record về Python qua callback/buffer
}
```

- Lý tưởng nhất: dùng cơ chế streaming hai chiều Playwright (ExposeBinding/`console` channel hoặc `page.evaluate_handle` + ReadableStream pump) để delta chảy về Python realtime; tối thiểu phải pump theo chunk chứ không gom hết cuối turn.
- Ưu điểm: request mang đầy đủ cookie/CF state/TLS fingerprint thật của Chrome, vượt qua lớp reputation đang chặn curl_cffi trên path `/backend-api` — **đang chờ kết quả probe in-page** (2 agent khác đang thử POST trong page context và diff header request thật). Mode này chỉ bật khi probe xác nhận 200 SSE trong page.
- Endpoint: `/backend-api/f/conversation` (authenticated), có bearer tự động từ cookie qua `credentials:'include'`.

### 1.c. Fallback `browser-dom`

- `ChatGPTWebSession` hiện tại, giữ nguyên làm đường an toàn cuối. Không sửa gì.

### 1.d. Selector mode (tự động)

Thứ tự quyết định khi khởi tạo worker factory (và re-evaluate mỗi khi account health đổi trạng thái):

1. Env override cứng: `WEBGPT_FASTPATH_MODE` ∈ {`anon-http`, `auth-inpage`, `dom`} — nếu đặt, thắng mọi heuristic.
2. Nếu `WEBGPT_MODE`/mock backend bật → đi thẳng local-mock path hiện có, không chọn fastpath.
3. Theo `auth_status` của account (từ `AccountHealthTracker` / capability snapshot):
   - `anonymous` hoặc chưa đăng nhập → `anon-http`.
   - `authenticated` VÀ probe in-page đã xác nhận (`auth-inpage` gate flag, §5) → `auth-inpage`.
   - `authenticated` nhưng gate chưa mở → `anon-http` nếu còn quota anon, ngược lại `dom`.
   - `required`/`blocked` → `dom` (giữ hành vi login-wall hiện tại).
4. Khi một mode lỗi liên tiếp ≥ N lần (mặc định N=2, env `WEBGPT_FASTPATH_FALLBACK_THRESHOLD`) trong window 10 phút → hạ vĩnh viễn xuống mode thấp hơn trong turn đó, ghi event `StateChanged` kèm lý do, không tự nâng trở lại trong process life (tránh oscillation); nâng lại cần restart hoặc lệnh admin.

## 2. Ngân sách latency mục tiêu

Baseline DOM hiện tại: overhead ~1.5–4s/turn (fill composer, poll 0.12s, stable_grace 0.45s, read DOM).

| Chỉ số | (a) anon-http | (b) auth-inpage | (c) dom (baseline) |
|---|---|---|---|
| TTFD — warm (sentinel cached, page ấm) | **≤ 600 ms** (chỉ RTT POST + first SSE byte) | **≤ 500 ms** (dispatch evaluate ~10–50ms + RTT) | 1.5–4 s |
| TTFD — cold (phải mint sentinel / init SDK) | ≤ 2.5 s (SDK token resolve 1–2s + RTT) | ≤ 1.5 s (SDK thường đã load sẵn trong page auth) | 1.5–4 s |
| TTT — turn nhỏ <100 ký tự out | ≤ 2 s warm | ≤ 1.8 s warm | 3–7 s |
| Overhead Python/turn (ngoài network) | ≤ 30 ms | ≤ 50 ms (evaluate round-trip + pump) | 1500–4000 ms |
| Process/RSS | curl_cffi (~30 MB) + 1 browser page minter (~250–350 MB chia sẻ) | 1 browser page authenticated (~250–350 MB), không thêm process | 1 browser + full UI polling (~300–450 MB) |
| Số browser page sống | 1 (minter) | 1 (auth page) | 1–N (theo worker) |

Nguyên tắc ngân sách: mọi con số trên là **gate rollout**, không phải ước lượng mềm. Shadow phase (§5) phải đo được từng ô trước khi flip default.

Ghi chú đo TTFD: mốc bắt đầu là lúc `send()` vào lock; mốc kết thúc là event `ResponseDelta` đầu tiên — trùng cách trace metrics hiện tại nên so sánh trực tiếp với baseline được.

## 3. Vòng đời token & quản lý page ấm

### 3.1. Sentinel TTL và invalidate

Hiện trạng: cache TTL = `expire_after(≈540s) − margin(env, default 60s)` = 480s; `invalidate_sentinel()` tồn tại; `CurlCffiTransport._raise_for_status` đã gọi invalidate khi 401/403.

Spec bổ sung:

1. **Invalidate chủ động (pre-emptive):** selector/factory chạy background task kiểm tra mỗi 30s; khi TTL còn lại < 90s và có traffic gần đây (< 5 phút), pre-mint sentinel mới **lúc idle** thay vì để turn kế tiếp chịu chi phí mint. Không mint khi hệ thống rảnh hoàn toàn (tiết kiệm quota requirements).
2. **Invalidate phản ứng:** giữ nguyên quy tắc 401/403 hiện có; bổ sung — response trả JSON error (không SSE) với mã ≥ 400 bất kỳ cũng invalidate trước khi classify exception, vì một số rejection shape không đi qua `_raise_for_status` chuẩn.
3. **Invalidate theo page lifecycle:** bất kỳ reload/navigation nào trên page minter/auth → invalidate sentinel cache ngay (instance SentinelSDK chết theo navigation; token cũ vẫn hợp lệ server-side nhưng khả năng mint lại phải xác nhận SDK init xong trước khi cho phép turn).
4. **Không invalidate khi đổi conversation_id:** token sentinel scope theo flow chứ không theo conversation (probe xác nhận mint 1 lần dùng cho POST mới); đổi hội thoại KHÔNG mint lại.
5. Telemetry: mỗi mint/invalidate phát một ProbeEvent `{reason, age_s, ttl_s}` vào trace để shadow phase đối chiếu tỷ lệ hit/miss cache.

### 3.2. Giữ page ấm (không reload giữa các turn)

- Cấm gọi `page.reload()` giữa các turn ở mode fastpath. Recovery page chỉ xảy ra khi page đóng/crash (kiểm tra `is_closed()`), và sau recovery phải: goto lại target → inject lại `sdk.js` → `SentinelSDK.init(...)` → verify `typeof globalThis.SentinelSDK.token === 'function'` → mới chuyển state `READY`.
- Idle keeper: task nền mỗi 60s thực hiện 1 evaluate no-op nhẹ (`globalThis.SentinelSDK ? 'up' : 'down'`) để (i) phát hiện navigation do site tự reload, (ii) giữ connection pool CF không lạnh. Nếu thấy `'down'` → coi như recovered-needed ở mục trên.
- Với `auth-inpage`: không điều hướng page sang conversation khác giữa các turn; conversation_id thuần logic trong payload, không cần URL đúng.
- Chống nhiễu popup/dialog: tái dùng `dismiss_popups` chỉ lúc bootstrap/recovery, không chạy mỗi turn.

## 4. Interface class & điểm hook

### 4.1. Method signatures (khớp contract `WorkerFactory` / duck-type session)

Hai class mới trong `gpt/transport/fastpath.py`:

```python
class FastPathWorkerFactory:
    # Khớp HybridWorkerFactory (hybrid.py):
    def __init__(self, browser_manager, *, max_workers=1, warm_workers=1,
                 queue_timeout=30.0, target_url="https://chatgpt.com",
                 auto_login=None, allow_local_mock=None,
                 mode: str | None = None) -> None          # mode = override env
    async def start(self) -> None
    async def acquire(self) -> tuple[str, FastPathSession]
    async def release(self, session_id: str, *, reusable: bool = True) -> None
    @asynccontextmanager
    async def lease(self) -> AsyncIterator[FastPathSession]
    async def stats(self) -> WorkerFactoryStats            # factory.WorkerFactoryStats
    async def close(self) -> None
    # bổ sung:
    def active_mode(self) -> str                           # 'anon-http' | 'auth-inpage' | 'dom'

class FastPathSession:
    # Duck-type khớp CurlCffiSession (hybrid.py) — những gì gateway gọi:
    session_id: str
    state: SessionState                                    # property
    conversation_id: str | None                            # property
    async def new_conversation(self, model: str | None = None) -> SessionInfo
    async def open(self, conversation_id: str) -> SessionInfo
    async def select_model(self, model: str) -> None
    async def select_reasoning_effort(self, effort: str) -> None
    async def models(self) -> list[ModelInfo]
    async def send(self, text: str, timeout_seconds: float = 120,
                   model: str | None = None, reasoning_effort: str | None = None,
                   files: tuple[str, ...] | None = None) -> TurnResult
    def events(self) -> AsyncIterator[SessionEvent]
    def drain_events(self) -> list[SessionEvent]
    async def reconcile(self, expected_user_text: str) -> ReconciliationResult
    async def close(self) -> None
    def get_info(self) -> SessionInfo
```

Quy ước bên trong (bắt buộc để tương thích trace/tool pipeline hiện tại):

- `send()` phát chuỗi event giống `CurlCffiSession.send`: `ResponseStarted(turn_id, model)` ngay trước khi POST → `ResponseDelta(text, accumulated_text)` mỗi delta → `ResponseCompleted(...)` hoặc `ResponseFailed(..., partial_text)` khi lỗi.
- Trả `TurnResult(turn_id, conversation_id, text, model, status, duration_ms)` — parser SSE tái dùng `CurlCffiTransport._consume_record` (refactor thành hàm module-level dùng chung, không copy).
- `reconcile()` raise `CommitUnknown` cho cả hai mode HTTP (không có history chính thống); mode `dom` delegate sang `ChatGPTWebSession.reconcile` thật.
- `files` non-empty → raise `ValueError` như hybrid.
- Mode chọn per-factory (một factory một mode); chuyển mode giữa chừng = factory mới, không mutate factory sống.

### 4.2. Envelope request (mode anon-http)

Header set tối thiểu đã xác minh 200 (probe 2026-08-24):

- `Accept: text/event-stream`, `Content-Type: application/json`
- `Cookie:` full jar từ context (ít nhất `cf_clearance`, `_puid`)
- `oai-device-id`
- `openai-sentinel-chat-requirements-token` ← `c`
- `openai-sentinel-proof-token` ← `p`
- `openai-sentinel-turnstile-token` ← `t`
- `oai-telemetry` (timing array từ `SentinelSDK.timing()`)
- KHÔNG `Authorization`. `Origin`/`Referer` chatgpt.com.

Payload: giữ nguyên shape `_build_conversation_payload` (action=next, messages, model, parent_message_id, conversation_mode) — endpoint anon chấp nhận cùng schema.

### 4.3. Điểm hook vào gateway server

Sửa `gpt/gateway/server.py`:

1. Dòng validate transport (~316): mở rộng whitelist `("hybrid", "browser", "fastpath")`.
2. Dòng chọn factory_class (~330): `factory_class = FastPathWorkerFactory if transport == "fastpath" else ...`; nhánh multi-account bọc `MultiAccountWorkerFactory` quanh FastPathWorkerFactory như hiện tại với hybrid.
3. `readiness()` (~618): thêm nhánh `self.transport == "fastpath"` — `auth_status` lấy từ `factory.active_mode()` + account health thay vì hard-code `"authenticated"`; `active_mode` đưa thêm vào payload readiness để observability.
4. CLI/env: cờ `--transport fastpath` map từ env `WEBGPT_TRANSPORT=fastpath` (theo pattern hiện có); `WEBGPT_FASTPATH_MODE` cho override mode con.
5. Trace metrics: gateway đã có trace_path — bổ sung trường `fastpath_mode`, `ttfd_ms`, `overhead_ms` vào record trace để shadow so sánh (§5).

Không đổi gì `ChatGPTWebSession`, drivers, hay tool transpiler.

## 5. Kế hoạch rollout có gate

Giai đoạn | Nội dung | Gate pass | Hành động fail

1. **Unit test mock**
   - Test FastPathSession contract bằng mock transport/page (pattern tests/test_session.py, test_multi_account.py): event sequence, TurnResult shape, sentinel invalidate triggers, selector mode matrix, fallback threshold.
   - Gate: toàn suite xanh, không cần network/browser.

2. **Smoke live 2 turn**
   - Chạy thủ công `transport=fastpath`, mode `anon-http`, 2 turn ("Say exactly: pong" + 1 follow-up cùng conversation).
   - Gate: 2/2 turn 200 SSE, TurnResult.status == completed, conversation_id ổn định giữa 2 turn, TTFD đo được và ghi log.
   - Fail → dừng, phân tích bằng probe script, quay lại giai đoạn 1; không sang shadow.

3. **Shadow mode song song**
   - Gateway nhận request qua transport cũ nhưng đồng thời bắn bản sao prompt qua fastpath (kết quả fastpath bỏ đi, chỉ ghi trace). Env `WEBGPT_FASTPATH_SHADOW=1`.
   - So sánh theo trace metrics: TTFD, TTT, error rate, tỷ lệ sentinel cache-hit, RSS.
   - Gate (≥ 50 turn shadow, ≥ 2 ngày): TTFD p50 fastpath < baseline p50 × 0.5; error rate fastpath ≤ baseline + 2%; không có regression RSS > 15%.
   - Fail → giữ default cũ, ghi báo cáo docs/reports/, chỉnh spec hoặc bỏ mode lỗi.

4. **Flip default**
   - Đổi default transport của launcher/gateway sang `fastpath` (mode tự động), giữ `WEBGPT_TRANSPORT=browser|hybrid` để rollback tức thì.
   - Rollback từng bước: (i) env override `WEBGPT_TRANSPORT=hybrid` không cần restart image; (ii) `WEBGPT_FASTPATH_MODE=dom` ép fallback mà vẫn đi qua code fastpath; (iii) revert commit flip nếu cần dài hạn. Mỗi bước đều độc lập và có tài liệu env tương ứng trong .env.example.

## 6. Rủi ro & điều kiện dừng

| # | Rủi ro | Khả năng | Điều kiện dừng (kill switch) |
|---|---|---|---|
| R1 | Quota anon cạn (IP/fingerprint bị chặn, redirect login wall) | Cao khi dùng nhiều | 2 turn liên tiếp nhận login-wall/quota → hạ về `dom`/auth-inpage, dừng mode anon trong process |
| R2 | Path auth-inpage thất bại probe (403 ngay trong page) | Trung bình (đang chờ kết quả) | Probe không ra 200 SSE → mode này không bao giờ bật; chỉ ship anon-http + dom |
| R3 | Reputation 403 lan sang cả anon path | Thấp–TB | 403 "Unusual activity" trên anon → dừng toàn bộ fastpath HTTP, fallback dom, mở incident report |
| R4 | SentinelSDK đổi API/key name (`token(flow)`, `{p,t,c}`) | TB (site đổi frontend thường xuyên) | Mint fail 3 lần liên tiếp → fallback dom; unit test fixture phải cập nhật theo bundle mới trước khi bật lại |
| R5 | Cloudflare challenge cản curl_cffi (TLS/JA3 đổi) | TB | Response 403 cf-mitigated / HTML challenge → invalidate + 1 retry; fail tiếp → dom |
| R6 | Streaming evaluate bị buffer (không realtime) làm TTFD xấu | TB với auth-inpage | Shadow đo TTFD auth-inpage > 1.5× budget → không flip mode này |
| R7 | Race/concurrency trên 1 page (nhiều turn song song) | TB | Enforce max_workers semaphore như hybrid; test race trong unit; vi phạm thứ tự event → fail stage 1 |
| R8 | Page crash/navigation giữa turn → CommitUnknown sai trạng thái | Thấp | Recovery bắt buộc verify SentinelSDK trước READY; không verify được → raise AuthRequired, không retry mù |
| R9 | Chi phí duy trì 2 codepath (fastpath + dom) | Chắc chắn xảy ra | Chấp nhận có chủ đích; dom là contract an toàn, không refactor trong scope này |

Điều kiện dừng toàn dự án (quay lại kiến trúc cũ hoàn toàn): R3 xảy ra ổn định trên cả hai mode HTTP trong > 24h, hoặc shadow cho thấy không mode nào đạt gate TTFD — khi đó tài liệu hóa kết quả âm trong docs/reports/ và giữ `hybrid`/`browser` làm mặc định vĩnh viễn.

## 7. File ảnh hưởng (khi hiện thực — ngoài scope spec này)

- Mới: `gpt/transport/fastpath.py`, `tests/test_fastpath.py`
- Sửa nhẹ: `gpt/gateway/server.py` (whitelist + factory_class + readiness), `.env.example` (env flags), `gpt/transport/curl_transport.py` (tách `_consume_record` thành helper dùng chung — optional)
- Không đổi: `session.py`, `hybrid.py`, drivers, toolcall, orchestrator
