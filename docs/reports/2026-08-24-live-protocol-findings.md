# Live Protocol Findings — Capture 24/08/2026

> Tổng hợp bằng chứng thật thu được ngày 24/8/2026 qua Burp proxy của user. Tài liệu này là nguồn tham chiếu cho việc nâng cấp transport trong repo (curl_transport, sentinel, gateway).

## 1. Phương pháp

- Traffic thật của user (trình duyệt ChatGPT) đi qua **Burp Suite proxy tại :8080**.
- Kết quả đọc lại qua **MCP bridge tại :9876**.
- Giới hạn quan trọng: bridge cắt message ở ngưỡng **~8KB**, do đó **body các request/response không đọc được**. Toàn bộ phân tích chỉ dựa trên:
  - method + path
  - status code
  - thứ tự thời gian của các request trong proxy history
- Hệ quả: mọi kết luận về payload (request body / response body) đều là suy luận gián tiếp từ endpoint name và thứ tự flow, chưa có bằng chứng body trực tiếp.

Quy mô quan sát: khoảng **~800 request** trong history phiên capture.

## 2. Flow login authenticated (thứ tự thật)

Chuỗi request ghi nhận được theo đúng thứ tự sau:

1. `GET /` → Cloudflare challenge trang orchestrate (`/chl_page`)
2. Turnstile: load `api.js` → `POST challenge-platform`
3. `POST /` (hoàn tất challenge, nhận clearance)
4. `GET /backend-api/user_granular_consent`
5. `POST accounts/check/v4-2023-04-27`
6. `/me` (profile)
7. `settings/user`
8. `POST /backend-api/sentinel/chat-requirements/prepare` ⭐ (khởi tạo gate sentinel)
9. `system_hints`
10. `GET /backend-api/models?iim=false&is_gizmo=false&supports_model_picker_upgrade_presets=true`
11. `tpp/models`
12. `pins`
13. Tiếp theo: danh sách `conversations…`

**Cookie xác nhận xuất hiện sau flow:**

- `__Secure-next-auth.session-token` — JWT multi-chunk (nhiều chunk cookie)
- `cf_clearance` — kết quả pass Cloudflare challenge
- `oai-did` — device id
- `oai-last-model-config={"model":"gpt-5-6-thinking","effort":"extended"}` — model config cuối cùng của user

## 3. Flow gửi 1 turn ở chế độ guest (thứ tự thật)

Chuỗi request khi submit một turn chưa đăng nhập:

1. `POST /backend-anon/conversation/init`
2. `GET checkout_pricing_config/configs/VN` (config vùng VN)
3. `settings/voices`
4. `POST /backend-anon/sentinel/chat-requirements/finalize` ⭐ (gate sentinel bước finalize)
5. `POST /backend-anon/f/conversation/prepare` ⭐ (submit turn qua prepare)
6. WebSocket topic `"conversations"`:
   - event `conversation-created`
   - event `conversation-turn-complete`
   - ví dụ `conversation_id`: `6a8bc6ea-81f0-83ec-a1b5-5a621b74ed94`

Lưu ý: anonymous dùng prefix `/backend-anon/`, và gate sentinel ở guest là **finalize** (không phải prepare như authenticated).

## 4. Phát hiện kiến trúc

- Trong ~800 request capture, **KHÔNG có** `POST /backend-api/f/conversation` thường (endpoint submit legacy chờ SSE).
- Submit turn thực tế đi qua **`/f/conversation/prepare`** — mô hình "conduit": client chuẩn bị turn qua prepare, stream không trả về trên response đó.
- Stream chuyển sang **WebSocket**: topic `"conversations"` phát `conversation-created` và `conversation-turn-complete`. Điều này khớp với flag `resume_with_websockets` thấy trong `server_ste_metadata` của artifact 15/8.
- **Điểm bỏ ngỏ**: text delta không thấy trên WS history của Burp — khả năng delta đi trên một connection realtime khác (chưa xác định). Cần kiểm chứng thêm trước khi chốt kiến trúc stream đầy đủ.

## 5. Hệ quả cho repo

- `curl_transport.py` hiện POST thẳng `/f/conversation` và chờ SSE — theo bằng chứng 24/8, đường này có nguy cơ **hết hiệu lực** với client thật (server đã chuyển sang prepare + WS).
- Sentinel cần nâng cấp để hỗ trợ cả hai bước **prepare → finalize**:
  - Authenticated: `POST /backend-api/sentinel/chat-requirements/prepare`
  - Guest: `POST /backend-anon/sentinel/chat-requirements/finalize`
- Mức độ tin cậy của gate ≥2 evidence đã đạt:
  1. Artifact 15/8 (`server_ste_metadata`, flag `resume_with_websockets`)
  2. Capture 24/8 (flow thật qua Burp)

## 6. Việc còn bỏ ngỏ

1. **Body chuẩn của prepare/finalize** — bị bridge che (giới hạn ~8KB cắt message), chưa có cấu trúc payload chính xác.
2. **Delta text đi kênh nào** — không thấy trên WS history của Burp; cần trace connection realtime riêng nếu có.
3. **Đường SSE legacy còn sống không** — probe song song đang được kiểm tra; kết quả sẽ bổ sung vào tài liệu này.

---

## Nguồn

- Burp Suite proxy history qua MCP bridge (:9876, proxy :8080), ngày 2026-08-24.
- Artifact reverse trước đó: `~/.local/share/bqa/webchat-reverse/run-20260815*`.
