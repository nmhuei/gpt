# API Parity Audit — Anthropic Messages API vs ChatGPT Web Gateway

- Ngày: 2026-08-24
- Phạm vi: `/v1/messages` + `/v1/messages/count_tokens` phục vụ Claude Code CLI
- Code đã đọc: `gpt/gateway/server.py` (bản chạy chính, gồm multi-account), `gpt/api/server.py` (bản đơn giản hơn, handler Anthropic giống hệt trừ live-stream), `gpt/api/protocol_adapters.py`, `gpt/api/openai_types.py`, `gpt/requests.py`, `gpt/utils/toolcall.py`, `gpt/utils/promptcompat.py`, `gpt/utils/toolstream.py`, `gpt/utils/assistantturn.py`, `gpt/gateway/runtime.py`.
- Ghi chú kiến trúc: mọi request Anthropic được `parse_anthropic_request()` chuyển thành `ChatCompletionRequest` chuẩn (OpenAI-style), chạy qua `CompletionRuntime` duy nhất, rồi `response_to_anthropic()` / `_anthropic_*_stream` chuyển ngược ra schema Anthropic. Toàn bộ "model" phía sau là ChatGPT Web qua browser; tool protocol là sentinel XML (`<tool_calls><invoke ...>`) nhồi trong prompt text.

---

## 1. Ma trận PARITY theo feature mà Claude Code CLI dùng thực tế

Legend: OK = đạt parity chức năng · PART = chạy nhưng sai lệch có hậu quả · MISS = thiếu hoàn toàn.
File:line mặc định là `gpt/gateway/server.py` nếu không ghi rõ.

### 1.1 SSE event stream

| Feature | Trạng thái | Bằng chứng | Hậu quả cho Claude Code |
|---|---|---|---|
| `message_start` với message skeleton | OK | `_anthropic_live_stream` L1425–1437 yield ngay trước khi chờ browser | CLI thấy stream mở ngay, không timeout khi browser mất vài phút |
| `content_block_start` (text) | OK | L1472–1479, L1495–1502; replay path L1563–1572 | — |
| `content_block_delta` / `text_delta` | PART | Gateway: stream thật qua `ToolStreamSieve` khi có tools (L1440–1456, on_delta L1428–1438). `api/server.py` thì tắt hẳn delta khi có tools (`callback = on_delta if not adapted.request.tools else None`) | Gateway OK. Nhưng text chỉ được forward khi sieve chứng minh "không phải tool block"; phần đầu response bị buffer đến khi phân loại xong → cảm giác latency đầu-tuỳ-thể. Nếu model viết prose rồi mới ra sentinel → `MalformedToolCall` sau khi text đã stream (fail giữa stream) |
| `input_json_delta` streaming tool input từng miếng | PART | L1595–1605: gửi **một lần** toàn bộ JSON trong một `partial_json` event sau khi turn hoàn tất | Đúng schema nên SDK gộp lại được; CLI không chết. Mất ý nghĩa "progressive" — không thể hiện model đang gõ tham số. Với tool input rất lớn (Write cả file), CLI nhận 1 cú burst |
| `content_block_stop` | OK | L1612 | — |
| `message_delta` (stop_reason) | PART | L1613–1620: chỉ `stop_reason` từ payload, `usage.output_tokens` luôn 0 | Xem 1.4 stop_reason và 1.6 usage |
| `message_stop` | OK | L1621–1623 | — |
| `ping` events | PART | L1464: `": ping\n\n"` (SSE comment), mỗi 15s khi im lặng | Hợp lệ theo SSE, SDK bỏ qua; nhưng khác wire-format thật của Anthropic (`event: ping`). Không gây lỗi thực tế |
| `error` event giữa stream | OK | L1540–1542: yield `_sse_event("error", ...)` với envelope Anthropic | CLI nhận lỗi dạng đúng thay vì stream đứt câm |
| Client disconnect huỷ work | OK | L1462–1464 check `is_disconnected`; finally L1544–1551 cancel task | Không giữ worker lease vô ích |

### 1.2 Tool use

