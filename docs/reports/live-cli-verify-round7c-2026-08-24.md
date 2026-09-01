# Live CLI Verification — Round 7c (2026-08-24) — T3 RETRY: BLOCKED_BY_USAGE_CAP (lần 3), giả thuyết burst/size có dữ liệu xác nhận hướng

**Verdict T3: FAIL — BLOCKED_BY_USAGE_CAP (lần 3 liên tiếp: R7, R7b, R7c).** Không tuyên bố mốc
"Claude Code CLI tự chủ đa bước qua ChatGPT Web". Cả 2 turn CLI của round này đều chết bởi
usage-cap trước khi model kịp sinh bất kỳ token nào. **Điểm mới của R7c**: (1) đây là round đầu
tiên mà MỌI attempt đều mang SOFT-COMPACT persistence policy trong handshake (gateway restart
20:29) — nhưng không đo được hiệu lực vì không generation nào thành công; (2) gate latency đạt
**<5 giây** từ OK thứ hai (yêu cầu <60s) và turn vẫn bị rate-limit 100% ⇒ loại trừ nguyên nhân
"bắn chậm nên cửa sổ đóng"; (3) thu được số cứng cho giả thuyết burst/size.

## Điều kiện & môi trường

- Repo: `/home/light/GitHub/gpt` — KHÔNG sửa code. Browser headless. KHÔNG subagent, KHÔNG web.
- Gateway: `http://127.0.0.1:18000`, PID 2901717 (start 20:29), account `personal`,
  `WEBGPT_TOOL_PROTOCOL=soft`, headless, allow-authenticated.
  **SOFT-COMPACT persistence policy xác nhận sống trong handshake**:
  `gpt/gateway/runtime.py:857` (`_SOFT_HANDSHAKE_TEXT`, nhánh WEBGPT_TOOL_PROTOCOL=soft,
  append một lần vào cuối first user turn của conversation fresh). Policy = baseline
  (convention `<cmd>`) + 2 câu persistence ("look around with ls/find", "one step at a time /
  only call done when proven / try another angle"). Đây là bản gateway mọi attempt R5–R7b
  đều CHƯA từng chạy với nó.
- Thư mục test: `/tmp/cc-live-test7`, task.md fizzbuzz tiếng Việt nguyên trạng.
- Turn CLI: `claude -p "Đọc file task.md trong thư mục hiện tại và thực hiện chính xác yêu cầu
  trong đó." --dangerously-skip-permissions --model claude-sonnet-4-20250514`,
  env `ANTHROPIC_BASE_URL=http://127.0.0.1:18000`, timeout 900s, cwd `/tmp/cc-live-test7`.
- Gate: probe SSE nhỏ `/tmp/sse_probe7.py` mỗi **120s** (R7b dùng 180s), cần **OK 2 lần liên
  tiếp**; khi mở, script tự bắn turn ngay trong cùng process (setsid) ⇒ latency gate→turn <5s.
- Budget turn: **dùng 2/2** (T3c_r1 auto-fire ở cửa sổ 1 + T3c_r2 retry ở cửa sổ 2).

## Timeline (giờ local +0700)

| Thời điểm | Sự kiện |
|---|---|
| 20:48:16 | Round bắt đầu. Gateway healthy (`active_sessions: 64`, workers idle) |
| 20:50:18 | Gate v1 start (poll 120s) |
| 20:53:59 | Probe #1 **OK** (status 200, delta `OK7`, 42.6s) |
| 20:56:09 | Probe #2 **OK** (10.0s) ⇒ **GATE_OPEN #1** |
| 20:56:09 | **T3c_r1 bắn tự động, latency <5s** |
| 21:01:48 | **T3C_R1_DONE rc=1 elapsed=339s** → 503 rate-limit; trace: prompt 47,802 chars / 24 tools / ~11,951 tok bị `submit_failed RateLimited` + `conversation_failover` lặp đến cạn retry |
| ~21:02 | Probe thủ công kiểm tra: **FAIL** ("Conversation failed over to a fresh ChatGPT web session") ⇒ cửa sổ đã đóng lại chỉ vài phút sau khi mở |
| 21:05:08 | Gate phase-2 start (đợi cửa sổ cho turn retry cuối) |
| 21:05:51 | Probe FAIL (42.9s) |
| 21:08:02 | Probe **OK** (10.3s) |
| 21:10:44 | Probe **OK nhưng flap**: status 200 + delta `OK7` xong kèm trailing `api_error` "Conversation failed over…" (41.7s) ⇒ GATE_OPEN #2 trên cửa sổ đã suy yếu |
| 21:10:44 | **T3c_r2 bắn (turn cuối budget)** |
| 21:14:45 | **T3C_R2_DONE rc=1 elapsed=241s** → cùng 503; không artifact. Budget cạn ⇒ dừng, viết báo cáo |

Song song toàn thời gian: session lạ `wgs_be5f221ce67f…` (không phải turn của agent này)
chạy vòng lặp liên tục `submit → submit_failed RateLimited → conversation_failover` với các
prompt kích thước 5,235 / 18,194 / 69,158 chars — **client song song từ R7b vẫn đang đốt quota
trên cùng account**, với mật độ 226 trace events/10 phút đo lúc 21:00 khi không turn nào của
round đang chạy.

## Bằng chứng T3 round này

| | T3c_r1 (20:56) | T3c_r2 (21:10) |
|---|---|---|
| RC | 1 | 1 |
| Latency gate→turn | <5s | <5s |
| Thời gian tới lỗi | 339s | 241s |
| stdout nguyên văn | `API Error: 503 Conversation failed over to a fresh ChatGPT web session; please resend this request to continue. This is a server-side issue, usually temporary — try again in a moment. If it persists, check your inference gateway (127.0.0.1:18000).` | y hệt |
| Prompt gửi lên (trace `prompt_built`) | 47,802 chars · 24 tools · ~11,951 tok | tương tự (cùng class) |
| Artifact fizzbuzz.py / output.txt | **KHÔNG** | **KHÔNG** |
| Generation web thành công | 0 | 0 |
| SOFT-COMPACT trong handshake | Có (lần đầu trong lịch sử T3) | Có |

Phân tích đích danh:

- **Không phải model behavior**: model không emit gì để đánh giá; mọi submit bị ChatGPT Web
  chặn pre-generation. Hiệu lực SOFT-COMPACT ở mức đa bước vì vậy **vẫn chưa đo được** (A/B
  2/2 của prompt-lab chỉ là single-step).
- **Không phải gate latency**: R7c đã chứng minh bắn <5s sau OK thứ hai vẫn đủ bị chặn ⇒ loại
  trừ giả thuyết "cửa sổ đóng do bắn chậm" của R7b.
- **Không phải gateway pipeline**: hành xử đúng thiết kế (failover rõ ràng, 503 lộ diện, RC=1).
- **Blocker là tài khoản + hình thái request**: xem đo dưới đây.

## Đo giả thuyết burst/size (bước 3 của quy trình)

Số liệu từ `prompt_built` trong trace, giới hạn từ 20:29 (process gateway hiện tại) đến hết
round — đúng giai đoạn cap hoạt động:

| Class request | n | Bị rate-limit | Tỷ lệ sạch |
|---|---|---|---|
| Nhỏ ≤10k chars (probe: 85–518 chars, 0 tools, ~22–130 tok) | 12 | 2 | **~83%** |
| Lớn >10k chars (turn CLI: 42.8k–47.8k chars, 24–28 tools, ~11–17k tok) | 48 | 36 | **~25%** |

Kích thước tương đối: turn CLI ≈ **500× probe** về chars (85 vs 47,802) và ≈ **480×** về token
ước tính (22 vs 11,951).

Kết luận correlation: **trong chế độ cap, request nhỏ đi qua ổn định còn request lớn gần như
luôn bị chặn — kể cả khi bắn <5s sau khi probe nhỏ vừa thành công 2 lần liên tiếp.** Điều này
hướng mạnh tới backend áp giới hạn theo burst/kích thước request (hoặc ít nhất là cap "soft"
cho phép request nhỏ lọt khe). Lưu ý phản ví dụ lịch sử: trước 19:59 (trước cap) request lớn
vẫn thành công bình thường (R5: 6 gens, R6: 9 gens) ⇒ size không phải điều kiện đủ, chỉ là
yếu tố phân biệt khi cap active. Số tuyệt đối có nhiễu do client song song dùng chung trace.

## Bảng so sánh T3 qua các round

| Chỉ số T3 | R5 (soft stealth) | R6 (fix A/B) | R7 (fresh runtime) | R7b (SOFT-COMPACT chưa có) | **R7c (SOFT-COMPACT live, round này)** |
|---|---|---|---|---|---|
| Attempt | 3 | 3 | 2 + BLOCKED | 2 | **2** |
| RC | RC=0 silent-deflection ×2 | RC=0 im lặng ×3 | RC=1 (503 lộ diện) | RC=1 cả 2 | **RC=1 cả 2** |
| Generation web thành công | 6 | 9 | 0 | 0 | **0** |
| tool_call parse được | 0/3 | có (T3c `cat`) | chưa đo được | chưa đo được | **chưa đo được** |
| Artifact fizzbuzz/output | Không | Không | Không | Không | **Không** |
| Latency gate→turn | — | — | ~3 phút | ~2–4 phút | **<5s (tự động)** |
| Handshake mang persistence policy | Không | Không | Không | Không | **Có (2/2 attempt)** |
| Điểm gãy chính | pipeline làm rơi cmd | leg-SSE-loss + handshake false | usage-cap | usage-cap | **usage-cap (kể cả bắn tức thời)** |

## Kết luận & khuyến nghị

1. Mốc "CLI tự chủ đa bước qua ChatGPT Web" **VẪN CHƯA đạt** — T3 đỏ 5 round liên tiếp; 3
   round gần nhất đỏ vì blocker môi trường (usage-cap), không phải protocol/gateway/model.
   Single-step (T1/T2) vẫn PASS (R7).
2. **Cửa sổ reset KHÔNG theo giờ đồng hồ.** Các mốc OK×2 quan sát được (khung cho suy chu kỳ):
   - R7b: mở 20:20:23+20:23:36 và 20:31:10+20:34:20
   - R7c: mở 20:53:59+20:56:09 và 21:08:02+21:10:44
   Khoảng cách giữa các lần mở: 11 phút → 23 phút → 14 phút. Cửa sổ sống chỉ ~4–8 phút
   (cửa sổ #2 của R7c đã flap ngay tại lúc mở). Mẫu hình khớp tốt hơn với **khoảng nghỉ của
   client song song** hơn là chu kỳ reset theo giờ.
3. Để hoàn tất T3 cần (theo thứ tự khả thi): (a) **dừng/tách client song song** trên cùng
   account `personal` — bằng chứng R7c cho thấy nó đang burn liên tục cả trong và ngoài cửa
   sổ; (b) account riêng cho verify; (c) nếu muốn thử trong cap: giảm kích thước request turn
   CLI (ít tools/system prompt gọn) để rơi vào class nhỏ đã chứng minh 83% lọt — nhưng đây là
   thay đổi cấu hình turn, không phải sửa code gateway.
4. Giữ nguyên cơ chế gate tự động của R7c (poll 120s, OK×2, auto-fire in-process): latency
   <5s đã chạm trần hữu ích; siết thêm không giải quyết được gì vì r1 thất bại ngay cả khi
   bắn tức thời.

## Đường dẫn vật chứng

- Turn outputs: `/tmp/cc-live-test7/t3c_r1.stdout` (+ `.stderr`, `.rc`),
  `/tmp/cc-live-test7/t3c_r2.stdout` (+ `.stderr`, `.rc`)
- Gate logs: `/tmp/t3_gate7c.log` (cả 2 pha, kèm dòng `GATE_OPEN firing … NOW`)
- Gate scripts: `/tmp/t3_gate7c.sh` (pha 1), `/tmp/t3_gate7c_r2.sh` (pha 2); probe:
  `/tmp/sse_probe7.py`
- Trace: `/home/light/Downloads/webgpt/logs/trace.jsonl` (T3c_r1 prompt 47,802 chars;
  failover loop của client song song trên `wgs_be5f221ce67f…`)
- SOFT-COMPACT policy: `gpt/gateway/runtime.py:857`
- Prompt debug dumps: `/home/light/Downloads/webgpt/logs/prompt-debug/`
