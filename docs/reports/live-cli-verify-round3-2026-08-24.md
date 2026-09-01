# Live CLI Verification — Round 3 (2026-08-24)

Round thứ ba của thang verify, lần đầu chạy với observability đầy đủ trên gateway:
`WEBGPT_PROMPT_DEBUG_DIR` + `--trace-file trace.jsonl` + `WEBGPT_MAX_CORRECTIONS=4`
(unit `webgpt-gateway.service`, restart 15:20:01 với correction prompt đã viết lại có ví dụ mẫu).

- Repo: `/home/light/GitHub/gpt` — KHÔNG sửa code production trong round này.
- Gateway: `http://127.0.0.1:18000`, healthy đầu và cuối (browser ready, workers live=2).
- Thư mục test: `/tmp/cc-live-test3`, task.md fizzbuzz tiếng Việt y hệt R1/R2.
- Turn CLI đã dùng: **5/8** hợp lệ (T1 ×1, T2 ×2, T3 ×2) + **1 turn vô hiệu** (xem bẫy env bên dưới).
- Observability: `/home/light/Downloads/webgpt/logs/prompt-debug/` (41 file) và
  `/home/light/Downloads/webgpt/logs/trace.jsonl` (400 events, seq 1–400).

## Cảnh báo phương pháp (bẫy env)

Lần chạy T1 đầu tiên (5s, PASS) thực chất **không đi qua gateway**: shell có sẵn
`ANTHROPIC_BASE_URL=http://127.0.0.1:4000` (opencode-bridge) và wrapper `~/.local/bin/gpt`
ưu tiên giá trị có sẵn trong môi trường. Đã chạy lại T1 với env ép
`ANTHROPIC_BASE_URL=http://127.0.0.1:18000`. Mọi round sau này phải set env tường minh.

## Bảng so sánh

| Mức | Round 2 | Round 3 | Thay đổi |
|---|---|---|---|
| T1 kết nối & streaming | PASS lần 1, 45s, còn tiền tố `Thinking` | **PASS lần 1, 16s**, stdout đúng `"AY OK" / "GATEWAY OK"`, **sạch hoàn toàn** | ✅ Thinking strip trên browser path hoạt động; latency giảm ~65% |
| T2 tool use 1 bước | FAIL ×2, 76–79s, `TOOL_REFUSAL` cạn budget=2 | FAIL ×2 (139s mỗi lần): lần 1 `FALSE_COMPLETION` cạn budget=4; lần 2 `Persistent tool refusal (FALSE_COMPLETION→TOOL_REFUSAL_SOFT)` — fail-fast mới kích hoạt | ❌ Vẫn đỏ. corrections 2→4 không thay đổi kết quả; chỉ thêm ~60s đốt budget |
| T3 đa bước tự chủ | FAIL ×2 (106s/82s), MALFORMED_TOOL + FALSE_COMPLETION | FAIL ×2 (229s/86s), cùng hai lý do; filesystem **không có file nào được tạo** (10 file trong dir chỉ là log .stdout/.stderr) | ❌ Không tiến bộ chức năng |
| Output nhiễm bẩn | Tiền tố `Thinking` ở T1 | Sạch | ✅ Đã fix xong |
| Fail-honest | 500/502 sạch, retryable | Giữ nguyên + thêm đường `persistent_tool_refusal` cắt sớm vòng lặp vô vọng | ✅ (nhỏ) |
| Mẫu 2-POST/attempt | Quan sát qua journal, chưa xác nhận | **XÁC NHẬN bằng trace**: mọi attempt đỏ đều là 2 POST (200 rồi 502 cùng port client); SDK retry sau error event và **mỗi POST mở session web mới, đốt lại toàn bộ budget** | ⚠️ Nhân đôi chi phí confirmed |

## Số generation thực tế (từ trace.jsonl)

Mẫu nhân đôi **được xác nhận, không bị vô hiệu**. Thực đo theo attempt:

