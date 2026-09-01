# Trace Forensics — Quota & Reliability (2026-08-25)

Nguồn dữ liệu (READ-ONLY, không bắn turn):
- `~/Downloads/webgpt/logs/trace.jsonl` — 9.203 events, 2.5MB, stream-parse bằng python.
- `~/Downloads/webgpt/logs/prompt-debug/wgs_*` — 764 dump (~327 session run A + 9 session run B), dùng mtime để neo wall-clock.
- `journalctl --user -u webgpt-gateway` — xác nhận các mốc restart.

## Phạm vi dữ liệu

trace.jsonl chứa đúng 2 process-run (phát hiện qua monotonic_ns reset tại line 7829):

| Run | Wall-clock (neo qua mtime dump wgs_*) | Sessions | request_completed |
|-----|----------------------------------------|----------|-------------------|
| A   | 2026-08-24 ~15:24 → 22:48              | 327      | 563               |
| B   | 2026-08-25 ~12:48 → 13:35              | 9        | 105                |

Tổng: 673 `request_start` / 668 `request_completed` = 625 `/v1/messages` + 38 `/v1/chat/completions` + 5 `/v1/models`. Toàn bộ số liệu dưới đây là **nguyên văn từ trace**, trừ nơi ghi rõ giới hạn.

---

## Q1 — Burst/size hypothesis (VERIFY-R7c): XÁC NHẬN, cliff ở >10k, nặng nhất 20k–50k

Tỷ lệ thành công theo bins `prompt_chars` (`promptcompat/prompt_built`) × status của cùng lifecycle POST `/v1/messages` (625 POST, ghép `request_id`; chars lấy prompt_built đầu tiên trong window). CI Wilson 95%.

| Bin prompt | n | ok | fail | Pass rate | 95% CI | Fail chủ yếu |
|-----------|--:|--:|-----:|----------:|--------|--------------|
| ≤2k       | 305 | 288 | 17 | **94.4%** | [91.3–96.5] | 14× http_503 |
| 2k–5k     | 28  | 28  | 0  | 100%      | [87.9–100]  | — |
| 5k–10k    | 18  | 14  | 4  | 77.8%     | [54.8–91.0] | 3× http_502 |
| 10k–20k   | 12  | 12  | 0  | 100%      | [75.7–100]  | — |
| 20k–50k   | 247 | 75  | 172 | **30.4%** | [25.0–36.4] | **167× http_503** |
| >50k      | 11  | 7   | 4  | 63.6%     | [35.4–84.8] | 3× http_502 |

- Gộp: **≤10k chars: 94.0% pass** (n=351, CI [91.0–96.1]) vs **>10k: 34.8% pass** (n=270, CI [29.4–40.7]).
- Loại nhiễu thời gian: chia run A theo bucket 30 phút — ở các window mà prompt lớn chết hàng loạt (bucket 300–330 và 330–360 phút), prompt ≤10k vẫn **100%** (4/4, 6/6) trong khi >10k chỉ **2/24, 2/24 (~8%)**. Ngược lại ở bucket 210–270 cả hai lớp cùng tụt (small 10/15, 6/17) → có cả thành phần account-level tạm thời.
- Contiguity test: **159/184** request >10k thất bại có ít nhất 1 request OK (bất kỳ cỡ) trong vòng ±120s → fail không phải do outage toàn cục.
- Lưu ý: Run B (sáng nay) chỉ có 6 POST ở 20k–50k, tất cả OK → hiện tượng sụp 20k–50k là bằng chứng ngày hôm qua (run A); n=247 nên vẫn đủ mẫu.
- Kết luận R7c: **đúng hướng nhưng ngưỡng thực tế là >10k với vùng chết 20k–50k; nhỏ ≤10k gần như miễn nhiễm.** Fail gần như đơn nhất là http_503 (rate-limit upstream).

## Q2 — Nhân đôi generation & lãng phí quota

Phân phối số upstream generation (`completionruntime/submit_start`) mỗi POST `/v1/messages`:

| submits/POST | 0 | 1 | 2 | 3 |
|---|--:|--:|--:|--:|
| POSTs | 96 | 508 | 20 | 1 |

→ Gateway-side doubling per POST hầu như đã hết: chỉ 21/625 POST (3.4%) vượt 1 submit (đa phần là resubmit sau failover trong cùng request); 96 POST (15.4%) chết trước khi gửi (69× fast-fail 503).

**Hạch toán quota upstream**: submit_start = 587; submit_completed (turn cam kết) = 376 → **211 sends vô sản = 35.9% quota-lần-thử lãng phí**. Phân rã 211: 205 failed-before-commit (153 RateLimited, 26 AuthRequired, 17 MalformedToolCall, 6 TargetClosedError, 1 BrowserDisconnected, 1 TimeoutError, 1 UIChanged) + 3 commit_unknown + 3 persistent refusal/failure. Số chắc chắn tiêu generation: ≥26 (MalformedToolCall + persistent + browser-death); 153 RateLimited khả năng cao cũng đã send vào conversation (state transition `generating→rate_limited` 129 lần) nhưng không xác nhận được từ trace.

Khuếch đại failover: **228 `webchat/conversation_failover`** (201 rate_limited, 26 auth_required, 1 commit_unknown) — mỗi lần spawn session mới và gửi LẠI toàn bộ prompt → nhân tải đúng lúc bị throttle.