| Feature | Trạng thái | Bằng chứng | Hậu quả cho Claude Code |
|---|---|---|---|
| Tool definitions (`tools[]` với `input_schema`) | OK | `protocol_adapters.py` `_anthropic_tools` L120–150 map sang function tools; `toolcall.py` `validate_tools` L560+ | — |
| `tool_use` content block trả về | OK | `response_to_anthropic` L333–356; stream `tool_use` block L1584–1605 | — |
| Tool result turn sau (`tool_result` block) | PART | `parse_anthropic_request` L238–251: `tool_result.content` chỉ lấy text (`_text_blocks(..., {"text"})`); render thành `<WEBGPT_TOOL_RESULT>{"id","content"}` trong prompt text (`promptcompat.py` L170–179). **Bỏ sót:** field `is_error` bị bỏ — CLI đánh dấu tool lỗi nhưng model không thấy flag; ảnh trong tool_result bị drop âm thầm | Model phải "đoán" từ nội dung text rằng lệnh thất bại. Bash/Edit/Read text-only vẫn sống tốt. Screenshot/Read-image từ CLI → model nhận rỗng |
| Parallel tool calls (nhiều `tool_use` trong 1 turn) | PART | `runtime.py` L409–419: >1 call chỉ được chấp nhận khi là fan-out Agent do user yêu cầu tường minh; còn lại correction `"MULTI_TOOL"` ép model gọi đúng 1 call | CLI vẫn chạy vòng lặp bình thường vì nó chấp nhận tuần tự; nhưng mất parallelism thật (mất tốc độ, đổi thứ tự thực thi). Task tool fan-out của CLI bị gateway tự chế lại theo regex heuristic (`_fanout_requested` L120+) — dễ miss các câu yêu cầu fan-out không khớp pattern |
| Streaming input_json_delta | PART | Xem 1.1 | — |
| Virtual Write transpile (gateway thêm tool Write → dịch thành Bash) | OK (mở rộng ngoài parity) | `toolcall.py` L28–47, L585–591 (`effective_model_tools`), `_virtual_write_to_bash` L185+ | CLI chỉ thấy tool nó khai báo; Write ảo giúp model giữ indentation. Cẩn thận: `Write` cho file `.py` buộc `lines` indent-coded — hành vi không có ở API thật nhưng vô hình với CLI |
| Tool-call id correlation & chống replay | OK | `_validate_tool_result_correlation` L1985+; `_record_for_pending_tool_results` L1352+ xử lý CLI resend full transcript | Đúng vòng lặp agent đa turn; duplicate/wrong id bị 409 |

### 1.3 Request fields

| Feature | Trạng thái | Bằng chứng | Hậu quả cho Claude Code |
|---|---|---|---|
| `system` riêng biệt (string hoặc block array) | OK | `protocol_adapters.py` L186–192; render thành `WEBGPT_MESSAGE role=system` trong prompt (`promptcompat.py` L183–188) | System prompt dài của CLI đi trọn vẹn, nhưng chỉ ở turn đầu browser (`web_bootstrapped`); các turn sau chỉ gửi tail + tool contract lặp lại (`runtime.py` L727–730). Model có thể "quên" chi tiết system prompt sâu theo phiên dài |
| `max_tokens` | PART | Nhận và validate (`requests.py` L86–90, `max_tokens_advisory`) nhưng **không bao giờ enforce** — ChatGPT Web không cắt theo token | Output có thể vượt max_tokens của CLI. stop_reason `max_tokens` không bao giờ xuất hiện → CLI không biết output bị cắt (thật ra không bao giờ bị cắt, nên chủ yếu vô hại) |
| `temperature` | PART | Anthropic adapter **không copy** temperature vào synthetic request (`protocol_adapters.py` L253–261 chỉ copy max_tokens) → mặc dù `parse_chat_completion_request` hỗ trợ, giá trị của CLI bị bỏ | Vô hại về mặt crash; CLI không kiểm soát được sampling (Web không cho phép) |
| `top_p` ≠ 1, `top_k`, `stop_sequences`, `metadata` | MISS | `_KNOWN_FIELDS` (`requests.py` L11–33) không chứa `top_k`/`stop_sequences`/`metadata`; adapter không copy → `stop_sequences`/`metadata` bị drop âm thầm (chỉ field trong synthetic body được validate). Nếu ai đó map nhầm sẽ 400, hiện tại là silent-ignore | Claude Code có gửi `metadata.user_id` và thỉnh thoảng `stop_sequences`: silent-ignore, không crash. Stop sequences không hoạt động — model có thể vượt qua điểm dừng client mong muốn |
| `tool_choice` (auto/any/tool) | OK | `_anthropic_tool_choice` L152–172: auto→auto, any→required, tool→function; enforce bằng correction loop (`MISSING_REQUIRED_TOOL`) | `any` không đảm bảo tuyệt đối — phụ thuộc correction budget 2 lần (`runtime.py` L500, L907) rồi 502 `malformed_model_tool_call` |
| `thinking` / extended thinking | MISS | Không có anywhere: grep `thinking` trong api/protocol = 0 hit; `thinking` blocks trong assistant history bị `_text_blocks(content, {"text"})` drop | CLI chạy ở mode không-thinking: OK về chức năng. Nếu CLI bật interleaved thinking với `thinking` blocks trong history, các block đó biến mất khỏi prompt web — context suy luận trước đó mất |
| Beta headers (`anthropic-beta:*`), oauth headers | OK | `_anthropic_request_headers` L123–131 giữ nguyên header `anthropic-*`, `x-claude-code-*`, gắn vào `request_headers` (chỉ metadata, không render) | Gate 3 conformance pass (`tests/test_claude_code_conformance.py`) |