| Attempt | POST | Session | Generations | Chuỗi lý do correction (chars) |
|---|---|---|---|---|
| T1 | 1 POST 200 | bc49cb | **1** | (không có) |
| T2a | 2 POST | 14ad2a / e6ddb9 | 5 + 5 = **10** | FC(20)→FC(370)→FC(380)→MT(457) ‖ RS(390)→MT(403)→MT(455)→MT(412) |
| T2b | 2 POST | 4b0163 / b61311 | 5 + 4 = **9** | FC(20)→MT(309)→MT(359)→MT(615) ‖ FC(20)→FC(20)→FC(295)→**persistent_refusal** |
| T3a | 2 POST | 9e2932 / 767ef9+aa1bd3* | 5 + 5 = **10** | RS(378)→MT(667)→MT(712)→MT(706) ‖ FC(240)→FC(304)→FC(294)→MT(445) |
| T3b | 2 POST | d33a13 / 719be4 | 2 + 5 = **7** | FC(164)→**persistent_refusal** ‖ FC(346)→FC(9)→MT(128)→FC(9) |

FC = FALSE_COMPLETION, MT = MALFORMED_TOOL, RS = TOOL_REFUSAL_SOFT. (\*) aa1bd3 là session
count_tokens. Tổng thật: **37 generations cho 5 turns, 0 tool call hợp lệ**. Lưu ý thêm: mỗi
request `count_tokens` của SDK cũng chạy full prompt-build và ghi thêm prompt-debug dump
(16 file pre_gpt cho 11 session thật) — nhiễu lưu trữ không cần thiết.

## Raw response theo lượt correction — giới hạn của observability hiện tại

**Kết quả quan trọng nhất về instrumentation: raw text của model KHÔNG được lưu ở bất kỳ đâu.**
Prompt-debug chỉ ghi prompt GỬI ĐI (`_write_prompt_debug`, runtime.py:902); trace chỉ ghi
`assistant_chars` (runtime.py:1240); `conversations.json` để `last_response=null`,
`messages=[]` với các session thất bại; journal chỉ có dòng HTTP. Vì vậy dưới đây là dữ liệu
nguyên vẹn gần nhất thu được:

1. **Nguyên văn phần đầu đề correction prompt gửi đi từng lượt** (file prompt-debug, giữ nguyên):
   - FALSE_COMPLETION: `Correction reason: FALSE_COMPLETION.` + `Validation detail: task requires a controller tool but model returned only prose.`
   - MALFORMED_TOOL: `Validation detail: Tool block did not contain any valid tool calls..` (có lỗi chính tả dấu chấm kép trong format string)
   - TOOL_REFUSAL_SOFT: `REFUSAL OVERRIDE` + `soft-refusal signals: counter_question`
   - Toàn bộ kèm TOOL CALL FORMAT + 3 ví dụ mẫu (Bash, heredoc, Write.lines) — prompt đã mạnh hơn R2 nhưng vẫn thua.
2. **Nguyên văn lỗi tới tay CLI** (đầy đủ, chưa redact gì vì không chứa token):
   - T2a: `API Error: 502 Tool correction budget exhausted (MALFORMED_TOOL): Tool block did not contain any valid tool calls.`
   - T2b: `API Error: 502 Persistent tool refusal after correction (FALSE_COMPLETION -> TOOL_REFUSAL_SOFT): model deflected instead of calling controller tools (soft-refusal signals: counter_question).`
   - T3b: `API Error: 502 Tool correction budget exhausted (FALSE_COMPLETION): task requires a controller tool but model returned only prose.`
