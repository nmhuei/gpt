# Live CLI Verification — Round 6 (2026-08-24) — R6-FIX (delta-stream protocol-aware + soft handshake re-append)

Vòng đo kiểm chứng 2 fix vừa merge: **(A)** delta stream protocol-aware (withhold tag
`<cmd>`/`<json>` khỏi text stream, tool_use luôn tới client) và **(B)** soft handshake tự gắn
lại mỗi khi web thread/conversation mới. **Kết quả then chốt của vòng này là một phát hiện cấu
hình: gateway live đang chạy code STALE** — process khởi động 17:27, còn `server.py`/`runtime.py`
sửa lần cuối 18:39 (pyc recompile 18:40) và **chưa bao giờ được restart**. Mọi con số dưới đây là
hành vi của code pre-FIX-A-final, không phải của R6-FIX.

- Repo: `/home/light/GitHub/gpt` — KHÔNG sửa code.
- Gateway: `http://127.0.0.1:18000`, healthy đầu và cuối (`ok:true`, browser ready). Env xác minh
  qua `/proc/935393/environ`: `WEBGPT_TOOL_PROTOCOL=soft`, `WEBGPT_MAX_CORRECTIONS=4`,
  `WEBGPT_PROMPT_DEBUG_DIR` bật.
- Thư mục test: `/tmp/cc-live-test6` (sạch), task.md fizzbuzz tiếng Việt y hệt R1–R5.
- Turn CLI đã dùng: **5/8** (T1 ×1, T2 ×1, T3 ×3 = đúng trần 2 retry), env tường minh
  `ANTHROPIC_BASE_URL=http://127.0.0.1:18000`. Thêm **2 probe SSE bằng httpx trực tiếp**
  (không tốn turn CLI, không đụng code).
- Trace: `/home/light/Downloads/webgpt/logs/trace.jsonl` — section claude-code seq ~300–560
  (file dùng chung với traffic client khác, lọc theo `client: claude-code`).

## Verdict nhanh

| Mức | Kết quả |
|---|---|
| T1 kết nối & streaming | **PASS** lần 1 (~42s), stdout `AY OK GATEWAY OK` sạch |
| T2 tool use 1 bước | **PASS** lần 1: correction soft bắn đúng trên prose deflection → model emit `<cmd>echo TOOLCHAIN_1787571811</cmd>` → CLI thực thi thật → token quay lại web + stdout. 3 generations, RC=0 |
| T3 đa bước tự chủ | **FAIL 0/3** (không artifact, RC=0 im lặng cả 3). NHƯNG model hợp tác 2/3 attempt; điểm gãy đã khoanh chính xác bằng client debug log + probe SSE |

→ **KHÔNG tuyên bố mốc "Claude Code CLI tự chủ đa bước qua ChatGPT Web".** T3 vẫn đỏ, nhưng đỏ
vì gateway chạy stale code + kiến trúc fallback của client; R6-FIX chưa thực sự được đo. Cần
**restart gateway rồi chạy lại Round 7** trước khi kết luận về fix.

## Bằng chứng gốc rễ mới — 2 phát hiện cấu trúc

### 1. Gateway stale + SSE fail-closed tái hiện đúng symptom BUG-A (probe trực tiếp)

Probe `/v1/messages` với `stream:true` (prompt ép tool call, httpx đọc raw SSE):

```
event: message_start
event: content_block_start   (text, index 0)
event: content_block_delta   {"text_delta":"<cmd>echo S"}     ← TAG THÔ LEAK vào text stream
event: error                 "Soft <cmd> tool call tags are incomplete."
```

Đây chính xác symptom R5 BUG-A mô tả ("leaked raw tags as visible text deltas then failed
closed"). Filter `emit_openers` trong `_anthropic_live_stream` (có đầy đủ trong working tree,
`server.py:1449` trở đi) **không có trong process đang chạy**. Kết hợp mốc thời gian
(process 17:27 vs file mtime 18:39) ⇒ kết luận stale code là chắc chắn.

### 2. Client CLI v2.1.241 LUÔN cặp mỗi logical turn = 1 request SSE + 1 request non-stream JSON

`ANTHROPIC_LOG=debug` phía client (T3c, và đối chiếu `td2.stdout` của R5 — pattern tồn tại từ
trước, không phải regression):

```
req1 stream:true  → mở SSE, gần như lập tức bỏ không đọc
req2 stream:false → JSON, stop_reason:"tool_use"  ← leg này MỚI là leg client tiêu thụ
req3 stream:true  → bỏ
req4 stream:false → JSON, stop_reason:"end_turn"
```