### 1.4 stop_reason

| Giá trị CLI cần | Trạng thái | Bằng chứng |
|---|---|---|
| `end_turn` | OK | `protocol_adapters.py` L358 |
| `tool_use` | OK | L358 |
| `max_tokens` | MISS | Không tồn tại — `max_tokens_advisory` không được dùng để cắt |
| `refusal` | MISS | Không map; refusal của web model bị bắt bằng heuristic text (`_looks_like_tool_refusal`, runtime.py L29–130) và **được correction-loop ép gọi tool**, không bao giờ surface lên CLI như refusal |

Hậu quả: CLI không phân biệt được các kết thúc bất thường; may mắn là 2 giá trị chính (end_turn/tool_use) đúng.

### 1.5 count_tokens

| Feature | Trạng thái | Bằng chứng | Hậu quả |
|---|---|---|---|
| `/v1/messages/count_tokens` | PART | Route L1250–1252 (`gateway/server.py` L2201); `estimate_anthropic_input_tokens` (`protocol_adapters.py` L270–290): parse request → dump JSON normalized → `ceil(bytes/4)` | Ước lượng deterministic, cùng representation với prompt web thật nên nhất quán tương đối. Sai lệch so với tokenizer Claude thật (thường ~3.5–4 char/token nhưng JSON dump kể luôn wrapper role/id → đếm dư wrapper, thiếu tool schema mà prompt web thật sự chèn). CLI dùng cho auto-compact/context % → con số hiển thị lệch, quyết định compact sớm/muộn hơn cần thiết. Không crash |

### 1.6 usage

| Feature | Trạng thái | Bằng chứng | Hậu quả |
|---|---|---|---|
| `usage.input_tokens/output_tokens` thật | MISS | `response_to_anthropic` L360 và mọi message_delta: `{input_tokens: 0, output_tokens: 0}` | **Đây là gap hành vi lớn nhất với CLI hiện đại**: Claude Code dựa vào usage tích luỹ để (a) hiển thị context còn lại, (b) trigger auto-compact gần giới hạn. Với 0 vĩnh viễn, CLI không tự compact; phiên dài chỉ kết thúc bằng prompt-overflow error từ gateway (`ValueError` → 400 "Prompt exceeds WEBGPT_MAX_PROMPT_CHARS") thay vì compact sạch. P0/P1 tuỳ phiên làm việc |

### 1.7 Content types

| Feature | Trạng thái | Bằng chứng | Hậu quả |
|---|---|---|---|
| Text blocks user/assistant | OK | `_text_blocks` L23–32 | — |
| Ảnh base64 (`type:"image"`) trong message hoặc tool_result | MISS | Chỉ extract `{"text"}` (L214, L243); image block bị bỏ, message user chỉ-text-trống thì **không được append gì cả** (L216–217: chỉ append khi `role=="user" and text`) | CLI paste/drag screenshot hoặc Read ảnh → model không thấy gì, có thể trả lời "tôi không thấy ảnh". Không crash. CLI chủ yếu dùng ảnh khi user yêu cầu — P1 |
| Multi-modal PDF/document | MISS | Như trên | Tương tự ảnh |
| Thinking/redacted_thinking blocks trong history | MISS | Drop tại L214 | Xem 1.3 |