3. **Phân tích gián tiếp từ độ dài response** (`assistant_chars`) — đáng chú ý:
   - Response đầu tiên của session tươi luôn ngắn bất thường: **đúng 20 ký tự ở 3/6 session**
     (14ad, 4b01, b613 — cả ba đều là T2), rồi FC. 20 chars ≈ một câu chốt ngắn kiểu xác nhận/
     từ chối một dòng, không phải nỗ lực làm task.
   - Sau correction, response dài 300–700 chars và lý do chuyển thành MALFORMED_TOOL: model
     **CÓ phát markup dạng tag** (`parse_tool_calls` chỉ raise "did not contain any valid tool
     calls" khi có open/close tag nhưng không parse được invoke nào — toolcall.py:854) nhưng
     cấu trúc `<invoke>`/`<parameter>` bên trong sai so với regex mong muốn.
   - T3b xuất hiện response **9 ký tự** ×2 — gần như chắc chắn câu cụt ("Đã chạy." / "Xong.").

## Phân tích refusal/format — nguyên nhân gốc nhìn được từ dữ liệu

1. **Correction prompt bị cắt mất ngữ cảnh task (`tail_messages: 0`).** Mọi correction prompt
   đều chỉ gồm khối CORRECTION + tool schema + rules/examples, với chữ
   *"performs the next step of the current user task"* nhưng **không hề chứa user task**.
   Trên thread browser mới (mỗi POST = session mới, conversation rỗng), model không biết task
   là gì → hỏi ngược (chính là signal `counter_question` bắt được ở TOOL_REFUSAL_SOFT) hoặc
   đáp xã giao 20 chars. Đây là giải thích khớp nhất cho việc response đầu session luôn cụt.
2. **MALFORMED_TOOL kéo dài dù đã có ví dụ:** model cố tuân thủ (300–700 chars, có tag) nhưng
   invoke bên trong không khớp parser. Không có raw text nên chưa xác định được model viết sai
   cú pháp chỗ nào (fence markdown? CDATA thiếu? tên param sai?) — đây chính là chỗ cần raw dump.
3. **corrections=4 chỉ làm tốn thời gian:** không một chuỗi correction nào tiến tới tool call
   hợp lệ; nâng budget không thể xanh khi (1) còn đứng.
4. **Fail-fast `persistent_tool_refusal` hoạt động đúng** (T2b POST2 dừng ở gen 4, T3b POST1
   dừng ở gen 2) — tiết kiệm 1–3 generations mỗi khi kích hoạt.
5. **Thinking strip đã sạch** trên browser path (T1 stdout không còn tiền tố).

## Verdict tổng thể

Gateway đạt **T1 xanh tuyệt đối (nhanh, sạch, ổn định)** nhưng T2/T3 vẫn đỏ 100% như R2.
Điểm khác biệt cốt lõi: giờ ta BIẾT vì sao — correction loop gửi cho model một mệnh lệnh làm
task mà không kèm task, vào một thread conversation rỗng, nên refusal là hành vi hợp lý của
model chứ không phải "cứng đầu". Chưa dùng được tool-use thật; chưa tạo được artifact nào.

## Đề xuất cho vòng sửa kế (theo thứ tự tác động kỳ vọng)

1. **Nhúng user task vào correction prompt**: khi xây correction, truyền `tail_messages`/prompt
   gốc (ít nhất message user cuối) vào `_controller_tool_correction_prompt` thay vì
   `tail_messages=0` (runtime.py:1267). Nếu e ngại leak, redact như pre_gpt dump đang làm.
2. **Thêm raw-response dump cạnh prompt dump**: ghi `result.text` (redacted) vào
   prompt-debug dir tại mỗi `tool_correction` emit — một file `.response.txt` cho mỗi
   `<seq>_..._correction.txt`. Không có dữ liệu này thì không thể chẩn đoán MALFORMED_TOOL chính xác.
3. **Chống nhân đôi chi phí**: sau error event, cân nhắc trả thẳng 5xx cho SDK ở POST đầu
   hoặc đánh dấu session để POST retry tái dùng conversation thay vì mở session mới đốt lại 4 corrections.
4. **Sửa format string** `Validation detail: {detail}..` (dấu chấm kép) và cân nhắc đưa 1 đoạn
   trích ≤200 chars của response sai vào detail để debug qua error body.
5. Giảm noise: bỏ prompt-build/prompt-debug cho request `count_tokens`.

## Phụ lục — artifact raw

- Trace: `/home/light/Downloads/webgpt/logs/trace.jsonl` (400 events)
- Prompt-debug: `/home/light/Downloads/webgpt/logs/prompt-debug/` (41 file, seq 000003–000392)
- CLI outputs: `/tmp/cc-live-test3/t{1,2,2b,3,3b}.{stdout,stderr}`
- Session store: `/home/light/Downloads/webgpt/tmp/conversations.json`
