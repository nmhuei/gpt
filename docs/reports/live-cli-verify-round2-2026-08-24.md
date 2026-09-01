# Live CLI Verification — Round 2 (2026-08-24)

Chạy lại thang verify theo đúng phương pháp round 1 (`live-cli-verify-2026-08-24.md`) để đo tiến bộ
sau loạt fix: usage tokens ước lượng, refusal mềm + raise sớm, stream đóng sạch, page crash → 503,
output sạch, và **claim-based correction đa ngữ**.

- Repo: `/home/light/GitHub/gpt`
- Gateway: `http://127.0.0.1:18000` (unit `webgpt-gateway.service`) — healthy ngay khi bắt đầu,
  healthy ở cuối (browser ready, workers live=5)
- CLI: `~/.local/bin/claude` 2.1.241, trỏ thẳng `ANTHROPIC_BASE_URL=http://127.0.0.1:18000`
  (round 1 phải qua proxy :18001 mới pass T1 — lần này test trực tiếp)
- Thư mục test: `/tmp/cc-live-test2` (task.md fizzbuzz y hệt round 1)
- Turn CLI đã dùng: **5/8** (T1 ×1, T2 ×2, T3 ×2). Bonus T2b bỏ qua (T2 đỏ).

## Bảng so sánh từng cặp

| Mức | Round 1 | Round 2 | Thay đổi |
|---|---|---|---|
| T1 kết nối & streaming | PASS chỉ ở lần 3, qua proxy; trực tiếp thì 500 Target crashed rồi hang-có-200 | **PASS lần 1, trực tiếp :18000**, 45s, RC=0 | ✅ Tiến bộ rõ: hết 500 Target crashed, hết hang sau 200. Vẫn còn tiền tố `Thinking` trong stdout |
| T2 tool use 1 bước | **PASS** 116s (`TOOLCHAIN_1787553966` chạy thật) | **FAIL ×2**, 76s / 79s, cùng lỗi: `API Error: 500 Tool correction budget exhausted (TOOL_REFUSAL): model denied access to controller-provided tools` | ❌ Thoái bộ chức năng: vòng correction mới phát hiện model từ chối tool, bắn correction, nhưng model vẫn chặn → cạn budget (default `WEBGPT_MAX_CORRECTIONS=2`, runtime.py:784) → raise 500 retryable thay vì trả kết quả |
| T3 đa bước tự chủ | FAIL ×2, ~25s mỗi lần, **prose giả "đã tạo và chạy"** tới thẳng user | FAIL ×2 (106s / 82s) nhưng **khác chất**: lần 1 `500 MALFORMED_TOOL` ("Tool block did not contain any valid tool calls"), lần 2 `500 FALSE_COMPLETION` ("task requires a controller tool but model returned only prose") | ⚠️ Tiến bộ một nửa: claim-based correction **có bắn thật** (thời gian gấp 3–4 lần round 1 = nhiều turn nội bộ), và fail giờ là **lỗi 500 sạch, retryable** chứ không còn prose lừa. Nhưng model vẫn không phát được tool call hợp lệ → vẫn không tạo ra `fizzbuzz.py`/`output.txt` |

## Chi tiết

### Gateway journal (mỗi attempt CLI = 2 HTTP POST)
Mẫu lặp lại mọi mức: POST đầu trả 200, vài chục giây sau client cùng port POST lại và nhận 500
(ví dụ T3a: 14:56:45 → 200, 14:58:30 → 500). Khớp mô hình: correction loop chạy nội bộ trong
request đầu, khi cạn budget trả error event làm SDK tự retry 1 lần rồi surface 500 cho CLI.

### Filesystem sau T3 ×2
`/tmp/cc-live-test2` chỉ còn `task.md` — **không file nào được tạo** (như round 1).

### Chẩn đoán tầng còn thiếu (T3/T2 vẫn đỏ)
1. Correction prompt (`WEBGPT CONTROLLER CORRECTION`, runtime.py:673/703) bắn tối đa 2 lần nhưng
   không đủ ép model web phát tool call parse được — budget quá ngắn so với mức cứng đầu của model.
2. T2 là regression đáng lo nhất: chính case từng PASS round 1 nay bị tầng correction chặn thành
   500 — nghĩa là detection nhạy hơn nhưng **không có đường thoát thành công**.
3. Vẫn còn nhiễm output: tiền tố `Thinking` trên text delta (T1 stdout: `ThinkingAY OKGATEWAY OK`).

## Verdict tổng thể

CLI qua gateway hiện đạt mức **"chat/streaming tin cậy + fail-honest"**, chưa đạt lại mức
tool-use của round 1:

- T1 từ "flaky cần 3 lần + proxy" lên **pass trực tiếp lần 1** — fix stream/page-crash hiệu quả.
- Output không còn prose fake-success tới tay user — mục tiêu chống-lừa của đợt fix đã đạt.
- Đổi lại T2 (từng PASS) giờ fail 100% vì correction budget cạn trước khi model chịu dùng tool;
  T3 vẫn không tạo được artifact nào.
- Kết luận: an toàn hơn (không còn nói dối), yếu hơn (chưa làm được việc thật).

## Việc còn thiếu để xanh T2/T3

1. Nâng/tinh chỉnh `WEBGPT_MAX_CORRECTIONS` hoặc làm correction prompt mạnh hơn (ví dụ kèm ví dụ
   tool-call đúng format) — hiện 2 lần không đủ.
2. Xử lý riêng case TOOL_REFUSAL: nếu model cứ từ chối, cân nhắc fallback thực thi tool server-side
   thay vì raise.
3. Sạch nốt tiền tố `Thinking` trong text delta.
4. Điều tra mẫu 2-POST/attempt (SDK retry sau error event) để tránh nhân đôi chi phí mỗi turn.

## Phụ lục — artifact raw

- `/tmp/cc-live-test2/t1.stdout` (PASS 45s)
- `/tmp/cc-live-test2/t2.stdout`, `t2b.stdout` (500 TOOL_REFUSAL ×2)
- `/tmp/cc-live-test2/t3.stdout` (500 MALFORMED_TOOL), `t3b.stdout` (500 FALSE_COMPLETION)
