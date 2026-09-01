# Prompt-lab: handshake variant A/B cho chế độ soft-protocol (2026-08-24)

**Kết quả: KHÔNG ĐỦ DỮ LIỆU LIVE để xếp hạng 5 biến thể.** 0/5 variant đo được trọn vẹn —
gateway :18000 chết vì hạ tầng trong suốt phiên (AuthRequired → rate-limit usage cap).
Báo cáo này tổng hợp: (1) phát hiện kỹ thuật quan trọng thu được, (2) khuyến nghị tạm thời
GIỮ NGUYÊN baseline V1 cho `_SOFT_HANDSHAKE_TEXT`, (3) kế hoạch chạy lại chính xác.

## Mục tiêu

Tìm câu giao kèo (handshake) đầu-hội-thoại khiến model ChatGPT Web hợp tác đa bước tốt nhất dưới
`WEBGPT_TOOL_PROTOCOL=soft`, đo bằng số liệu thay vì cảm tính. Text thắng sẽ thay `_SOFT_HANDSHAKE_TEXT`
tại `gpt/gateway/runtime.py:853`.

## Phương pháp (đã cố định, tái sử dụng được)

- Endpoint `POST http://127.0.0.1:18000/v1/chat/completions`, key `sk-webgpt-local`,
  `model=chatgpt-web`, non-stream. Mỗi POST = 1 gateway turn = conversation fresh (stateless replay,
  trace ghi `conversation_id: null`). Cùng mô hình với soft-framing probe sáng nay.
- **Phải khai báo đúng 1 tool Bash** (schema tối giản). Xem "Phát hiện #1" — request không-tool
  hiện NÁT ở tầng parse.
- Task đồng nhất mọi variant: "Create a file named a.txt in the current directory containing exactly:
  x — then read it back and show me its content." (cần ≥2 vòng cmd). Variant text nối vào cuối
  message user đầu tiên; follow-up trung lập "Here is the command output: … Continue with the next
  step, or tell me the task is complete." Lệnh `<cmd>` được chạy thật trong `/tmp/promptlab/<V>/`.
- Ngân sách 12 turn ngữ nghĩa; mỗi variant ≤3 turn.

### Biến thể

| ID | Nội dung (nguyên văn phần khác biệt) |
|----|--------------------------------------|
| V1 | baseline hiện tại: "When my setup needs a shell action, reply with just <cmd>the exact shell command</cmd> and nothing else — I'll run it and paste the output back. If it's a question, just answer normally." |
| V2 | V1 + "\n\nWork step by step: run one command per reply until the whole task is done." |
| V3 | V2 + " Never say a file is created unless you saw the command that created it succeed." |
| V4 | "You are my terminal operator.\n\n" + V3 |
| V5 | "Reply with only <cmd>the exact shell command</cmd> each turn; I'll paste the output back." (14 từ) |

Lưu ý thiết kế: khi client khai báo tools + soft mode, gateway TỰ động thêm V1 vào cuối turn đầu
(`runtime.py:1417`, `_soft_handshake_needed`). V2–V4 là mở rộng của V1 nên không bị nhiễu;
**V5 là replacement nên bị nhiễm V1 khi đo qua API công khai** — muốn đo sạch V5 phải thay
hằng số rồi restart gateway, hoặc chấp nhận đo dạng "V1+V5-suffix".

### Chấm điểm composite (0–100): hoàn thành task 40 (file 'x' +20, có lệnh đọc lại +10, không fake-success +10) · ít prose lạc đề 30 (−8/lần hỏi ngược, −10 fake-success, trừ tối đa 10 nếu verbose) · ít turn lãng phí 30 (−10/cmd thừa, −10 hỏi ngược).

## Bảng dữ liệu đo được

| Variant | Turn HTTP-200 | <cmd>/tool-call emit | Hỏi lại | Fake-success | Turn để xong | Composite |
|---------|---------------|----------------------|---------|--------------|--------------|-----------|
| V1 | **1/2** (turn 2 mất do 502) | ≥1 (bằng chứng gián tiếp) | n/a | n/a | n/a | **không chấm được** |
| V2 | 0 | — | — | — | — | chưa đo |
| V3 | 0 | — | — | — | — | chưa đo |
| V4 | 0 | — | — | — | — | chưa đo |
| V5 | 0 | — | — | — | — | chưa đo |

Chi tiết các lần thử (toàn bộ thất bại TRƯỚC khi tới model hoặc mất response):