Giới hạn dữ liệu: `turn_id` chỉ non-null trên 48/668 completed (mỗi turn đúng 1 POST) → **không đo được trực tiếp số POST/task phía client**; proxy duy nhất là 210 error response trả về claude-code (nghi vấn mỗi lỗi gây 1 retry client — không kiểm chứng được từ trace).

## Q3 — Top lỗi theo lớp (xếp hạng tần suất)

| # | Lớp lỗi | Đếm (nguồn) |
|---|---------|--------------|
| 1 | **Rate-limit upstream** | 211 POST trả client http_503 (196 trên /v1/messages); 153 submit_failed RateLimited; 129+24 transition generating→rate_limited |
| 2 | **AuthRequired / phiên chết** | 26 failover auth_required + 25 transition generating→auth_required |
| 3 | **Tool-call hỏng** | 17 MalformedToolCall (submit-level) + 21 MALFORMED_TOOL correction-loop = ~38 lần, mỗi lần đốt 1 generation |
| 4 | **http_502** | 13 POST-level (bad gateway, phân bố đều mọi size) |
| 5 | **Browser crash/death** | 6 TargetClosedError + 1 BrowserDisconnected + 1 browser_disconnected transition |
| 6 | Khác | FALSE_COMPLETION corrections 33, TOOL_REFUSAL(+SOFT) 13, Timeout 1, UIChanged 1, 409/404/400/405/500 từng cái |

SSE-chết: **không có event kind nào mô tả SSE/stream lifecycle trong trace** → không đếm được; journal cũng không có signature riêng (giới hạn schema, không suy diễn).

## Q4 — Correction loop & thời gian turn

Sửa cách đếm: `correction_count` trong metadata `request_completed` **luôn = 0 (bug instrumentation)** → đo từ event `tool_correction` trong window từng POST:

| Corrections/POST | 0 | 1 | 2 | 3 | 4 (max cap) |
|---|--:|--:|--:|--:|--:|
| POSTs | 602 | 12 | 1 | 2 | 8 |

- POST vào correction loop: 23/625 (**3.7%**); cần ≥2 corrections: 11 (**1.8%**); chạm cap=4: 8 POST (toàn bộ nỗ lực từ lần 3 trở đi là lãng phí). Loop cạn kiệt: 3 persistent_tool_refusal + 3 persistent_tool_failure.
- Thời gian turn OK (/v1/messages): **mean 13.0s, p50 8.0s, p90 40.2s, p99 71.9s** (n=415).
- Chi phí correction: corr=0 → p50 7.7s; corr=1 → mean 26.0s; corr=4 → mean **77.9s** (~10× turn sạch).
- Chờ lease: queue_ms p50 0s, p90 3.1s, max 34.5s — không phải điểm nghẽn.
- Affinity hit (`position_skipped_affinity_hit`): 278/639 lượt bỏ qua reposition DOM (43.5%).

## Q5 — Đề xuất cải tiến (ưu tiên ROI)

1. **Ngân sách payload ~10k chars trước khi send** (ROI cao nhất). Bằng chứng: ≤10k pass 94.0% vs >10k pass 34.8%; cùng window, small 100% vs big 8%. Action: cắt tail hội thoại + prune tool-schema (đã thấy tool_schema 23.7k chars) để đưa prompt về ≤10k; quá hạn thì tách context sang attachment/summarize hoặc từ chối sớm thay vì đốt một lượt send chắc chắn 503.
2. **Backoff toàn cục + circuit breaker thay vì failover-resend tức thời**. Bằng chứng: 35.9% sends vô sản; 227 failover (201 rate_limited + 26 auth_required) đều resend full prompt sang conversation mới ngay lúc đang bị throttle; failure cluster theo thời gian (bucket 210–360' run A). Action: cooldown chung giữa các session (30s→10min exponential), xếp hàng chờ thay vì spawn conversation mới, dừng hẳn khi 503 liên tục.
3. **Siết correction loop**. Bằng chứng: corr=4 mean 77.9s vs 7.7s turn sạch; 8 POST chạm cap; 38 lỗi tool-call lớp #3. Action: hạ cap thực dụng còn 2 cho MALFORMED_TOOL, inject hint sửa lỗi một lần duy nhất, fail-fast sang client thay vì retry mù.
4. **Vá lỗ hổng observability** (rẻ, mở khóa verify R4): `correction_count` luôn 0, `turn_id` null 92%, thiếu event SSE lifecycle, thiếu correlation id task/client. Action: populate các field này + thêm kind `stream_closed` — giúp đo trực tiếp client-retry/doubling ở lần forensics sau.
5. **Cứng hóa transpiler tool-call**: 17 MalformedToolCall submit-level + 21 trong loop; mỗi occurrence = 1 generation mất trắng. Action: dump raw assistant text vi phạm (đã có cơ chế wgs_*) làm fixture hồi quy cho `utils/toolcall.py`.

## Giới hạn dữ liệu

- Không có wall-clock timestamp trong trace (chỉ monotonic_ns) — mốc ngày giờ được neo gián tiếp qua mtime file prompt-debug + journal.
- Không nhóm được POST thành "task" phía client (thiếu correlation id) → số "% turn vượt 2 POST" chỉ đo được mặt gateway (3.4%), không đo được retry của claude-code.
- SSE lifecycle không có trong schema trace.
- Run B quá ngắn (~47 phút) để đối chiếu độc lập ngày hôm nay.
