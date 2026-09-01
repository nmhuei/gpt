# Live CLI Verification — Claude Code qua webgpt gateway (2026-08-24)

Mục tiêu: verify thực chất câu hỏi lớn nhất — **Claude Code CLI chạy qua gateway có làm được việc thật không** (thao tác máy, chạy tool bash, vòng lặp tool-use nhiều bước).

- Repo: `/home/light/GitHub/gpt`
- Gateway: `http://127.0.0.1:18000` (systemd user unit `webgpt-gateway.service`, PID 1253)
- CLI: `~/.local/bin/claude`, phiên bản claude-cli/2.1.241, model `claude-3-5-sonnet`
- Thư mục test: `/tmp/cc-live-test` (task.md = fizzbuzz 1..15 → `fizzbuzz.py` + `output.txt`)
- Tổng số turn CLI đã dùng: **6** (T1 ×2 direct + 1 diagnostic qua proxy, T2 ×1, T3 ×2)

## Bảng kết quả

| Mức | Mô tả | Kết quả | Thời gian | Ghi chú |
|---|---|---|---|---|
| T1 | Kết nối & streaming ("GATEWAY OK") | **PASS** (lần thử 3) | 4s / >180s / 14s | Lần 1: HTTP 500 browser crash. Lần 2: hang tới timeout dù gateway log 200. Lần 3 (qua logging proxy :18001): PASS 14s |
| T2 | Tool use 1 bước (Bash thật) | **PASS** | 116s | stdout chứa `TOOLCHAIN_1787553966` khớp đúng timestamp tạo lệnh — tool chạy THẬT trên máy |
| T3 | Đa bước tự chủ (fizzbuzz task) | **FAIL** (×2/2) | 25s / 26s | Model trả prose tự nhận "đã tạo và chạy script" nhưng **không file nào tồn tại**; kèm câu hỏi ngược "Bạn muốn tiếp tục...?" |
| T4 | Số liệu & stderr | Hoàn thành | — | Xem chi tiết dưới |

## Chi tiết từng mức

### T1 — kết nối & streaming
- Lần 1 (`t1.stdout`, 4s): `API Error: 500 Locator.count: Target crashed`. Gateway log: exception trong
  `gpt/transport/session.py:484 reconcile → gpt/drivers/ui.py:720 history()` — Playwright `Locator.count: Target crashed`
  khi đọc lịch sử UI để reconcile pending request → `_reconcile_pending_request` (server.py:1499) trả 500.
  **Tầng vỡ: transport/browser session (page crash), không phải API layer.**
- Lần 2 (`t1b.*`, timeout 180s, RC=124, 0 byte stdout/stderr): gateway log ghi 2 POST `/v1/messages?beta=true` trả **200 OK**
  (13:39:48, 13:40:29) nhưng CLI không nhận/hiển thị gì rồi treo tới khi bị kill. Response stream có vẻ không đóng/không parse được ở phía SDK.
- Diagnostic (không tính retry): curl trực tiếp stream:true → SSE hoàn chỉnh (`message_start` … `message_stop`), 45s,
  nội dung đúng nhưng có rác tiền tố `"Thinking"` trong text delta. `POST /v1/messages/count_tokens` → 200 `{"input_tokens":22}`.
- Lần 3 qua proxy Python buffer-to-full-response (:18001 → :18000): **PASS 14s**, stdout `ThinkingAY OKGATEWAY OK`
  (chứa chuỗi yêu cầu). Payload CLI: 149KB, system 27.6K chars, 28 tools, beta headers `claude-code-20250219,...`.
  ⇒ Gateway chấp nhận đủ payload đầy đủ của Claude Code.

