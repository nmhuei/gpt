# Live CLI Verification — Round 7d (2026-08-24) — T3 PASS: milestone "Claude Code CLI tự chủ đa bước qua ChatGPT Web" ĐẠT

**Verdict T3: PASS — RC=0 ngay ở turn đầu tiên, tiêu chí đầy đủ.**
`fizzbuzz.py` tồn tại · `output.txt` đúng chuỗi FizzBuzz 1..15 · `python3 fizzbuzz.py`
chạy ra kết quả đúng · model emit 12 tool_use, tất cả tới client, được execute và
kết quả được trả về web-model (12 tool_result) · handshake mang SOFT-COMPACT
(`soft_handshake_appended=true`). **Tuyên bố mốc "Claude Code CLI tự chủ đa bước qua
ChatGPT Web: ĐẠT."**

## Điều kiện & môi trường (sạch tuyệt đối lần đầu)

- Repo `/home/light/GitHub/gpt`, KHÔNG sửa code, browser headless, KHÔNG subagent/web.
- Pre-flight PASS: gateway process start **20:29:54** > `gpt/gateway/runtime.py` mtime
  **20:29:41** ⇒ code mới nhất + SOFT-COMPACT policy sống.
- Budget corrections=4 xác nhận qua env service (`WEBGPT_MAX_CORRECTIONS=4`) — round này
  dùng **0/4** (không cần correction nào).
- Trace: `/home/light/Downloads/webgpt/logs/trace.jsonl`; prompt-debug:
  `/home/light/Downloads/webgpt/logs/prompt-debug/`.
- **Không co-tenant**: session lạ cuối cùng (`wgs_b6c58d…`) kết thúc 21:16:19; từ đó đến
  khi bắn turn, trace chỉ chứa probe của round. Lưu ý: nghỉ thực tế của account tính từ
  submit phi-probe cuối chỉ ~18 phút (21:16→21:34) — ngắn hơn nhiều so với các cửa sổ
  thất bại trước, củng cố thêm rằng biến quyết định là co-tenant chứ không phải giờ nghỉ.
- Turn CLI: `claude -p "Đọc file task.md trong thư mục hiện tại…" --dangerously-skip-permissions
  --model claude-sonnet-4-20250514`, env `ANTHROPIC_BASE_URL=http://127.0.0.1:18000`,
  timeout 900s, cwd `/tmp/cc-live-test7`.

## Timeline (giờ local +0700)

| Thời điểm | Sự kiện |
|---|---|
| 21:26–21:29 | Pre-flight + probe thủ công #1 OK (10.7s) |
| 21:30:56 | Gate R7d start (poll 180s, OK×2 liên tiếp ⇒ tự bắn) |
| 21:31:06 | Probe #1 **OK** (10.2s) |
| 21:34:18 | Probe #2 **OK** (12.0s) ⇒ **GATE_OPEN**, latency gate→turn **<5s** |
| 21:34:21 | `prompt_built`: **42,829 chars · 28 tools · ~10,708 tok · soft_handshake_appended=true** |
| 21:34:32 | Submit #1 thành công — model emit tool_use đầu tiên (`ls`); sau đó 13 submits liên tiếp đều thành công |
| 21:35:52 | **TURN DONE rc=0 elapsed=94s** — dùng 1/2 turn budget |

## Bằng chứng T3d_r1

- Trace (session `wgs_2e16d5c9…`): **14/14 submit_completed, 0 submit_failed,
  0 conversation_failover, 0 correction_prompt_built**; parsed 14 turns với tổng
  **12 tool_calls**.
- Client transcript (CLI): 12 tool_use — `ls` → `cat task.md` → viết fizzbuzz.py →
  chạy → **tự phát hiện indentation bị pipeline làm rơi và tự sửa 3 cách khác nhau**
  (printf → python3 pathlib write_text → base64 decode) → chạy lại mỗi lần → thành công.
  12/12 tool_use có tool_result tương ứng trả về model (loop khép kín đầy đủ).
- Handshake SOFT-COMPACT: text persistence policy ("look around with ls or find … try
  another angle") xác nhận trong prompt dump đầu tiên của conversation; flag
  `soft_handshake_appended=true` trên prompt 42,829 chars.
- Response dump (stdout CLI): model tổng kết bằng tiếng Việt kèm output đúng, không deflect.
- Artifact: `fizzbuzz.py` (142 bytes, Python hợp lệ) + `output.txt` (15 dòng đúng).

## Đo giả thuyết burst/size (mục 5 — không cần thiết nhưng có dữ liệu)

Không rate-limit nào xảy ra nên không đo được tỷ lệ chặn trong cap. Dữ liệu thuần khiết:
request lớn 42,829 chars / 28 tools / ~10.7k tok đi qua **ngay lập tức** (submit đầu tiên
thành công 11 giây sau GATE_OPEN), cùng class request mà trong giai đoạn cap R7c bị chặn
36/48 lần. Kết hợp với việc không co-tenant ⇒ xác nhận blocker R7/R7b/R7c là **đốt quota
bởi client song song**, không phải protocol/gateway/kích thước bản thân nó.

## Bảng tổng R5→R7d riêng T3

| Chỉ số T3 | R5 | R6 | R7 | R7b | R7c | **R7d (round này)** |
|---|---|---|---|---|---|---|
| Attempt | 3 | 3 | 2+BLOCKED | 2 | 2 | **1 (đủ ngay)** |
| RC | 0 silent-deflect ×2 | 0 im lặng ×3 | 1 (503 lộ diện) | 1 cả 2 | 1 cả 2 | **0** |
| Generation web thành công | 6 | 9 | 0 | 0 | 0 | **14** |
| tool_call parse được | 0/3 | có (T3c `cat`) | chưa đo được | chưa đo được | chưa đo được | **12, loop khép kín 12/12 tool_result** |
| Artifact fizzbuzz/output | Không | Không | Không | Không | Không | **Cả hai, nội dung đúng** |
| Latency gate→turn | — | — | ~3 phút | ~2–4 phút | <5s | **<5s** |
| Handshake persistence policy | Không | Không | Không | Không | Có (không đo được hiệu lực) | **Có + phát huy thật (self-recovery 3 lần thay vì bỏ cuộc)** |
| Corrections dùng | — | — | — | — | — | **0/4** |
| Điểm gãy chính | pipeline làm rơi cmd | leg-SSE-loss + handshake false | usage-cap | usage-cap | usage-cap | **Không có — PASS** |

## Verdict chuỗi R5→R7d

Ba round đỏ liên tiếp (R7/R7b/R7c) do blocker môi trường (usage-cap, sau này xác nhận do
co-tenant đốt chung account), không phải protocol/gateway/model. Khi môi trường sạch lần
đầu tiên: mọi lớp đã sửa từ trước (failover lộ diện, soft protocol, SOFT-COMPACT) hoạt
động đúng thiết kế ngay lần đo đầu — model tự khám phá thư mục, đọc task, tự viết script,
tự phát hiện lỗi định dạng do pipeline và tự thử 3 hướng khác nhau cho tới khi chạy đúng,
rồi xác minh kết quả trước khi kết thúc. Đúng hành vi persistence mà A/B prompt-lab dự báo.

**Mốc "Claude Code CLI tự chủ đa bước qua ChatGPT Web": ĐẠT** (điều kiện: môi trường
không co-tenant; khuyến nghị khóa account độc quyền trước các đo tiếp theo).
