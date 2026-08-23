# Plan: Hybrid Browser + curl_cffi (Solution B)

> Browser chạy ngầm headless, chỉ dùng để lấy/refresh token.
> Mọi request thực tế đi qua `curl_cffi` — nhanh, nhẹ, không hiện cửa sổ.

---

## Kiến trúc tổng quan

```
┌─────────────────────────────────────────────────────────────────┐
│                    Claude Code CLI / OpenCode                    │
│              POST /v1/messages (Anthropic protocol)              │
└─────────────────────┬───────────────────────────────────────────┘
                      │ HTTP
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                  API Server (Starlette :8000)                    │
│    gpt/api/server.py — parse request, route to runtime          │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│              CompletionRuntime / Worker Factory                   │
│                                                                  │
│  ┌──────────────────┐     ┌──────────────────────────────────┐  │
│  │  TokenManager    │────▶│  CurlCffiTransport               │  │
│  │  (browser ngầm)  │     │  (curl_cffi session pool)        │  │
│  │                  │     │                                  │  │
│  │  • Lấy cookies   │     │  • POST /backend-api/conversation│  │
│  │  • Lấy Bearer    │     │  • Stream SSE response           │  │
│  │  • Lấy sentinel  │     │  • impersonate="chrome"          │  │
│  │  • Refresh 30min │     │  • Parse → TurnResult            │  │
│  └──────────────────┘     └──────────────────────────────────┘  │
│         ▲                              │                         │
│         │ CDP (headless)               │ HTTP (curl_cffi)        │
│         ▼                              ▼                         │
│  ┌──────────────┐            ┌──────────────────┐               │
│  │  CloakBrowser │            │  chatgpt.com     │               │
│  │  --headless   │            │  /backend-api/   │               │
│  │  port 9222    │            │  (direct HTTP)   │               │
│  └──────────────┘            └──────────────────┘               │
└─────────────────────────────────────────────────────────────────┘
```

---

## File mới cần tạo

### 1. `gpt/transport/token_manager.py` — Trích xuất & refresh token từ browser

```python
class TokenManager:
    """Quản lý token/cookies từ browser headless, auto-refresh."""

    def __init__(self, page: Page):
        self.page = page
        self._cookies: dict[str, str] = {}
        self._access_token: str | None = None
        self._oai_device_id: str | None = None
        self._last_refresh: float = 0
        self._refresh_interval: float = 1800  # 30 phút

    async def extract_all(self) -> TokenBundle:
        """Lấy toàn bộ token cần thiết từ browser context."""
        # 1. Lấy cookies từ browser context (cf_clearance, session-token, etc.)
        # 2. Lấy accessToken qua page.evaluate("fetch('/api/auth/session')")
        # 3. Lấy oai-device-id từ localStorage hoặc cookies
        # 4. Gọi /backend-anon/sentinel/chat-requirements để lấy sentinel token
        # 5. Trả về TokenBundle frozen dataclass

    async def refresh_if_needed(self) -> TokenBundle:
        """Auto refresh nếu quá 30 phút."""

    async def get_sentinel_tokens(self, conversation_id: str | None) -> SentinelTokens:
        """Lấy sentinel proof token + turnstile token cho mỗi request."""
        # Chạy JS trong browser context để giải PoW
```

**Lý do tách riêng**: Token extraction cần V8 engine (browser), nhưng chỉ chạy
~1 lần/30 phút. Tách ra để browser không phải xử lý conversation traffic.

### 2. `gpt/transport/curl_transport.py` — Transport layer dùng curl_cffi

