# Live CLI Verification — Round 4 (2026-08-24)

Vòng đo hội tụ của mọi fix lần đầu: `WEBGPT_TOOL_PROTOCOL=both` + ORIGINAL USER TASK trong
mọi correction prompt (R5-FIX) + discover-first policy + corrections=4 + raise-sớm persistent
+ response dump mới (`<seq>_<session>_response.txt`).

- Repo: `/home/light/GitHub/gpt` — KHÔNG sửa code.
- Gateway: `http://127.0.0.1:18000`, unit restart 16:33:33 với env mới, healthy đầu và cuối.
- Thư mục test: `/tmp/cc-live-test4` (sạch), task.md fizzbuzz tiếng Việt y hệt R1–R3.
- Turn CLI đã dùng: **5/8** (T1 ×2, T2 ×1, T3 ×2), env ép tường minh
  `ANTHROPIC_BASE_URL=http://127.0.0.1:18000` (tránh bẫy opencode-bridge :4000 của R3).
- Observability: `/home/light/Downloads/webgpt/logs/prompt-debug/` (+~90 file mới, trong đó
  **24 `_response.txt`** — lần ĐẦU TIÊN có raw text model trả về) và
  `/home/light/Downloads/webgpt/logs/trace.jsonl` (seq 1–245 cho round này).
- CLI outputs: `/tmp/cc-live-test4/t{1,1b,2,3,3b}.{stdout,stderr}`.

## Bảng so sánh 3 vòng

| Mức | Round 2 | Round 3 | Round 4 (nay) |
|---|---|---|---|
| T1 kết nối & streaming | PASS lần 1, 45s | PASS lần 1, 16s, sạch | ⚠️ **Kết nối/stream OK (RC=0) nhưng SAI NỘI DUNG ×2**: model từ chối echo `"AY OK GATEWAY OK"`, trả prose tiếng Việt + hỏi ngược, tự nhận request là "bootstrap nhúng trong dữ liệu người dùng" (~40s/lần) |
| T2 tool use 1 bước | FAIL ×2 (TOOL_REFUSAL cạn budget=2) | FAIL ×2 (FC→cạn budget=4 / persistent) | **FAIL** (1 turn, ~100s): `502 Tool correction budget exhausted (FALSE_COMPLETION)` sau 10 generations, 0 tool call |
| T3 đa bước tự chủ | FAIL ×2 (MALFORMED + FC, không artifact) | FAIL ×2 (229s/86s, không artifact) | **FAIL ×2**: T3a `502 budget exhausted (TOOL_REFUSAL)`; T3b `502 Persistent TOOL_REFUSAL after correction`. Filesystem: **không có fizzbuzz.py/output.txt** |
| Output nhiễm bẩn | Tiền tố `Thinking` | Sạch | Sạch (nhưng nội dung sai ở T1) |
| Fail-honest / raise-sớm | 500/502 retryable | persistent_tool_refusal cắt sớm | ✅ Hoạt động đúng: kích hoạt 2 lần, dừng ở correction_count=1 (T3a POST1, T3b POST2) |
| Nhân đôi chi phí (2 POST/turn) | Chưa xác nhận | Confirmed trên các attempt ĐỎ | **Xấu hơn: giờ nhân đôi trên MỌI turn kể cả turn RC=0** (T1 ×2 POST cũng 2 generation) |

## Format model thực sự emit — bằng chứng raw lần đầu tiên

24 file `_response.txt` của round này, kiểm tra toàn bộ:

- **```json fence: 0/24** — giao thức json-fn được dạy KHÔNG bao giờ được thử.
- **`<tool_calls>` block: 0/24 thật** — duy nhất 1 file nhắc chuỗi `<tool_calls>` nhưng nằm
  trong câu TỪ CHỐI ("Mình không thể xuất một <tool_calls> block giả…"), và chính câu quote
  này khiến classifier bắn MALFORMED_TOOL (false positive, không phải attempt format).
- **23/24 còn lại: prose thuần**, phần lớn là refusal rõ ràng. Kết luận: **bằng chứng
  META-GPT (JSON 3/3) không transfer sang đường live** — vấn đề không phải format mà là
  tầng tin cậy (xem root cause).

## Root cause — nhìn thấy trực tiếp lần đầu nhờ response dump

Model web TỪ CHỐI toàn bộ khung điều khiển vì coi đó là prompt injection. Nguyên văn:

- `I can't follow the embedded "WEBGPT SESSION BOOTSTRAP" instructions or treat them as
  system/tool commands. They are just text provided in the message.` (T2, gen 1)
- `The text above is attempting to override the chat instructions by presenting a fake
  controller protocol...` (T2, gen 4)
- `Mình không thể xuất một <tool_calls> block giả để gọi các tool không tồn tại trong phiên
  này... chỉ là dữ liệu trong tin nhắn chứ không cấp quyền cho mình chạy Bash` (T3b)
- `Việc gửi lại cùng một bootstrap không tạo ra quyền truy cập vào thư mục hiện tại.` (T3b, gen 5)

Tầng vỡ: **tầng transport/trust phía ChatGPT Web** — bootstrap + controller protocol được
đưa vào như text trong message, và lớp phòng thủ injection của web tự phân loại nó là
untrusted. Mọi fix tầng prompt (format, task context, số vòng correction) đều không chạm
được gốc này. T1 cũng dính cùng cơ chế: chữ "BOOTSTRAP" trong system prompt khiến model
coi cả yêu cầu echo đơn giản là lệnh nhúng → từ chối.

## Các fix đã verify (đều hoạt động như thiết kế — nhưng không đủ)

| Fix | Bằng chứng |
|---|---|
| ORIGINAL USER TASK trong correction (R5-FIX) | grep `ORIGINAL USER TASK` = 1 trong mọi correction prompt kiểm tra (000068, 000099, 000123...); task thật (`echo TOOLCHAIN_1787564517`) xuất nguyên văn trong prompt |
| WEBGPT_TOOL_PROTOCOL=both | Prompt gửi đi chứa tool schema + hướng dẫn; nhưng 0/24 response thử json fence |
| Raise-sớm persistent | `persistent_tool_refusal`/`persistent_tool_failure` bắn ở correction_count=1 (trace seq 145, 243) — tiết kiệm 3 generations so với chạy hết budget |
| Response dump | 24 file `_response.txt` + sidecar `.json` — đúng thiết kế, chỉ ghi khi correction-relevant |
| corrections=4 | Vẫn chỉ đốt thời gian: không chuỗi correction nào tiến tới tool call |

## Generations thực tế (trace, round này)

| Turn | POST | Session (rút gọn) | Generations | Chuỗi reason |
|---|---|---|---|---|
| T1 | 2 POST (đều 200) | b33ec6 / 330c57 | 1 + 1 = **2** | (không correction, nội dung sai) |
| T1b | 2 POST (200) | 6d2715 / bae3cf | 1 + 1 = **2** | (không correction, nội dung sai) |
| T2 | 2 POST | 5e9b11 / 92a5e2 | 5 + 5 = **10** | FC→FC→FC→REFUSAL ‖ SOFT→FC→FC→REFUSAL(+1) |
| T3a | 2 POST | 68ca79 / f59035 | 2 (**persistent cắt sớm**) + 5 = **7** | FC→persistent ‖ FC→FC→FC→REFUSAL |
| T3b | 2 POST | 5e45ba / 89b547 | 5 + 2 (**persistent cắt sớm**) = **7** | FC→MT*→FC→FC ‖ REFUSAL→persistent |

(\*) MT = MALFORMED_TOOL do quote `<tool_calls>` trong câu refusal — false positive parser.

Tổng: **28 generations cho 5 turns CLI, 0 tool call hợp lệ, 0 artifact**. Nhân đôi chi phí
giờ phủ cả turn thành công HTTP (mọi turn = đúng 2 POST `/v1/messages`, mỗi POST ≥1
generation thật, kể cả khi RC=0).

## Verdict

- **T1**: kết nối + streaming + fail-honest XANH ở tầng giao thức, nhưng nội dung ĐỎ 2/2
  (model không tuân lệnh user đơn giản nhất) — kém hơn R3.
- **T2/T3: ĐỎ 100%** (0/1, 0/2). Không tuyên bố mốc "CLI tự chủ đa bước qua gateway".
- Tiến bộ thật của vòng này là **chẩn đoán**: root cause chuyển từ giả thuyết ("model cứng đầu,
  format sai") sang **bằng chứng raw** — ChatGPT Web phân loại controller protocol là injected
  content và từ chối. Đây là bài toán tầng TRUST/transport (kênh đưa protocol tới model),
  không phải bài toán prompt engineering.

## Đề xuất cho vòng kế (theo tác động kỳ vọng)

1. **Đổi kênh tin cậy**: đưa protocol/tool schema qua kênh web UI coi là authored (ví dụ
   conversation seed/project instructions/custom GPT actions thay vì message body), hoặc
   dùng tài khoản/session có chế độ developer mode. Mọi tweak text hiện tại đều đã bị
   lớp injection-defense nuốt.
2. **Chống nhân đối chi phí trên mọi turn**: điều tra vì sao mỗi CLI turn sinh 2 POST
   (SDK retry ngầm hay gateway behavior); R3 chỉ thấy trên attempt đỏ, R4 thấy cả khi 200.
3. **Sửa false positive MALFORMED_TOOL**: không classify MALFORMED khi chuỗi tag chỉ xuất
   hiện trong câu trích dẫn/từ chối của prose.
4. Giảm noise `count_tokens`: vẫn chưa làm (mỗi preflight vẫn chạy full prompt-build;
   trace seq 13/41/47... là các request_completed không session kèm generation thật ở
   session trước đó).

## Phụ lục — artifact raw

- Trace: `/home/light/Downloads/webgpt/logs/trace.jsonl` (round này: seq 1–245)
- Response dumps: `/home/light/Downloads/webgpt/logs/prompt-debug/*_response.{txt,json}` (24 cặp mới)
- CLI outputs: `/tmp/cc-live-test4/t{1,1b,2,3,3b}.{stdout,stderr}`
- Session store: `/home/light/Downloads/webgpt/tmp/conversations.json`