Hệ quả: mọi generation sinh ra trên **leg SSE đều mất** (client không bao giờ đọc); leg JSON
fallback gửi lại transcript theo những gì client biết. Đây là lời giải cho "nhân đôi 2 POST/turn"
từ R4. Tool_use đến client qua **JSON path hoạt động tốt**: T2 echo được execute thật;
T3c `cat task.md` cũng được execute và feed lại (`WEBGPT_TOOL_RESULT call_410eea83…`).

## Chuỗi thất bại T3 (reconstruct trọn vẹn từ trace + prompt dumps + client log)

Lấy T3c (attempt 3, directive prompt) làm chuẩn — 4 request client, 4 generation gateway:

1. **Turn 1**: leg SSE (session `f23a4d05`, gen emit `<cmd>cat …task.md</cmd>` ngay, 0 correction)
   → bị bỏ. Leg JSON (session `23c44ede`) → `stop_reason:tool_use` ✓ → CLI execute `cat`
   (kết quả feed lại qua affinity delta, dump `000518`: nội dung task nguyên văn ở T3a; ở T3c
   result là `"Parser skipped input between top-level statements"` — anomaly cần điều tra riêng:
   lệnh cat đơn giản trả về lỗi parser thay vì nội dung file).
2. **Turn 2**: model (sau MALFORMED correction idx1 khi reply kèm prose quanh tag) emit ĐÚNG MỘT
   command consolidated tạo-file+chạy+cat-output (`000527_response.txt` — heredoc/printf hoàn chỉnh)
   → tool_use parse OK — nhưng nằm trên **leg SSE bị bỏ**.
3. Leg JSON của turn 2 mở web thread MỚI (`6f994839`), replay transcript count=5 (đủ round cat)
   nhưng **`soft_handshake_appended: false`** — handshake không được gắn lại trên thread kế tiếp
   (đúng hành vi cũ BUG-B: append-once). Model nhìn lịch sử kết thúc bằng tool_result, không có
   giao kèo → kết thúc bằng prose kèm code-block gợi ý ("Tôi không có quyền thực thi shell…") →
   `end_turn` → CLI thoát **RC=0 im lặng**, 0 artifact.

T3a (attempt 1) cùng chuỗi y hệt (sessions `6380449c` → `d8f343b3` → delta → `45e540ae`,
handshake false ở thread cuối). T3b (attempt 2): model deflection thuần 258 chars không emit tag,
không correction nào bắn (honest-deflection vẫn là điểm mù classifier — recommendation #3 của R5
chưa làm).

## FIX verification status

| Fix | Trạng thái đo được |
|---|---|
| (A) Delta stream protocol-aware | **CHƯA được đo trên live** — probe chứng minh process đang chạy code cũ (leak + fail-closed). Working tree có filter hoàn chỉnh (`server.py::_anthropic_live_stream`) |
| (B) Soft handshake re-append | **Một phần quan sát được trên stale code**: `soft_handshake_appended: true` trên thread mới KHÔNG có tool round (`373`, `496`) nhưng **false trên thread quyết định có tool round** (`402`, `535`). Logic mới trong `runtime.py::_soft_handshake_needed` chưa chạy |

## Bảng so sánh R4 / R5 / R6 từng mức

| Mức | Round 4 (protocol=both) | Round 5 (soft stealth) | **Round 6 (R6-FIX, stale runtime)** |
|---|---|---|---|
| T1 kết nối & streaming | ⚠️ RC=0 nhưng sai nội dung ×2 (injection-defense) | PASS lần 1, ~11s | **PASS lần 1, ~42s, sạch** |
| T2 tool use 1 bước | FAIL (502 budget exhausted, 10 gens, 0 tool call) | PASS lần 1 (4 gens, 0 correction) | **PASS lần 1** (3 gens, 1 correction TOOL_REFUSAL hoạt động đúng, token thật) |
| T3 đa bước tự chủ | FAIL ×2 (TOOL_REFUSAL persistent) | FAIL ×3 (model emit đủ lệnh ở T3c, pipeline làm rơi) | **FAIL ×3** — cùng kiểu R5 nhưng giờ map được cơ chế: leg SSE mất do stream lỗi/stale + thread cuối thiếu handshake |
| Artifact fizzbuzz.py/output.txt | Không | Không | **Không** |
| Output nhiễm bẩn | Sạch | Sạch (riêng T3a nhân đôi stdout) | T3c stdout chứa log debug lẫn text + **nhân đôi câu final** (bug ghép draft+final tái xuất khi response ngắn); T1/T2 sạch |
| Silent-fail RC=0 | persistent cắt sớm OK | honest-deflection im lặng (T3a/b) | Vẫn im lặng ×3 — chưa có signal `no_tool_action_on_task_prompt` |
| Nhân đôi POST/turn | Mọi turn | Giải quyết một phần | **Giải thích xong gốc rễ**: client CLI tự cặp SSE+JSON mỗi turn; leg SSE là generation lãng phí khi nó kịp chạy |
| Correction soft | Bắn nhưng không cứu được | Hoạt động đúng khi trigger (T-D2) | Trigger đúng 2 lần (T2 TOOL_REFUSAL, T3c MALFORMED) và gen sau correction emit tag thành công |