### 1.8 Sampling / generation params

`temperature` bị bỏ (xem 1.3); `top_p≠1`, `seed`, `logprobs`, `stop` đều không representable trên Web (`requests.py` L44–85). Không field nào khiến CLI crash vì adapter Anthropic không copy chúng vào synthetic body.

### 1.9 Errors

| Feature | Trạng thái | Bằng chứng |
|---|---|---|
| Envelope `{"type":"error","error":{"type","message"}}` | OK | `_anthropic_error` L246–267; gate 5 test pass |
| HTTP status mapping + `x-should-retry`, `retry-after` | OK | L246–267, L254–256; rate-limit không advertise retry sai (`test_protocol_adapters.py::test_anthropic_rate_limit_does_not_advertise_retry`) |
| Overloaded (529) | MISS | Map tối đa 503/504; CLI retry-backoff coi 503 như lỗi chung — vẫn retry được |

---

## 2. Đặc thù ChatGPT Web khiến parity khó (phân tích)

1. **Không có token count thật.** Web không expose tokenizer hay usage. Gateway đo bằng chars: estimate = `len(json_bytes)/4` cho count_tokens (`protocol_adapters.py` L283–289) và `estimated_tokens=(len(prompt)+3)//4` cho trace (`runtime.py`). Vì prompt web thật khác representation Anthropic (thêm wrapper `WEBGPT_MESSAGE`, tool XML contract ~vài nghìn ký tự lặp mỗi turn), mọi con số chỉ là proxy. Không thể trả usage trung thực nếu không tự tokenize.
2. **Không có stop_reason chuẩn.** Web chỉ trả text. `stop_reason` suy ra từ việc parse sentinel tool block (`finish_reason="tool_calls" if calls else "stop"` — `assistantturn.py` L52). `max_tokens`/`refusal` không tồn tại ở tầng web; refusal thật bị heuristic hoá thành correction loop.
3. **Giới hạn context web << 200K của CLI.** Nút thắt thật là `WEBGPT_MAX_PROMPT_CHARS=200000` ký tự (~50K token) mỗi submit (`runtime.py` L501, raise L805–808), cộng với composer/contenteditable của Web. CLI tưởng 200K token (~800KB) vì usage=0; khi vượt, `compact_messages` (`promptcompat.py` L77–133) cắt deterministically: pin system + user đầu/cuối + tool-call pending, phần giữa mới nhất được nhét theo budget — tức là **mất history im lặng** chứ không phải auto-compact có kiểm soát của CLI.
4. **tool_result phải nhồi vào prompt text.** Web không có message type riêng; result được serialize `<WEBGPT_TOOL_RESULT>{"id","content"}` (`promptcompat.py` L170–179) kèm dòng "Continue reasoning from this authoritative controller result." Hệ quả: (a) mọi non-text (ảnh) mất; (b) `is_error` mất; (c) result lớn bị budget chars bóp; (d) model phải tin sentinel — nên sinh ra correction loop chống "tool refusal" (runtime.py L29–130, danh sách marker thủ công, dễ false-negative với cách diễn đạt mới).
5. **Streaming là mô phỏng.** Deltas thật đến từ DOM mutation của trang Web; tool sentinel phải được `ToolStreamSieve` giữ lại khỏi text stream (nếu leak, CLI sẽ thấy XML tool trong text). Vì vậy text có thể stream, tool JSON thì không bao giờ stream progressive được.
6. **Một invoke mỗi turn** (trừ fan-out Agent): protocol sentinel của gateway cố tình single-call để giảm malformed; Web không có khái niệm parallel function calling đáng tin.

---

## 3. Bảng GAP xếp hạng theo độ phá vỡ Claude Code

### P0 — CLI chết hoặc không hoàn thành việc

