# Live CLI Verification — Round 5 (2026-08-24) — STEALTH PROTOCOL (`WEBGPT_TOOL_PROTOCOL=soft`)

Vòng đo quyết định T2/T3 lần đầu chạy với giao thức **soft/stealth**: không khai báo tool schema
trong prompt, không banner; thay bằng 1 câu giao kèo ngắn append cuối turn đầu
(*"When my setup needs a shell action, reply with just `<cmd>the exact shell command</cmd>` and
nothing else — I'll run it and paste the output back."*), correction soft có task context.
Probe trước đó cho thấy model emit đúng tag này 2/2 khi được giao kèo kiểu đó.

- Repo: `/home/light/GitHub/gpt` — KHÔNG sửa code.
- Gateway: `http://127.0.0.1:18000`, env xác minh qua `/proc/<pid>/environ`:
  `WEBGPT_TOOL_PROTOCOL=soft`, `WEBGPT_MAX_CORRECTIONS=4`, `WEBGPT_PROMPT_DEBUG_DIR` bật.
  Healthy đầu và cuối (`ok:true`, browser ready).
- Thư mục test: `/tmp/cc-live-test5` (sạch), task.md fizzbuzz tiếng Việt y hệt R1–R4.
- Turn CLI đã dùng: **7/8** (T1 ×1, T2 ×1, T3 ×3 = đúng trần 2 retry) + **2 turn chẩn đoán**
  (T-D, T-D2) + 1 replay curl không tốn turn. Env ép tường minh
  `ANTHROPIC_BASE_URL=http://127.0.0.1:18000`.
- Trace round này: `/home/light/Downloads/webgpt/logs/trace.jsonl` section seq 1–285 (bản ghi
  mới nhất sau lần rotate cuối).

## Verdict nhanh

| Mức | Kết quả |
|---|---|
| T1 kết nối & streaming | **PASS** lần 1, ~11s, stdout `"AY OK GATEWAY OK"` sạch |
| T2 tool use 1 bước | **PASS** lần 1 (~128s): `TOOLCHAIN_1787567417` khớp token, vòng ``<cmd>`` → transpile `tool_use` → Bash thật → feed lại `WEBGPT_TOOL_RESULT` chạy trọn vẹn, **0 correction** |
| T3 đa bước tự chủ | **FAIL 0/3** — KHÔNG phải vì model: ở lần thử tốt nhất (T3c) model emit ĐỦ cả ``<cmd>cat task.md</cmd>`` LẪN ``<cmd>cat > fizzbuzz.py <<'PY'… + python3 fizzbuzz.py > output.txt</cmd>``; bước tạo file bị **tầng pipeline làm rơi** (không thực thi trên máy, không có trong replay sau đó) |

→ **Không tuyên bố mốc "CLI tự chủ đa bước qua ChatGPT Web — STEALTH PROTOCOL".**
Mốc T2 (tool-use đơn bước) qua soft protocol: **ĐẠT**. T3 đỏ vì một bug pipeline xác định
(reproduce 3/3), không còn là bài toán hành vi model như R3/R4.

## Bảng so sánh R3 / R4 / R5 từng mức

| Mức | Round 3 (schema + correction có ví dụ) | Round 4 (protocol=both + task context + discover-first) | **Round 5 (SOFT stealth)** |
|---|---|---|---|
| T1 kết nối & streaming | PASS lần 1, 16s, sạch | ⚠️ RC=0 nhưng SAI NỘI DUNG ×2 (model từ chối echo, coi là injection) | **PASS lần 1, ~11s, sạch** — handshake mềm không còn kích hoạt injection-defense |
| T2 tool use 1 bước | FAIL ×2 (FALSE_COMPLETION/MALFORMED, cạn budget) | FAIL (10 generations, 0 tool call, 502 budget exhausted) | **PASS lần 1** — 4 generations, tool_call parse thành công ở gen đầu, 0 correction, kết quả shell thật |
| T3 đa bước tự chủ | FAIL ×2 (không artifact) | FAIL ×2 (TOOL_REFUSAL persistent, không artifact) | **FAIL ×3** (không artifact) — nhưng lần 3 chứng minh model emit ĐỦ cả lệnh đọc VÀ lệnh tạo-file+chạy-script; vòng lặp mới là nơi làm rơi bước tạo file |
| Artifact fizzbuzz.py/output.txt | Không | Không | **Không** (điểm gãy: bước Write/run sau bước read đầu tiên) |
| Output nhiễm bẩn | Sạch | Sạch (nhưng nội dung sai T1) | Lỗi lại ở T3a: **stdout nhân đôi** toàn bộ prose (R1-era bug tái xuất); T1/T2/T3c sạch |
| Fail-honest / raise-sớm | persistent cắt sớm OK | Hoạt động đúng | Correction soft **hoạt động đúng khi được trigger** (T-D2: `tool_correction` bắn 1 lần trên turn delta, gen sau correction emit tool_call thành công); T3a/b không trigger vì honest-deflection không phải false claim |
| Nhân đôi chi phí 2 POST/turn | Confirmed trên attempt đỏ | Mọi turn kể cả RC=0 | **Giải quyết ở mức giao thức**: T1/T3a/T3b đúng 1 POST; các POST thừa còn lại ở T2/T3c/T-D/T-D2 là round-trip tool-loop hợp lệ, không phải SDK retry |

## Generations / POST theo turn (trace + conversations.json)

| Turn | POST | Web sessions | Generations | tool_calls parse được | Correction | Kết quả |
|---|---|---|---|---|---|---|
| T1 | 1 | 1 (70f35f90) | 1 | 0 | 0 | PASS — echo chính xác |
| T2 | 4 | 3 (1d964ba7, b8752177×2, d79cc3d3) | 4 | 2 | 0 | **PASS** — token thật |
| T3a | 1 | 1 (feb2cd6e) | 1 | 0 | 0 | FAIL — prose xin task.md, xin-clarification |
| T3b | 1 | 1 (b3deec1a) | 1 | 0 | 0 | FAIL — cùng pattern, 166 chars |
| T3c | 4 | 3 (add0f7ff, b3266204×2, d201bdb3) | 4 | 3 | 0 | FAIL — đọc được task.md, heredoc tạo-file bị rơi |
| T-D (diag) | 4 | 3 (cf3fbd6f, 5958de8b×2, 38304422) | 4 | 3 | 0 | FAIL — bước 1 chạy thật (`d/one.txt`), lệnh `cp … two.txt` bị rơi |
| T-D2 (diag) | 4 | 3 (5aea2699, fe85ae5d×2, 2f2e4b49) | 4 (+1 gen correction) | 3 | **1** | FAIL — cùng pattern; correction soft nổ đúng và gen sau correction vẫn emit tool_call mà vẫn mất |

Tổng: **19 web generations / 7 turns CLI**, giảm mạnh so với 28 gens/5 turns toàn fail (R4) và
37 gens/5 turns toàn fail (R3). Correction: chỉ 1 lần (T-D2) — hoạt động đúng thiết kế. Hai
thất bại im lặng kiểu mới cần lưu ý: (i) T3a/b honest-deflection (model nói thật là chưa đọc
được file → classifier không thấy false-completion → RC=0 với câu trả lời vô hành động);
(ii) vòng tool thứ 2 rơi âm thầm (xem mục Chẩn đoán bổ sung) → CLI kết thúc "thành công"
trong khi task dở dang.

## Bằng chứng raw từng turn

### T1 — PASS
- Prompt gửi đi (dump `000003_wgs_70f35f9054ea_pre_gpt.txt`): `tool_schema_chars: 0`,
  `tool_protocol: "soft"`, `soft_handshake_appended: true`. Đúng thiết kế stealth.
- stdout nguyên văn: `AY OK GATEWAY OK`.

### T2 — PASS
- Gen 1: `finish_reason: "tool_calls", tool_calls: 1` — model emit ``<cmd>echo TOOLCHAIN_1787567417</cmd>`` ngay lập tức, 0 correction.
- Gateway transpile thành Anthropic `tool_use` (Bash) → CLI thực thi thật trên máy → kết quả
  `TOOLCHAIN_1787567417` quay lại web dưới dạng `WEBGPT_TOOL_RESULT {"id":"call_bffffb…","content":"TOOLCHAIN_1787567417"}`
  + dòng *"Continue reasoning from this authoritative controller result."* (dump `000044_…pre_gpt.txt`).
- stdout: chứa đúng token. Điểm trừ nhỏ: session web thứ hai **phát lại** cmd giống nhau trong lúc
  replay transcript (gen 2 của b8752177) trước khi nhận authoritative result → 1 generation thừa/tool-step.

### T3a/T3b — FAIL (prose deflection, không emit tag)
- Cùng prompt tự chủ ("Đọc file task.md …"). Model trả 378/166 chars prose thuần,
  `tool_calls: 0`: "Mình chưa có quyền truy cập trực tiếp vào thư mục /tmp/cc-live-test5 hay file
  task.md trong phiên chat này…" + gợi ý code block. Handshake ``<cmd>`` bị bỏ qua hoàn toàn.
- Không correction nào bắn (không phải false-completion — model thành thật nói không làm được);
  CLI nhận RC=0 ⇒ thất bại im lặng kiểu mới: **honest-deflection, silent fail**.
- T3a stdout bị nhân đôi toàn bộ nội dung (reconcile/history ghép draft+final như R1).

### T3c — FAIL nhưng Model ĐÃ emit đủ cả 3 lệnh (điểm gãy ở tầng pipeline, không phải model)
Prompt có mệnh lệnh shell trực tiếp ("Bắt đầu ngay: chạy lệnh đọc nội dung file task.md").
Chuỗi thật từ trace + conversations.json + prompt dumps:
1. Gen 1 (add0f7ff): `tool_calls: 1` → ``<cmd>cat task.md</cmd>`` → **thực thi thật**, nội dung
   task quay về đúng nguyên văn.
2. Session web mới (b3266204) replay transcript → model phát LẠI cat (gen thừa do replay).
3. Turn delta (affinity-hit, tail=1, dump `000125_…pre_gpt.txt`, 376 chars): sau khi nhận
   `WEBGPT_TOOL_RESULT` chứa nội dung task, model emit tiếp
   `<cmd>cat > fizzbuzz.py <<'PY' … PY` + `python3 fizzbuzz.py > output.txt</cmd>` —
   **đúng lệnh tạo file + chạy script mà task yêu cầu** (`tool_calls: 1` tại seq 134,
   arguments lưu trong conversations.json record `wgs_b3266204…`).
4. **Lệnh này KHÔNG BAO GIỜ được thực thi**: tìm toàn bộ filesystem (`find / -xdev`) không có
   `fizzbuzz.py`/`output.txt` sinh trong window này; parser đã parse OK nên đây không phải
   MALFORMED; không correction nào bắn.
5. POST kế tiếp mở session web MỚI (d201bdb3) replay transcript **thiếu nguyên round heredoc**
   (dump `000138`: chỉ có assistant tc `cat task.md` + result) → model nhìn lịch sử "vừa cat
   xong rồi im" và kết thúc bằng prose *"Tôi không có quyền thực thi shell trong phiên hiện tại
   nên không thể tự tạo/chạy file thật"* + code-block gợi ý.

⇒ Điểm gãy chính xác: **hop giữa gateway-response của turn delta (gen emit tool_use NGAY SAU
khi feed WEBGPT_TOOL_RESULT qua kênh affinity-delta) và vòng lặp CLI** — tool_use không bao giờ
được commit vào lịch sử client: không thực thi, không replay, vòng lặp rơi ra prose. Serial hóa
SSE phía gateway kiểm tra code là đúng chuẩn (`input_json_delta` dùng `json.dumps` escape
newline); timeline 30ms giữa POST-delta và POST-sau nghiêng về việc client-side không commit
round này (SDK retry sang thread mới) chứ không phải gateway build sai payload — cần instrument
thêm phía client để phân định cuối cùng. Câu hỏi mở thứ hai: python trong heredoc bị mất thụt
đầu dòng (arguments lưu có `for i…:\nif i % 15 == 0:` — kể cả được chạy cũng IndentationError);
không có raw `_response.txt` (0 correction nổ) nên chưa xác định được model viết flat hay tầng
nào flatten.

## So với probe tiền nghiệm

Probe: emit ``<cmd>`` 2/2 với giao kèo + yêu cầu-lệnh-trực-tiếp. Live:
- Khớp probe khi prompt có mệnh lệnh trực tiếp: T2 gen 1 và T3c gen 1 đều emit ngay (2/2).
- Không khớp khi prompt tự chủ thuần: T3a/T3b emit 0/2. **Handshake mềm chỉ chắc chắn ăn với
  action đầu tiên được mệnh danh rõ ràng; với task tự chủ thuần model deflected về prior
  chatbot.**
- Đóng góp lớn nhất của soft protocol: T3c chứng minh model CÓ THỂ tự khởi xướng cả lệnh tạo
  file heredoc nhiều dòng sau khi được cấp kết quả — điều chưa từng thấy ở R3/R4 (0 tool call
  hợp lệ trên 65 generations). Vấn đề còn lại chuyển hoàn toàn về tầng pipeline/loop.

## Chẩn đoán bổ sung — khoanh vùng chính xác bug vòng lặp (không tốn thêm code change)

### T-D (turn 6): 2 lệnh tuần tự thuộc nhau
Model emit ``<cmd>mkdir … && date > one.txt && … && cp one.txt two.txt …</cmd>``, bước 1 chạy
thật (`d/one.txt` sinh ra, output thật feed lại), lệnh copy bước 2 bị rơi y hệt T3c:
`two.txt` không tồn tại, session cuối nhận transcript thiếu round cp, model kết thúc
*"Tiếp tục Bước 2… Bạn muốn tôi chạy lệnh copy ngay hay chỉ đưa câu lệnh shell?"* — RC=0.

### T-D2 (turn 7): như T-D + `ANTHROPIC_LOG=debug` phía client
- Trace gateway cho thấy **correction soft BẮN THẬT** trên turn delta (`tool_correction`,
  correction_count=1, seq 247) và gen sau correction parse được `tool_calls: 1` (lệnh cp) —
  tức correction pipeline dưới protocol soft hoạt động; thất bại nằm SAU điểm parse.
- Client log xác nhận flow: request #2 (non-stream JSON) nhận `stop_reason: "tool_use"` ✓
  (bước 1 thực thi, `d2/a.txt` sinh ra); request #3 là turn delta có correction → tool_call
  cp KHÔNG bao giờ thành execution (`b.txt` không tồn tại); request #4 client gửi lại full
  transcript thiếu round cp và nhận `end_turn` prose. **Reproduce 3/3 (T3c, T-D, T-D2).**

### Curl replay payload delta (không tốn turn CLI)
Gửi đúng cấu trúc `[user task, assistant tool_use, user tool_result]` lên `/v1/messages`:
web thread mới trả **prose thuần** ("Bước 1 hoàn tất… tiếp tục Bước 2: Bash\ncp … Bạn muốn
xác nhận kiểu nào?") — `finish_reason: stop, tool_calls: 0`. Cùng với việc mọi web session
sau turn đầu đều là thread MỚI mà `soft_handshake_appended: false`, điều này giải thích nửa
"hành vi" của vấn đề: **các thread kế tiếp không còn thấy giao kèo**, model chỉ còn nhớ quy
ước qua ví dụ tool_call nằm trong transcript replay.

### Tổng hợp hai lỗi chồng lớp
1. **Lỗi A (pipeline — kills multi-step):** tool_use parse được trên turn feedback-delta
   (`_record_for_pending_tool_results` → `append_to_session=True`) không bao giờ tới vòng thực
   thi client — 3/3 reproductions, kể cả khi có correction trung gian. POST sau luôn là full
   transcript THIẾU round đó → model mất mạch → prose finale → RC=0 im lặng.
2. **Lỗi B (prompt/hành vi — làm xấu nhưng không phải gốc):** thread web mới sau turn đầu không
   có handshake và thường deflected về prose-gợi-y-lệnh khi lịch sử kết thúc bằng tool_result;
   correction soft cứu được trường hợp này (T-D2) nhưng sản phẩm của correction vẫn chết vì lỗi A.

## Đề xuất cho vòng sửa (theo thứ tự tác động kỳ vọng)

1. **Sửa lỗi A trước hết** — instrument đường trả response của nhánh pending/append_to_session
   (`server.py::_complete_anthropic`, `_record_for_pending_tool_results`): dump response dict
   TRƯỚC `response_to_anthropic` và SSE sau chuyển đổi cho mọi turn (không chỉ khi có
   correction), chạy lại T-D để bắt chính xác hop làm mất tool_use. Cờ hiệu chỉnh nhỏ có thể
   thử: tắt delta-mode (luôn replay full) để đối chiếu.
2. **Handshake lặp lại trên MỌI thread mới**: hiện `soft_handshake_appended` chỉ true ở turn
   đầu, mà mọi POST sau đều mở web thread mới → thêm 1 dòng nhắc ``<cmd>`` vào cuối mỗi prompt
   replay (vẫn mềm, không phải protocol block nên ít nguy cơ injection-classifier).
3. **Silent-fail T3a/b**: cân nhắc signal `no_tool_action_on_task_prompt` (task yêu cầu hành
   động + response thuần prose hỏi-ngược → bắn soft correction).
4. **Replay re-emit**: gen thừa phát-lại-lệnh ở mỗi session mới (1 generation/tool-step) —
   hết lỗi A + giữ thread context sẽ tự giảm.
5. **Thụt đầu dòng heredoc**: xác minh model emit flat hay pipeline flatten (cần raw dump);
   nếu model, thêm ví dụ heredoc vào correction prompt.
6. **Trùng lặp stdout T3a**: kiểm tra path ghép draft+final trên browser transport (bug R1 tái
   xuất khi response ngắn).

## Phụ lục — artifact raw

- CLI outputs: `/tmp/cc-live-test5/t{1,2,3}.{stdout,stderr}`, `/tmp/cc-live-test5/t3{a,b}.stdout`
  (t3a = attempt 1), `/tmp/cc-live-test5/td{,2}.{stdout,stderr}` (T-D2 stdout chứa ANTHROPIC_LOG
  debug client-side), `/tmp/cc-live-test5/t2.token`, `/tmp/cc-live-test5/task.md`,
  `/tmp/cc-live-test5/delta_payload.json` + response curl (trong log phiên)
- Filesystem evidence: `/tmp/cc-live-test5/d/one.txt` có mà `two.txt` không;
  `/tmp/cc-live-test5/d2/a.txt` có mà `b.txt` không — bằng chứng trực tiếp lỗi A
- Trace: `/home/light/Downloads/webgpt/logs/trace.jsonl` — section cuối (seq 1–285) cho round này
- Prompt dumps round này: `/home/light/Downloads/webgpt/logs/prompt-debug/000003_wgs_70f35f9054ea*`,
  `000016_wgs_1d964ba7*`, `000028_wgs_b8752177*`, `000044_wgs_b8752177*`, `000057_wgs_d79cc3d3*`,
  `000073_wgs_feb2cd6e*`, `000085_wgs_b3deec1a*`, `000097_wgs_add0f7ff*`, `000109_wgs_b3266204*`,
  `000125_wgs_b3266204*` (delta heredoc), `000138_wgs_d201bdb3*` (replay thiếu round),
  `000272_wgs_628d7f6c*` (curl replay). `_response.txt` mới duy nhất liên quan correction:
  T-D2 delta turn (`tool_correction` seq 247–252).
- Session store: `/home/light/Downloads/webgpt/tmp/conversations.json` (records `wgs_1d964ba7…`,
  `wgs_b8752177…`, `wgs_add0f7ff…`, `wgs_b3266204…`, `wgs_d201bdb3…`, `wgs_cf3fbd6f…`,
  `wgs_5958de8b…`, `wgs_38304422…`, `wgs_fe85ae5d…`, `wgs_2f2e4b49…`)