```python
class CurlCffiTransport:
    """Gửi request ChatGPT qua curl_cffi, không dùng browser."""

    def __init__(self, token_manager: TokenManager):
        self.token_manager = token_manager
        self._session: AsyncSession  # curl_cffi.requests.AsyncSession

    async def send(self, request: SendRequest) -> TurnResult:
        """POST /backend-api/f/conversation với streaming SSE."""
        # 1. Lấy token bundle từ token_manager
        # 2. Build headers: Authorization, oai-device-id, sentinel tokens
        # 3. Build payload: model, messages, thinking_effort, etc.
        # 4. POST qua curl_cffi với impersonate="chrome", stream=True
        # 5. Parse SSE stream → accumulate text → return TurnResult

    async def _build_conversation_payload(self, request: SendRequest) -> dict:
        """Tạo JSON payload giống hệt browser gửi."""
        # Dựa trên Burp capture đã có:
        # {
        #   "action": "next",
        #   "messages": [{"role": "user", "content": {"parts": [text]}}],
        #   "model": "gpt-5-5-thinking",
        #   "thinking_effort": "extended",
        #   "conversation_mode": {"kind": "primary_assistant"},
        #   "conversation_id": "...",  # nếu multi-turn
        # }

    async def _stream_sse(self, response) -> TurnResult:
        """Parse SSE stream từ curl_cffi response."""
        # Dùng SSEDecoder đã có trong gpt/reverse/stream_parser.py
```

### 3. `gpt/transport/__init__.py`

```python
from gpt.transport.token_manager import TokenManager, TokenBundle
from gpt.transport.curl_transport import CurlCffiTransport
```

---

## File cần sửa

### 4. `pyproject.toml` — Thêm dependency

```diff
 dependencies = [
     "playwright>=1.40.0",
     "cloakbrowser>=0.5.0",
+    "curl_cffi>=0.7.0",
     "pyotp>=2.9.0",
     "starlette>=0.30.0",
     "uvicorn>=0.25.0",
 ]
```

### 5. `gpt/factory.py` — Thêm `HybridWorkerFactory`

```python
class HybridWorkerFactory:
    """Worker pool dùng 1 browser shared cho token + N curl_cffi workers."""

    def __init__(self, browser_manager, *, max_workers=10, ...):
        self.token_manager: TokenManager  # 1 shared instance
        self._transports: list[CurlCffiTransport]  # pool of N workers
        # Mỗi transport chỉ tốn ~5MB RAM thay vì ~120MB/tab

    async def start(self):
        # 1. Start browser (headless) — 1 page duy nhất
        # 2. Tạo TokenManager trên page đó
        # 3. Extract initial tokens
        # 4. Tạo pool CurlCffiTransport workers

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[CurlCffiSession]:
        # Lease 1 CurlCffiTransport từ pool
        # Wrap thành CurlCffiSession (implement same interface as ChatGPTWebSession)
```

### 6. `gpt/session.py` — Thêm `CurlCffiSession` (adapter)

```python
class CurlCffiSession:
    """Session adapter wrapping CurlCffiTransport, cùng interface ChatGPTWebSession."""

    async def send(self, text, timeout_seconds=120, model=None, ...) -> TurnResult:
        # Delegate to CurlCffiTransport.send()
        # Không cần DOM interaction, model selection, hay slider clicking

    async def new_conversation(self, model=None) -> SessionInfo:
        # Chỉ reset conversation_id, không cần browser

    async def close(self):
        # Close curl_cffi session, không close browser
```

### 7. `gpt/debug.py` — Thêm `--transport` flag

```diff
 api_server_parser.add_argument("--port", ...)
 api_server_parser.add_argument("--cdp-url", ...)
+api_server_parser.add_argument(
+    "--transport",
+    choices=["browser", "hybrid", "curl"],
+    default="browser",
+    help="Transport: browser (DOM, default), hybrid (browser token + curl_cffi stream), curl (pure curl_cffi, requires manual tokens)"
+)
```

### 8. `gpt/api/server.py` — Sử dụng HybridWorkerFactory khi `--transport hybrid`

```python
# Trong create_app():
if transport == "hybrid":
    factory = HybridWorkerFactory(browser_manager, max_workers=max_workers)
else:
    factory = ChatGPTWorkerFactory(browser_manager, max_workers=max_workers)
```

---