| # | Thời điểm | Kết quả | Nguyên nhân gốc |
|---|-----------|---------|-----------------|
| 19:11 | POST /v1/chat/completions | 503 browser_disconnected ×n | session web hết hạn (`AuthRequired` trong trace) |
| 19:38 | retry sau restart | 503 ×4 POST | failover churn đốt quota → `RateLimited` |
| ~19:44 | smoke probe | **200 "ready"** | cửa sổ limit vừa hé |
| 19:47 | V1-T1 | **502 malformed_model_tool_call "Unknown tool requested: Bash"** | model ĐÃ emit Bash call (handshake hoạt động!) nhưng harness thời điểm đó chưa khai báo tools → parser từ chối; response gốc không lưu dump |
| 19:48→ | V1-T2, probes | 503/RateLimited đến hết phiên | usage cap vẫn đóng |

## Phát hiện kỹ thuật (giá trị độc lập với A/B)

1. **Regression kênh mềm khi không khai báo tools:** từ lúc soft-protocol được merge vào production,
   request KHÔNG tools mà model trả `<cmd>` sẽ bị `ToolTranspiler` parse thành tool call tên Bash rồi
   502 `Unknown tool requested: Bash` (`gpt/utils/toolcall.py:1022`, đường soft-parse ở
   `toolcall.py:1110-1128`; caller truyền `allowed_tools=set(validate_tools(tools))` — rỗng nhưng
   KHÔNG phải None → mọi call đều chối). Kênh "không-tools" từng sống sót 4/4 trong soft-framing
   probe giờ không dùng được nữa. Mọi probe soft về sau PHẢI khai báo tool Bash.
2. **Soft mode giữ prompt sạch:** với tools + soft, `render_messages` bỏ hẳn bootstrap +
   loud controller block (`promptcompat.py:150-175`), chỉ append handshake hội thoại —
   kiến trúc đúng như thiết kế.
3. **Failover churn tự đốt quota:** mỗi lỗi tạm thời tạo conversation mới trên web; chuỗi
   AuthRequired → failover lặp đã đẩy tài khoản vào usage-cap trong vài phút. Khuyến nghị:
   gateway nên backoff cấp account khi thấy `RateLimited`, không failover ngay.
4. Hạ tầng phục hồi thủ công trong phiên: warm-browser qua Cloudflare challenge + auto-login
   TOTP bằng saved credentials (script `/tmp/promptlab/warmcf.py`, `manualogin2.py`) — profile
   `personal` xác thực lại OK lúc 19:34, gateway healthy, smoke 200 lúc 19:44.

## Khuyến nghị tạm thời (chờ dữ liệu A/B đầy đủ)

- **GIỮ NGUYÊN V1** trong `_SOFT_HANDSHAKE_TEXT`. Đây là variant duy nhất có tín hiệu tích cực
  trong phiên (model emit Bash tool call ngay turn đầu dù chỉ đo được gián tiếp), và là cấu hình
  đã chứng minh `<cmd>` chuẩn xác 2/2 trong soft-framing probe + R5 production. Không đủ cơ sở
  thay bằng V2–V5.
- Không có dữ liệu nào ủng hộ thêm câu chống fake-success (V3) hay vai operator (V4) — hai giả
  định còn nguyên và cần đo.

## Khoảng trống & kế hoạch xếp lịch

- **Thiếu toàn bộ:** số vòng cmd liên tiếp, hỏi-lại-giữa-chừng, fake-success, tổng turn, composite
  cho cả 5 variant (trừ 1 tín hiệu T1 của V1).
- **Ngân sách còn:** 12 − 2 = 10 turn ngữ nghĩa theo sổ theo dõi `/tmp/promptlab/state.json`
  (2 turn đã trừ là những generation bị mất/502). Cần đúng 10 cho 5 variant × 2 turn
  (T1 create → T2 read+confirm-rút gọn); nếu cho phép 3 turn/variant thì cần nâng ngân sách lên 15.
- **Xếp lịch đề xuất:** chạy khi gateway rảnh và cửa sổ usage mới — (a) kiểm tra
  `trace.jsonl` không còn `RateLimited` trong 15 phút gần nhất; (b) 1 smoke probe "ready";
  (c) chạy liền 5 variant bằng `python3 /tmp/promptlab/lab.py <V1..V5> 2` (mỗi turn cách nhau 25s,
  script tự truy ngân sách 12); (d) `score.py` in bảng composite; (e) điền lại bảng trên.
  Tránh khung giờ có CLI khác đang dùng gateway (watchdog log / active_sessions cao).
- Harness + scorer đã sẵn sàng: `/tmp/promptlab/lab.py`, `/tmp/promptlab/score.py`,
  state `/tmp/promptlab/state.json`.

## Files

- Báo cáo: `docs/reports/prompt-lab-handshake-2026-08-24.md`
- Harness/scorer/state (tạm): `/tmp/promptlab/{lab.py,score.py,state.json,recovery.log}`
- Injection site handshake (production, KHÔNG sửa): `gpt/gateway/runtime.py:853,1417`
- Soft parse path: `gpt/utils/toolcall.py:506,1110-1130`; render soft: `gpt/utils/promptcompat.py:150-175`