| # | Gap | Bằng chứng | Kịch bản phá vỡ |
|---|---|---|---|
| P0-1 | Prompt overflow 400 thay vì auto-compact: usage luôn 0 → CLI không bao giờ tự `/compact`; phiên dài hit `WEBGPT_MAX_PROMPT_CHARS=200000` và nhận 400 `invalid_request_error` giữa task | `runtime.py` L501, L805–808; `protocol_adapters.py` L360 | Session refactor dài (>~45 phút chat) chết giữa chừng với lỗi khó hiểu; user phải tự compact tay |
| P0-2 | Tool-refusal heuristic false-negative → MalformedToolCall 502 sau correction budget 2 | `runtime.py` L29–130, L500, L907–910 | Web model diễn đạt refusal bằng câu chưa có trong marker list → correction loop không sửa được → CLI nhận 502 `malformed_model_tool_call`, step fail. Xác suất tăng theo độ dài phiên (model quên contract) |

### P1 — Mất tính năng phụ, CLI vẫn chạy

| # | Gap | Bằng chứng |
|---|---|---|
| P1-1 | usage tokens = 0 → context % hiển thị sai, không cảnh báo sắp hết context | `protocol_adapters.py` L360, server L1527–1533/L1613–1620 |
| P1-2 | Ảnh/screenshot trong messages và tool_result bị drop âm thầm | `protocol_adapters.py` L214, L243 |
| P1-3 | Parallel tool calls bị cấm (trừ fan-out Agent regex) | `runtime.py` L409–419 |
| P1-4 | `is_error` của tool_result không truyền; model không biết tool fail trừ khi text nói rõ | `protocol_adapters.py` L238–251 |
| P1-5 | count_tokens lệch so với tokenizer thật (JSON-wrapper inflation, thiếu tool-contract inflation) | `protocol_adapters.py` L270–290 |
| P1-6 | History giữa bị `compact_messages` cắt im lặng (không báo client) | `promptcompat.py` L77–133, trace `prompt_compacted` chỉ internal |
| P1-7 | `stop_sequences`, `metadata`, thinking-history bị silent-drop | `protocol_adapters.py` L253–261; `requests.py` `_KNOWN_FIELDS` |
| P1-8 | `any`/`tool_choice` không guarantee tuyệt đối (phụ thuộc correction budget) | `runtime.py` L907 |
| P1-9 | Mixed prose+tool-call fail giữa stream (text đã stream rồi mới raise) | `toolstream.py` finalize `mixed_sentinel`; server L1540 |

### P2 — Cosmetic / quan sát được nhưng không ảnh hưởng công việc

| # | Gap | Bằng chứng |
|---|---|---|
| P2-1 | `ping` là SSE comment thay vì `event: ping` | server L1464 |
| P2-2 | `input_json_delta` gửi 1 cú thay vì từng miếng | server L1595–1605 |
| P2-3 | Replay path chunk text cứng 32 ký tự (non-live stream) | server L1573–1582 |
| P2-4 | `usage.output_tokens:0` trong message_delta; `stop_sequence:null` hardcoded | server L1613–1620 |
| P2-5 | `id` message dạng `msg_<hex16>` không prefix format Anthropic thật (thật cũng msg_ — OK), `model` echo requested_model thay vì model thật | server L1417–1437 |
| P2-6 | Không 529 Overloaded riêng biệt | `_map_exception` server L2055+ |

---

## 4. TOP-10 việc phải fix để Claude Code đạt parity

Mỗi mục kèm cách verify cụ thể.