## Flow chi tiết

### Khởi động (1 lần)

```
1. Launch CloakBrowser headless (port 9222) ← chạy ngầm, không hiện GUI
2. API Server start → tạo HybridWorkerFactory
3. Factory mở 1 page duy nhất trên browser
4. TokenManager.extract_all():
   a. Lấy cookies từ browser context (CDP)
   b. fetch('/api/auth/session') → accessToken
   c. fetch('/backend-anon/sentinel/chat-requirements') → sentinel token
   d. Giải PoW trong browser JS context
5. Tạo pool N curl_cffi transports (mỗi cái ~5MB)
6. API Server ready → nhận request từ Claude Code CLI
```

### Xử lý request (mỗi lần)

```
1. Claude Code CLI → POST /v1/messages → API Server
2. Server lease 1 CurlCffiTransport từ pool
3. Transport lấy fresh tokens từ TokenManager
   (auto refresh nếu > 30 phút)
4. Transport build payload:
   - Headers: Authorization, oai-device-id, sentinel tokens, cookies
   - Body: messages, model, thinking_effort
5. curl_cffi POST /backend-api/f/conversation (impersonate="chrome", stream=True)
6. Parse SSE stream → TurnResult
7. Server format response → Claude Code CLI
8. Release transport về pool
```

### Token refresh (mỗi 30 phút, background)

```
1. TokenManager detect token sắp hết hạn
2. Dùng browser page (vẫn đang mở ngầm) → re-fetch
3. Cập nhật token bundle → tất cả transports dùng token mới
4. Không gián đoạn request đang xử lý
```

---

## So sánh tài nguyên

| Metric | Browser hiện tại | Hybrid (plan này) |
|---|---|---|
| Browser tabs cần | 1/worker | **1 shared** (chỉ token) |
| RAM per worker | ~120MB | **~5MB** (curl_cffi session) |
| 3 workers tổng RAM | ~360MB + browser | **~135MB** (120 browser + 15 curl) |
| 10 workers tổng RAM | Không khả thi | **~170MB** (120 browser + 50 curl) |
| Latency per request | 1-3s (DOM render) | **<500ms** (direct HTTP) |
| Time to First Token | 2-5s | **<300ms** |
| GUI hiện lên | Có (nếu không headless) | **Không bao giờ** |

---

## Thứ tự triển khai

| Step | Task | Effort |
|---|---|---|
| 1 | `pip install curl_cffi` + thêm vào pyproject.toml | 5 phút |
| 2 | Tạo `gpt/transport/token_manager.py` — extract cookies+tokens từ browser | 1-2 giờ |
| 3 | Tạo `gpt/transport/curl_transport.py` — POST + SSE streaming | 1-2 giờ |
| 4 | Tạo `CurlCffiSession` adapter trong `gpt/session.py` | 30 phút |
| 5 | Tạo `HybridWorkerFactory` trong `gpt/factory.py` | 30 phút |
| 6 | Thêm `--transport hybrid` vào `gpt/debug.py` | 15 phút |
| 7 | Wire vào `gpt/api/server.py` | 30 phút |
| 8 | Test thủ công: 1 request đơn | 30 phút |
| 9 | Test fan-out: 3-5 concurrent requests | 30 phút |
| **Tổng** | | **~5-6 giờ** |

---

## Rủi ro & Mitigation

| Rủi ro | Xác suất | Mitigation |
|---|---|---|
| Token bị bind vào TLS session ID | Trung bình | `curl_cffi impersonate="chrome"` match cùng TLS fingerprint |
| Sentinel PoW cần per-request (không cache được) | Cao | Mỗi request gọi TokenManager.get_sentinel_tokens() qua browser JS |
| OpenAI thay đổi payload format | Thấp | Giữ browser fallback (`--transport browser`) luôn hoạt động |
| curl_cffi bị detect qua HTTP/2 frame order | Thấp | curl_cffi dùng libcurl-impersonate, match 100% Chrome frame order |
