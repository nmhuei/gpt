# ACTIVE_ISSUES — Danh sách lỗi cần sửa & Quy tắc duy trì

> **Quy tắc bắt buộc cho toàn bộ Agent:**
> 1. **Ghi nhận ngay**: Khi phát hiện bất kỳ lỗi, bug, điểm nghẽn hoặc hành vi bất thường mới, agent PHẢI bổ sung ngay mục đó vào phần **`## 1. Active Issues (Cần sửa)`**.
> 2. **Xóa / Di dời khi đã fix**: Khi một lỗi được sửa xong và đã **kiểm chứng thực tế (verified with real test)**, agent PHẢI xóa mục đó khỏi danh sách Active hoặc chuyển xuống bảng **`## 2. Resolved Archive (Đã sửa & Kiểm chứng)`** để giữ file luôn tinh gọn.

---

## 1. Active Issues (Cần sửa)

### 📌 ISSUE-002: Lỗi thụt lề & trích dẫn khi Agent xuất mã nguồn nhiều dòng qua Bash Heredoc
- **Mức độ:** Trung bình.
- **Hiện tượng:** Khi `gpt` agent tạo file script bằng cú pháp `cat > file.py << 'PY'`, một số dòng code Python bị mất thụt lề (indentation) hoặc bị lỗi biến shell, dẫn tới `IndentationError` khiến agent phải mất thêm 2-3 round tự sửa lại.
- **Nguyên nhân kỹ thuật:** Agent ưu tiên dùng lệnh Bash thay vì dùng tool chuyên dụng `ApplyPatch` hoặc ghi file qua base64/write_text an toàn.
- **Hướng khắc phục đề xuất:**
  - Tinh chỉnh `DEFAULT_SYSTEM_PROMPT` trong `gpt/agent/runner.py` để hướng dẫn agent ưu tiên tạo file qua base64 `echo <base64> | base64 -d > file` hoặc dùng `ApplyPatch`.

---

### 📌 ISSUE-003: Xung đột phiên đồng thời (409 Conflict) trên Gateway khi nhiều agent gọi cùng lúc
- **Mức độ:** Trung bình.
- **Hiện tượng:** Khi 2 agent gọi `POST /v1/messages` gần như đồng thời lên cùng một worker profile, gateway trả về `409 Conflict`.
- **Nguyên nhân kỹ thuật:** `BrowserWorker` của CloakBrowser lock tab theo phiên, chưa có hàng đợi xếp hàng thông minh (queue retry backoff) ở tầng client.
- **Hướng khắc phục đề xuất:**
  - Bổ sung logic auto-retry có jitter backoff (0.5s - 2s) trong `GatewayClient` (`gpt/agent/client.py`) khi nhận HTTP 409.

---

### 📌 ISSUE-004: Thiếu thư viện toán học phổ biến (`sympy`) khiến Agent import lỗi
- **Mức độ:** Thấp.
- **Hiện tượng:** Agent thường viết `from sympy import isprime` và bị `ModuleNotFoundError: No module named 'sympy'`, trong khi môi trường hiện tại có `Crypto.Util.number.isPrime`.
- **Hướng khắc phục đề xuất:**
  - Thêm `sympy` vào `pyproject.toml` / cài đặt vào `.venv` để agent không bị đứt quãng khi import toán học.

---

## 2. Resolved Archive (Đã sửa & Kiểm chứng)

| Mã lỗi | Mô tả | File sửa đổi | Ngày fix | Trạng thái kiểm chứng |
| :--- | :--- | :--- | :--- | :--- |
| **FIX-001** | Lỗi 503 Model Selection (`Model selection click did not read back as active: gpt-5-6-thinking`) | `gpt/drivers/ui.py` | 2026-09-02 | **Đã verify:** `slug_map` và `_model_matches` hỗ trợ 100% `gpt-5-6-thinking`, trả `200 OK`. |
| **FIX-002** | Lỗi đường dẫn Launcher `/home/light/.local/bin/gpt` không tìm thấy virtualenv | `/home/light/.local/bin/gpt` | 2026-09-02 | **Đã verify:** `gpt doctor` và `gpt status` chạy từ mọi thư mục đều pass. |
| **FIX-003** | Khắc phục triệt để lỗi Fallback về `5.5-mini` / Lỗi 403 REST | `webgpt-gateway.service`, `gpt/drivers/ui.py` | 2026-09-02 | **Đã verify:** Chuyển sang `--transport browser` kết hợp Power Slider High auto-navigation (3 of 3). 100% GPT-5.6 Sol Thinking. |
| **FIX-004** | Giữ nguyên Session ID qua các round (`x-webgpt-session-id`) | `gpt/agent/client.py` | 2026-09-02 | **Đã verify:** Agent chạy liên tục nhiều turn không bị mất context. |
| **FIX-005** | Lỗi sập `Reasoning effort is not available in the current UI: high` trên gateway | `gpt/gateway/runtime.py`, `gpt/drivers/ui.py` | 2026-09-02 | **Đã verify:** Thêm try/except fallback khi UI không có effort dropdown; `curl /v1/chat/completions` và `gpt` CLI test 1-turn trả lời `"OK"` thành công. |
| **FIX-006** | Tràn tiến trình con mồ côi (Orphan Process Leak) & Tối ưu hóa 50% RAM Gateway | `scripts/ctf_spawn_session.py`, `gpt/transport/browser.py`, `webgpt-gateway.service` | 2026-09-02 | **Đã verify:** Thêm `os.killpg` SIGKILL escalation khi timeout; giảm worker từ 2 xuống 1; nạp cờ `--js-flags=--max-old-space-size=512`; RAM gateway giảm từ 874MB+ xuống 422MB (giảm hơn 50%). |