1. **Trả usage ước lượng thật thay vì 0.** Tính `input_tokens` bằng chính estimator của gateway trên prompt đã render (đã có `(len(prompt)+3)//4` ở `runtime.py` — hãy đưa qua TurnResult → `format_openai_chat_response` → `response_to_anthropic`), `output_tokens` từ text trả về. Verify: `pytest tests/test_claude_code_conformance.py -k usage` (viết test mới assert `usage.input_tokens > 0`); chạy CLI thật `claude --verbose` xem "% context used" tăng dần.
2. **Auto-compact hợp tác: khi gateway sắp compact (trace `prompt_compacted`), trả warning trong response** (vd chèn system-reminder text hoặc giảm max_tokens_advisory) hoặc ít nhất log + metric. Verify: test đẩy 250K chars qua `complete_normalized`, assert không 400 mà response vẫn sinh ra và trace có `prompt_compacted`.
3. **Nới MULTI_TOOL cho phép nhiều invoke khi CLI khai báo nhu cầu song song** (hoặc env flag `WEBGPT_ALLOW_MULTI_TOOL=1`), giữ strict-mode cho certification. Verify: unit test mới — model text chứa 2 invokes Read+Bash → `AssistantTurnBuilder.from_model_text` trả 2 calls; CLI chạy "read these 3 files in parallel".
4. **Truyền `is_error` vào `<WEBGPT_TOOL_RESULT>` payload** (`{"id","content","is_error":true}`) trong `render_messages`. Verify: extend `tests/test_protocol_adapters.py::test_parse_responses_function_output_and_anthropic_tool_result`; assert chuỗi prompt chứa `"is_error": true`.
5. **Preserve image blocks ít nhất dưới dạng placeholder có cấu trúc** — vd `[image block omitted: 1024x768 png, N bytes]` trong tool_result/user content thay vì drop im lặng, để model biết user đã gửi ảnh. Verify: post request có image block → prompt debug (`WEBGPT_PROMPT_DEBUG_DIR=… pytest …`) chứa placeholder.
6. **Map refusal thật ra stop_reason + không correction-loop khi model rõ ràng từ chối lần 2.** Sau correction thứ 2 vẫn refusal → trả `stop_reason:"end_turn"` kèm text thay vì 502. Verify: mock session.send trả refusal text 3 lần; assert response 200 với text, không 502.
7. **count_tokens hiệu chỉnh:** trừ overhead JSON-wrapper, cộng tool-contract chars (`build_tool_instructions`) để sát với prompt web thật; document hệ số. Verify: so sánh `estimate_anthropic_input_tokens(body)` với `len(render_messages(...))//4` trong test property-based.
8. **Silent-drop → explicit reject cho các field CLI thật sự gửi:** nếu body có `stop_sequences` non-empty hoặc thinking enabled, trả 400 rõ ràng ("not supported by ChatGPT Web backend") thay vì ignore — tránh CLI kỳ vọng hành vi không tồn tại. Verify: curl POST với `stop_sequences:["END"]` → 400 invalid_request_error có message rõ.
9. **Stream mixed-sentinel an toàn hơn:** khi `mixed_sentinel` xảy ra, thay vì raise giữa stream, emit `content_block_stop` + `message_delta(stop_reason="end_turn")` + `error` event có cấu trúc. Verify: test hiện có `test_anthropic_tool_stream_keeps_tool_sentinel_out_of_text` — mở rộng assert stream kết thúc sạch bằng message_stop.
10. **Ping đúng wire-format Anthropic:** đổi `": ping\n\n"` thành `_sse_event("ping", {"type":"ping"})`. Verify: `grep -c 'event: ping'` trên stream 20s từ mock backend (`WEBGPT_…mock` mode dùng trong `test_claude_code_conformance.py::test_gate_2`).

Bonus (không nằm trong top-10 nhưng rẻ): copy `temperature` vào synthetic request trong `parse_anthropic_request` để ít nhất validate đúng; expose 529 cho RateLimited.

## 5. Lệnh verify tổng thể

```bash
# Suite conformance hiện tại (gate 1–6)
python -m pytest tests/test_claude_code_conformance.py -q
# Protocol adapters (anthropic round-trip, tool conversion, force-initial-tool)
python -m pytest tests/test_protocol_adapters.py tests/test_messages.py -q
# Agent loop correlation
python -m pytest tests/test_gateway_agent_loop.py -q
# Smoke end-to-end với mock backend (không cần browser):
WEBGPT_LOCAL_MOCK=1 python -m gpt.gateway.server &  # rồi:
curl -s localhost:PORT/v1/messages -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-4","max_tokens":64,"stream":true,
       "system":"You are terse.","messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}],
       "tools":[{"name":"Bash","description":"run shell","input_schema":{"type":"object","properties":{"command":{"type":"string"}}}}]}' \
  | head -40   # kiểm tra thứ tự message_start → content_block_* → message_delta → message_stop
```

Kết luận nhanh: nền tảng đủ để Claude Code CLI chạy vòng lặp bash/edit/read đa turn qua gateway (SSE schema đúng, tool_use hai chiều đúng, correlation chặt). Các gap còn lại tập trung ở **sự thật hoá usage/count_tokens** (P0-1/P1-1), **chống refusal/malformed fail-closed** (P0-2), và **những gì bị drop âm thầm** (ảnh, is_error, parallel calls, stop_sequences).
