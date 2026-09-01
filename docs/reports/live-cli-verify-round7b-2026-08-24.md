# Live CLI Verification — Round 7b (2026-08-24) — T3 RETRY: STILL BLOCKED_BY_USAGE_CAP

**Verdict T3: FAIL — BLOCKED_BY_USAGE_CAP (lần 2).** Không tuyên bố mốc "Claude Code CLI tự chủ
đa bước qua ChatGPT Web". Cả 2 turn CLI của round này đều chết bởi usage-cap phía tài khoản
ChatGPT Web, không phải lỗi protocol/gateway pipeline. **Không một generation web nào của turn
CLI thành công trong round này** ⇒ FIX-A/FIX-B vẫn chỉ được xác minh ở mức single-step (R7),
chưa đo được ở mức đa bước.

## Điều kiện & môi trường

- Repo: `/home/light/GitHub/gpt` — KHÔNG sửa code. Browser headless.
- Gateway: `http://127.0.0.1:18000`, account `personal`, max-workers 8 / warm 4,
  `WEBGPT_TOOL_PROTOCOL=soft`, headless, allow-authenticated.
- Thư mục test: `/tmp/cc-live-test7`, task.md fizzbuzz tiếng Việt nguyên trạng.
- Turn CLI: `claude -p "Đọc file task.md … thực hiện chính xác yêu cầu" --dangerously-skip-permissions
  --model claude-sonnet-4-20250514`, env `ANTHROPIC_BASE_URL=http://127.0.0.1:18000`,
  timeout 900s, cwd `/tmp/cc-live-test7`.
- Gate: probe SSE nhỏ `/tmp/sse_probe7.py` mỗi 3 phút, cần **OK 2 lần liên tiếp** mới bắn turn.
- Budget turn: **dùng 2/2** (T3 attempt + 1 retry). Không chạy lại T1/T2.

## Timeline (giờ local +0700)

| Thời điểm | Sự kiện |
|---|---|
| 19:59 | Cap bắt đầu (từ R7) |
| 20:20:23 | Probe #1 **OK** (status 200, delta `OK7`, 44s) |
| 20:23:36 | Probe #2 **OK** (12s) ⇒ GATE_OPEN lần 1 |
| 20:27–20:29 | **T3r1 bắn** → RC=1 sau ~60s; trace: 100% submit `generating → rate_limited` trên 4 session fresh liên tiếp (`wgs_ffcba830…`, `wgs_cd1bbc45…`, `wgs_4a91c501…`, `wgs_c4495bee…`, `wgs_830fe860…`), failover conversation ×4, cạn retry ⇒ 503 |
| 20:28 | Trace seq 1417: *"ChatGPT Web reports that the current usage limit was reached."* |
| 20:29 | **Gateway được restart bởi tác nhân ngoài** (PID mới 2901717, start 20:29; watchdog.log không ghi restart này — KHÔNG phải do agent round này) |
| 20:31:10 | Probe #3 **OK** (9.8s, trên gateway mới) |
| 20:34:20 | Probe #4 **OK** (9.2s) ⇒ GATE_OPEN lần 2 |
| 20:36–20:38 | **T3r2 bắn (retry duy nhất)** → RC=1; cùng pattern rate_limited trên session fresh (trace numbering mới: seq 121–214, `conversation_failover reason=rate_limited`) |
| 20:42 | Cửa sổ chờ +30 phút còn dư nhưng budget turn đã cạn (2/2) ⇒ dừng, viết báo cáo |

## Bằng chứng T3 round này

| | T3r1 (20:27) | T3r2 (20:36) |
|---|---|---|
| RC | 1 | 1 |
| Thời gian tới lỗi | ~60s (503 sau 35s request cuối) | ~90s |
| stdout nguyên văn | `API Error: 503 Conversation failed over to a fresh ChatGPT web session; please resend this request to continue. This is a server-side issue, usually temporary — try again in a moment. If it persists, check your inference gateway (127.0.0.1:18000).` | y hệt |
| Artifact fizzbuzz.py / output.txt | **KHÔNG** | **KHÔNG** |
| Generation web thành công | 0 | 0 |
| rate_limited transitions đo được | ≥5 (mỗi session fresh 1) | 13 trong cửa sổ (13 `submit_failed_before_commit_unknown`) |

Phân tích đích danh:

- **Không phải model behavior**: model không kịp emit bất cứ thứ gì có thể đánh giá — mọi submit
  bị ChatGPT Web từ chối trước khi sinh token (`generating → rate_limited` sau 1.7–4.3s).
