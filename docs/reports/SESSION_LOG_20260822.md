# Session Log — 2026-08-22

## Tổng Quan

Phiên làm việc tập trung vào việc reverse-engineer giao thức ChatGPT Web, xây dựng API Gateway
cho Claude Code CLI sử dụng GPT-5.5 Thinking (High Effort), và debug/tối ưu hoá toàn bộ flow.

---

## 1. Reverse-Engineering ChatGPT Web Protocol (Burp Suite)

### 1.1 Phát hiện Endpoint Đổi Model và Effort

Bằng cách bắt gói tin qua Burp Suite / Playwright request interception, đã xác định chính xác:

- **Gửi tin nhắn**: `POST /backend-api/f/conversation`
  - `model`: `"gpt-5-5-thinking"`
  - `thinking_effort`: `"extended"` (= High), `"standard"` (= Medium)
  - `conversation_mode`: `{"kind": "primary_assistant"}`

- **Lưu cấu hình model mặc định**: `PATCH /backend-api/settings/user_last_used_model_config`
  - Query params: `?model_slug=gpt-5-5-thinking&thinking_effort=extended`
  - **Kết quả**: OpenAI chặn model Legacy (5.5) không cho đặt làm default server-side
    - `gpt-5-6-thinking` + `extended` → `{"success": true}`
    - `gpt-5-5-thinking` + `extended` → `{"success": false, "Failed to meet eligibility criteria..."}`

- **Lấy danh sách model**: `GET /backend-api/models`
  - Trả về categories, versions, intelligence_presets (Instant/Medium/High)
  - Mỗi preset chứa `model_slug`, `thinking_effort`, `lane`

- **Cài đặt người dùng**: `GET /backend-api/settings/user`
  - Chứa `last_used_model_config`, `model_sticky_for_new_chats`, `available_options`

### 1.2 Phát hiện UI DOM Structure (Radix Menu)

- **Composer Pill**: `button.__composer-pill` (hiển thị "5.5 Medium" hoặc "5.5 High")
- **Menu mở ra**: `[role="menu"]` chứa:
  - `[role="menuitem"][aria-label="Power"]` — **SliderControl** (thanh trượt)
  - `[role="menuitem"]:has-text("Advanced")` — Toggle hiển thị nâng cao
  - `[role="menuitem"]:has-text("Model")` — Chọn model (5.5, 5.6 Sol, o3)
  - `[role="menuitem"]:has-text("Effort")` — Chọn effort (Instant, Medium, High)
- **SliderControl**: Thanh trượt nằm ngang, click tại vị trí:
  - **15%** width → Instant
  - **50%** width → Medium
  - **88%** width → High

---

## 2. Tự Động Hoá Chọn Model 5.5 High

### 2.1 Vấn Đề

Mỗi khi mở phiên chat mới, mặc dù URL có `?model=gpt-5-5-thinking`, giao diện vẫn
mặc định về **Medium** effort. Phải chuyển sang **High** trước mỗi prompt.

### 2.2 Giải Pháp Đã Triển Khai (3 lớp trong `gpt/drivers/ui.py`)

**Lớp 1** — URL Navigation (`select_model()`): Điều hướng `?model=gpt-5-5-thinking`,
nếu target chứa "high" thì gọi `select_reasoning_effort("high")`.

**Lớp 2** — Slider Auto-Click (`select_reasoning_effort()`):
Click Pill → tìm SliderControl `[aria-label="Power"]` → click tại 88% width → High.
Fallback: DOM dispatch mousedown+mouseup+click. Fallback cuối: locator click từng option.

**Lớp 3** — Pre-Send Guard (`send()`): Trước khi gõ prompt, kiểm tra pill text.
Nếu thấy "5.5" nhưng không có "high" → tự động gọi `select_reasoning_effort("high")`.

### 2.3 Kết Quả Xác Minh

- Pill trước send: `'5.5\nMedium'` → Pill sau send: `'5.5\nHigh'`
- Payload Burp: `{"model": "gpt-5-5-thinking", "thinking_effort": "extended"}`
- Screenshot: `~/Downloads/webgpt/real_view_auto_high_success.png`

---

## 3. API Gateway Server

### 3.1 Khởi Chạy

```bash
python3 -m gpt.debug api-server \
  --port 8000 --allow-authenticated \
  --model-aliases ~/Downloads/webgpt/model_aliases.json \
  --cdp-url http://127.0.0.1:9222 --prewarm
```

### 3.2 Endpoints

| Endpoint | Giao thức |
|---|---|
| `POST /v1/messages` | Anthropic Messages API |
| `POST /v1/chat/completions` | OpenAI Chat Completions API |
| `GET /health` | Health check |
| `GET /models` | Model listing |

### 3.3 Multi-Worker

Sửa `gpt/debug.py`: cho phép `--max-workers > 1` khi `--allow-authenticated`.
`ChatGPTWorkerFactory` quản lý pool worker (mỗi worker = 1 tab riêng biệt).

---

## 4. Claude Code CLI Integration

```bash
ANTHROPIC_BASE_URL="http://127.0.0.1:8000" ANTHROPIC_API_KEY="dummy" \
claude -p "prompt" --output-format text
```

- Fibonacci test: HTTP 200, ~44s response time
- CTF SSSH RSA: Phân tích đầy đủ, 0 cyberflag / 0 refusal
- Fan-out subagents test: Bị ConnectionRefused do gateway tắt giữa chừng

---

## 5. Phân Tích `curl_cffi` vs Browser Engine

| Yếu tố | `curl_cffi` | Browser Engine (Headless) |
|---|---|---|
| TLS/JA3 Fingerprint | ✅ Giả lập tốt | ✅ Native |
| JavaScript Engine (V8) | ❌ Không có | ✅ Có |
| Cloudflare Turnstile | ❌ Không giải | ✅ Tự động |
| OpenAI Sentinel PoW | ❌ Phải reverse | ✅ Tự động |
| RAM tiêu thụ | ~30MB | ~80-120MB |

**Kết luận**: `curl_cffi` chỉ giải quyết lớp TLS fingerprint, không có JS engine
nên không vượt được Cloudflare Turnstile VM và OpenAI Sentinel PoW.

---

## 6. Các File Đã Sửa

- **`gpt/drivers/ui.py`**: `select_model()`, `select_reasoning_effort()`, `send()` — thêm 3 lớp auto High
- **`gpt/debug.py`**: `cmd_api_server()` — cho phép multi-worker khi authenticated

---

## 7. Test Suite

- **196/196 pytest passed** (6.3s)
- **Ruff linter**: All checks passed

---

## 8. Trạng Thái

### Đã Hoàn Thành
- [x] Reverse-engineer ChatGPT model/effort switching protocol
- [x] Tự động hoá 5.5 High selection (3 lớp bảo vệ)
- [x] API Gateway hoạt động với Claude Code CLI
- [x] Multi-worker support cho authenticated sessions
- [x] Pre-send effort guard
- [x] 196/196 tests pass, lint clean

### Chưa Hoàn Thành
- [ ] Test fan-out subagents thực tế (gateway bị tắt giữa chừng)
- [ ] Đo benchmark số agent tối đa song song
- [ ] Tối ưu Headless Non-DOM Protocol
- [ ] Cân nhắc hybrid curl_cffi + browser (browser lấy token, curl_cffi stream)