### T2 — tool use 1 bước
- 1 turn duy nhất, 116s, stdout: `ThinkingTOOLCHAIN_1787553966`.
- Timestamp khớp 100% với giá trị sinh trước khi gọi CLI ⇒ vòng transpile tool call → thực thi shell thật → feed kết quả lại hoạt động.
- Gateway log window cho thấy nhiều POST liên tiếp (13:46:07 → 13:47:52) ⇒ loop tool-use ≥1 lần chạy đúng.
- Lưu ý: chậm (~2 phút cho 1 tool round-trip) và text delta dính tiền tố "Thinking".

### T3 — đa bước tự chủ
- Cả 2 lần đều ~25–26s, stdout là prose thuần tuyên bố thành công giả:
  - Lần 1 (487 bytes): mô tả nội dung output.txt + câu hỏi ngược `"Bạn muốn tiếp tục theo hướng Python cơ bản hay CTF/script automation?"`; text bị **nhân đôi** (cùng nội dung lặp 2 lần).
  - Lần 2 (277 bytes): tương tự, `"Đã tạo và chạy script fizzbuzz.py..."` — kiểm tra filesystem: **không có fizzbuzz.py, không có output.txt**.
- Gateway log: mỗi lần chỉ 1 cặp POST nhanh (~26s) — **không có round-trip tool nào xảy ra**. Model trả lời dạng chat thay vì phát tool call;
  gateway không buộc format tool-call nên CLI nhận end_turn với prose.
- **Tầng vỡ: tầng orchestration/prompt-side — model web trả prose giả lập việc dùng tool thay vì tool call thật; gateway không phát hiện/cảnh báo "prose claiming success".**

## Vấn đề nền tảng quan sát được

1. **Browser session không ổn định**: Playwright page crash giữa chừng (`Target crashed`) làm 500; health vẫn báo `browser:"ready"` (health check không phản ánh trạng thái page thật).
2. **Hang sau 200**: có request gateway trả 200 nhưng CLI treo vô hạn — khả năng stream không kết thúc đúng cách trên một số đường reconcile.
3. **Nội dung nhiễm bẩn**: tiền tố `"Thinking"`, text trùng lặp (nghi ngờ reconcile/history ghép cả draft lẫn final message).
4. **Chậm**: đơn giản nhất 14s, trung bình 45–116s cho 1 request nhỏ.
5. **Multi-step autonomy thất bại**: chỉ work khi prompt ép "run this exact command with your Bash tool"; với nhiệm vụ tự chủ, model thoát ra bằng prose.

## Đánh giá tổng thể

CLI qua gateway này đạt mức **"chat + tool-use đơn mệnh lệnh"**, KHÔNG đạt mức agent tự chủ:

- So với API Anthropic thật: T1/T2 cho thấy pipeline SSE + tool-transpile + thực thi bash thật về cơ bản hoạt động (parity một phần).
- Nhưng độ tin cậy thấp (T1 cần 3 lần mới pass; crash/hang ngẫu nhiên), độ trễ cao gấp hàng chục lần, và quan trọng nhất:
  **T3 fail 100%** vì model web trả prose thay vì tool call khi không bị ép cứng — vòng lặp đa bước tự chủ chưa dùng được cho việc thật.
- Kết luận: dùng được cho tác vụ one-shot có mệnh lệnh tool rõ ràng; chưa dùng được làm Claude Code hằng ngày (đa bước, tự chủ).

## Phụ lục — artifact raw

- `/tmp/cc-live-test/t1.stdout`, `t1.stderr` (500 Target crashed)
- `/tmp/cc-live-test/t1b.stdout`, `t1b.stderr` (hang, 0 bytes)
- `/tmp/cc-live-test/t1c.stdout` (pass qua proxy) + `/tmp/cc-live-test/cli_req.log` (full request/response capture)
- `/tmp/cc-live-test/t2.stdout` (TOOLCHAIN_1787553966), `t2.ts`
- `/tmp/cc-live-test/t3.stdout`, `t3b.stdout` (prose fake-success ×2)
- `/tmp/cc-live-test/curl1.out` (SSE raw hợp lệ), `ct.out` (count_tokens)