- **Không phải gateway pipeline**: pipeline hành xử đúng thiết kế với đầu vào nó nhận được —
  boot session fresh → submit → nhận rate-limit → failover conversation → retry → trả 503 rõ ràng
  cho client (RC=1, không silent-fail kiểu RC=0 của R5/R6).
- **Blocker là tài khoản**: đúng như cảnh báo R7 — một client khác chạy song song trên cùng
  gateway + cùng account `personal`, đốt quota liên tục; cap quay lại chỉ vài phút sau khi cửa
  sổ mở.
- **Anomaly đáng lưu ý**: probe nhỏ (64 max_tokens, không tools) thành công ổn định (4/4 trong
  round, 9–44s) trong khi turn CLI lớn (~17 tools + system prompt lớn) bị rate-limit 100%.
  Có thể ChatGPT Web áp giới hạn theo burst/kích thước request, hoặc các probe "chui" vào khe
  hở giữa các đợt burn của client song song. Cần tách biệt trong round kế nếu muốn mở cửa sổ
  bền hơn (ví dụ: dừng client song song, hoặc dùng account riêng).

## Bảng so sánh T3 qua các round + generation count

| Chỉ số T3 | R5 (soft stealth) | R6 (fix A/B, stale runtime) | R7 (fresh runtime) | **R7b (round này)** |
|---|---|---|---|---|
| Attempt | 3 (a/b/c) | 3 (a/b/c) | 2 + BLOCKED | **2 (r1/r2)** |
| RC | a/b: RC=0 silent-deflection; c: — | RC=0 im lặng cả 3 | RC=1 (503 lộ diện) | **RC=1 cả 2** |
| Generation web thành công | 1+1+4 = 6 | 4+1+4 = 9 | **0** / ≥10 RateLimited | **0** / ≥18 RateLimited |
| tool_call parse được | 0/0/3 | có (T3c `cat` execute thật) | chưa đo được (0 gen) | **chưa đo được (0 gen)** |
| Artifact fizzbuzz/output | Không | Không | Không | **Không** |
| Điểm gãy chính | pipeline làm rơi cmd đủ | leg-SSE-loss + handshake false | usage-cap tài khoản | **usage-cap tài khoản (tái hiện)** |
| Silent-fail RC=0 | Có | Có ×3 | Không | **Không** (fail luôn 503, RC=1) |

Generation count toàn round 7b (attributable turn CLI): **T3r1 = 0 ok / 4+ session RateLimited ·
T3r2 = 0 ok / 13 submit_failed RateLimited**. Hai generation thành công duy nhất trong cửa sổ
đều là probe nhỏ (2× `OK7`). Trace dùng chung nên số tuyệt đối có nhiễu từ client song song.

## Kết luận & khuyến nghị

1. Mốc "CLI tự chủ đa bước qua ChatGPT Web" **CHƯA đạt** — T3 đỏ 4 round liên tiếp nhưng 2 round
   gần nhất đỏ vì blocker môi trường, không phải protocol. Single-step (T1/T2) đã PASS sạch trên
   fresh runtime (R7): leak-tag sạch, handshake true, correction loop hoạt động.
2. Để hoàn tất T3 cần một trong: (a) dừng/tách client song song trên cùng account;
   (b) account riêng cho verify; (c) chờ cửa sổ reset cap dài (thường theo giờ, không phải phút);
   (d) giảm kích thước request turn CLI nếu backend giới hạn theo size/burst.
3. Khi cap mở: giữ nguyên protocol round này (gate 2×OK cách 3 phút, bắn ngay sau OK thứ hai —
   độ trễ gate→turn <1 phút vì R7b cho thấy cửa sổ chỉ sống vài phút).

## Đường dẫn vật chứng

- Response dumps: `/tmp/cc-live-test7/t3r1.stdout` (+ `.stderr`, `.rc`),
  `/tmp/cc-live-test7/t3r2_response.txt` (+ `.stderr`, `.rc`)
- Trace: `/home/light/Downloads/webgpt/logs/trace.jsonl` (old-process seq 1406–1479 = T3r1;
  new-process seq 121–214 = T3r2)
- Prompt debug dumps: `/home/light/Downloads/webgpt/logs/prompt-debug/`
- Gate logs: `/tmp/t3_gate7b_round1.log` (cửa sổ 1), `/tmp/t3_gate7b.log` (cửa sổ 2)
- Poller: `/tmp/t3_gate7b.sh`; probe: `/tmp/sse_probe7.py`