Generations round này (claude-code): T1 = 1 · T2 = 3 · T3a = 4 · T3b = 1 · T3c = 4 → **13
generations / 5 turns**, 4 tool call parse thành công (cao nhất mọi round), 2 executed thật.

## Verdict cuối cùng

- T3 **FAIL 0/3** ⇒ không tuyên bố mốc. Nhưng đây không phải vòng "đo fix thấy fail": **fix A
  chưa hề được nạp vào process đo** (stale since 17:27 vs edit 18:39). Round này giá trị lớn nhất
  là chẩn đoán: (i) bắt được cơ chế nhân đôi POST (client SSE+JSON pairing), (ii) xác nhận JSON
  path + tool transpile + execution loop hoạt động end-to-end (T2 pass, T3c bước 1 pass),
  (iii) xác định hop chết còn lại = thread feedback cuối thiếu handshake ⇒ deflection prose ⇒
  silent RC=0.

## Đề xuất cho Round 7

1. **Restart gateway** (`systemctl --user restart webgpt-gateway` hoặc tương đương) để nạp code
   18:39, verify nhanh bằng chính probe SSE của round này (phải thấy: không leak tag, không error,
   `content_block_start type tool_use` + `message_delta stop_reason tool_use`).
2. Chạy lại T1/T2/T3 nguyên protocol này (turn budget như round). Kỳ vọng: FIX A hết stream error
   (leg SSE có thể thành leg chính), FIX B dạy lại handshake trên thread feedback ⇒ hop chết cuối
   được xử lý.
3. Điều tra anomaly `Parser skipped input between top-level statements` (kết quả sai của lệnh
   `cat` đơn giản ở T3c turn 1 — nghi ngờ phía transpiler/bash-parser của client hoặc gateway ghi
   đè content).
4. Silent-fail: thêm signal `no_tool_action_on_task_prompt` (task hành động + response thuần
   prose hỏi ngược → bắn soft correction) — vẫn còn thiếu, T3b sẽ lặp lại nếu model deflect.
5. Nhân đôi chi phí: cân nhắc gateway phát hiện và dedupe 2 request đồng thời cùng body (hoặc
   chấp nhận như chi phí nền của CLI v2.1.241).
6. Heredoc/printf indentation: model tự sửa được (T3c tự chuyển sang printf 1 dòng sau khi thấy
   lỗi) — ưu tiên thấp.

## Phụ lục — artifact raw

- CLI outputs: `/tmp/cc-live-test6/t{1,2,3}.{stdout,stderr}`, `/tmp/cc-live-test6/t3{b,c}.stdout`
  (t3b/t3c chứa ANTHROPIC_LOG=debug của client), `t2.expect`/`t2.token`, `sse_test.out`,
  `sse_test2.out` (probe SSE raw), `task.md`.
- Filesystem evidence: **không có** `fizzbuzz.py`/`output.txt` trong `/tmp/cc-live-test6/` sau
  3 attempt.
- Trace: `/home/light/Downloads/webgpt/logs/trace.jsonl` — claude-code events seq ~300–560.
- Prompt dumps round này: `000300_wgs_8d4efe27*` (T1), `000312/320/328_wgs_8fe51238*` +
  `000341_wgs_c11b90fb*` (T2), `000357/365_wgs_6380449c*`, `000373/389_wgs_60866aab*`,
  `000402_wgs_371e8f99*` (T3a), `000434-region session 40a04b3c` (T3b), `000496/518/527_wgs_04f027f6*`,
  `000535-region session 6f994839` (T3c).
- Session store: `/home/light/Downloads/webgpt/tmp/conversations.json` (records `wgs_8d4efe27…`,
  `wgs_8fe51238…`, `wgs_c11b90fb…`, `wgs_6380449c…`, `wgs_60866aab…`, `wgs_371e8f99…`,
  `wgs_04f027f6…`, `wgs_034d6780…`).
