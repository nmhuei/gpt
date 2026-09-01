# PARITY-DELTA-AUDIT — đối chiếu audit gốc 2026-08-24 với code hiện hành

- Ngày: 2026-08-26
- Phạm vi: `/v1/messages` + `/v1/messages/count_tokens` phục vụ Claude Code CLI; READ-ONLY (không đụng gateway 18000, không full pytest — chỉ chạy targeted suites).
- Đầu vào: `docs/reports/api-parity-audit-2026-08-24.md` (ma trận 37 hàng, P0×2 + P1×9) và `docs/reports/verify-fromscratch-2026-08-25/parity-claims.md`.
- Phương pháp: mọi item kiểm chứng bằng grep/đọc code thật + chạy targeted tests (`test_usage_estimation.py` + `test_count_tokens_align.py` + `test_image_placeholder.py` = 31 passed; `test_claude_code_conformance.py` = 15 passed). Số dòng lấy tại working tree 2026-08-26.

---

## 1. Bảng delta theo từng item (cũ → hiện tại)

Legend cũ = audit 08-24 (sau điều chỉnh bởi parity-claims 08-25). Evidence = working tree hiện tại.

### 1.1 SSE event stream

| Item | Cũ | Hiện tại | Evidence |
|---|---|---|---|
| `message_start` skeleton | OK | **OK+** — giờ kèm `usage` từ StreamUsageEstimator (input > 0 ngay từ đầu) | `gpt/gateway/server.py:1516-1531`; `gpt/api/server.py` tương đương |
| `text_delta` streaming | PART | **OK** — sieve protocol-aware giữ từ opener đầu (`<cmd>/<json>/<WEBGPT_TOOL_CALL>/DSML/<tool_calls>`), prose thường stream progressive | `gpt/gateway/server.py:1544-1550` (emit_openers), on_delta `:1590-1600` |
| Remainder reconciliation sau finalize | (chưa có) | **OK** — text bị giữ lại do opener được re-emit đúng một lần khi prefix khớp | `gpt/gateway/server.py:1657-1665` |
| Block sau text (tool_use) vẫn phát | PART | **OK** — R5 BUG-A hardening, `_anthropic_block_events(payload, start_index=1)` | `gpt/gateway/server.py:1669-1710` |
| `input_json_delta` progressive | PART | **PART (không đổi)** — vẫn 1 cú `partial_json` duy nhất sau finalize | `gpt/gateway/server.py:1883-1884` |
| `message_delta` stop_reason+usage | PART | **OK** — `usage` từ estimator tích luỹ cả delta lẫn held-back blocks; error path cũng phát usage trước terminator | `gpt/gateway/server.py:1687-1697`, `1779-1804`, fallback `:1833-1837` |
| `ping` | PART | **PART (không đổi)** — vẫn SSE comment `": ping\n\n"` thay vì `event: ping`; có cờ finished chặn ping sau terminator (LIVE-F1) | `gpt/gateway/server.py:1633`, `gpt/api/server.py:1385` |
| Lỗi giữa stream | OK | **ĐỔI THEO THIẾT KẾ (R4-DOUBLING)** — lỗi đến sau khi HTTP 200 không còn phát `error` event mà đóng sạch bằng `content_block_stop → message_delta(end_turn) → message_stop` để chống retry-storm nhân bản generation | `gpt/gateway/server.py:1715-1738` (`_anthropic_no_retry_close`) |
| Client disconnect huỷ work | OK | **OK** | `gpt/gateway/server.py` finally-cancel (gate 6 test còn xanh) |

### 1.2 Tool use

| Item | Cũ | Hiện tại | Evidence |
|---|---|---|---|
| Tool definitions | OK | **OK** | `gpt/api/protocol_adapters.py:_anthropic_tools` |
| `tool_use` block trả về | OK | **OK** | `gpt/api/protocol_adapters.py` response_to_anthropic |
| `tool_result` (`is_error`) | PART | **DONE** — `is_error` parse ở ingress, render `"is_error": true` vào `<WEBGPT_TOOL_RESULT>` chỉ khi lỗi | `gpt/api/protocol_adapters.py:392-393`; `gpt/utils/promptcompat.py:147-155, 681-682` |
| Parallel tool calls | PART (cấm trừ fan-out regex) | **CẢI TIỆN** — bounded multi-tool: mặc định chấp nhận ≤3 invokes/turn (`WEBGPT_MAX_TOOL_CALLS_PER_TURN`, 1=strict cũ), vượt → correction có thông báo giới hạn; fan-out Agent carve-out giữ nguyên. ROADMAP ghi in-progress chờ đo malformed-rate | `gpt/gateway/runtime.py:632-650` (cap), `:704-740` (carve-out + MULTI_TOOL), telemetry `:1898-1912` |
| Streaming input_json_delta | PART | **PART (không đổi)** | như 1.1 |
| Virtual Write transpile | OK | **OK** | `gpt/utils/toolcall.py` |
| Id correlation / chống replay | OK | **OK** | `_record_for_pending_tool_results` `gpt/gateway/server.py:1445+` |

### 1.3 Request fields

| Item | Cũ | Hiện tại | Evidence |
|---|---|---|---|
| `system` string/blocks | OK | **OK** | `gpt/api/protocol_adapters.py:327-333` |
| `max_tokens` | PART (không enforce) | **PART (không đổi)** — nhận, validate, không cắt; `stop_reason:"max_tokens"` chưa bao giờ phát | synthetic chỉ copy `max_tokens` `gpt/api/protocol_adapters.py:410-411` |
| `temperature` | PART (bỏ im lặng) | **PART (không đổi)** — synthetic body không copy temperature | `gpt/api/protocol_adapters.py:400-411` |
| `top_p≠1/top_k/stop_sequences/metadata` | MISS (silent-drop) | **VẪN MỞ** — `parse_anthropic_request` bỏ qua mọi field lạ của body Anthropic, không 400 không warning; `_KNOWN_FIELDS` chỉ áp cho body synthetic | `gpt/api/protocol_adapters.py:398-412`; grep toàn bộ production code: 0 hit `stop_sequences`/`metadata` |
| `thinking`/extended thinking history | MISS | **VẪN MỞ** — assistant block array lọc `{"text"}` nên thinking/redacted_thinking trong history mất khỏi prompt web | `gpt/api/protocol_adapters.py:349-351` |
| `tool_choice` auto/any/tool | OK/PART | **GIỮ NGUYÊN** — enforce qua correction `MISSING_REQUIRED_TOOL`; nay có layered cap (protocol-shaped min(env,2)) + anti-repeat fail-fast nên thất bại sớm có kiểm soát hơn | `gpt/gateway/runtime.py:752-753`; cap `:984-1008`, anti-repeat `:2085-2110` |
| Beta/oauth headers | OK | **OK** (gate 3 pass) | `tests/test_claude_code_conformance.py::test_gate_3` |

### 1.4 stop_reason

| Giá trị | Cũ | Hiện tại | Evidence |
|---|---|---|---|
| `end_turn` / `tool_use` | OK | **OK** | `gpt/api/protocol_adapters.py:546` |
| `max_tokens` | MISS | **VẪN MỞ** | không có anywhere |
| `refusal` | MISS | **VẪN MỞ** — refusal dai dẳng kết thúc bằng 502 `malformed_model_tool_call` (fail-closed chủ ý), không surface `stop_reason:"refusal"` | `_map_exception` `gpt/gateway/server.py:2393`; raise sau breaker `runtime.py` |

### 1.5 count_tokens

| Cũ | Hiện tại | Evidence |
|---|---|---|
| PART (JSON-dump lệch usage) | **DONE** — cùng đường render với usage: `render_messages(initial=True)` → ceil(chars/4); route trả `{"input_tokens": N}`; suite align 10 pass | `gpt/api/protocol_adapters.py:417-458` (`rendered_request_prompt`, `estimate_anthropic_input_tokens`); route `gpt/gateway/server.py:1334-1337`; `tests/test_count_tokens_align.py` |

### 1.6 usage

| Cũ | Hiện tại | Evidence |
|---|---|---|
| MISS → PART (adapter-only, wire 0) | **DONE WIRE CẢ 2 PROTOCOL × 2 SERVER** — stream: `message_start`/`message_delta` dùng `StreamUsageEstimator`; non-stream: mọi call site thread `prompt_text`; cache fields đầy đủ schema (=0); OpenAI chunk cũng estimate | `gpt/gateway/server.py:1522,1531,1598,1664,1691-1697,1780,1804`; non-stream `gpt/gateway/server.py:1449`, `gpt/api/server.py:1262`; `anthropic_usage()` `gpt/api/protocol_adapters.py`; tests `test_api_server.py:1121-1374` (6 test usage) — chạy targeted PASS |

### 1.7 Content types

| Item | Cũ | Hiện tại | Evidence |
|---|---|---|---|
| Text blocks | OK | **OK** | — |
| Ảnh base64 (message + tool_result) | MISS | **PART→đóng phần "drop âm thầm"** — ingress `_block_sequence_text` giữ thứ tự block, ảnh → `[image omitted: mime ~KB]` (kill-switch `WEBGPT_IMAGE_PLACEHOLDER=0`); base64 vứt tại ingress. Model BIẾT có ảnh nhưng KHÔNG xem được ảnh (chưa upload thật) | `gpt/api/protocol_adapters.py:36-79` (ingress), user `:355`, tool_result `:387`; render `gpt/utils/promptcompat.py:44-107`; `tests/test_image_placeholder.py` 7 pass |
| PDF/document | MISS | **VẪN MỞ** — block `type=document` rơi ra ngoài `_block_sequence_text` → drop im lặng hoàn toàn (kể cả placeholder) | `gpt/api/protocol_adapters.py:60-79` |
| Thinking/redacted_thinking history | MISS | **VẪN MỞ** | như 1.3 |

### 1.9 Errors

| Item | Cũ | Hiện tại | Evidence |
|---|---|---|---|
| Envelope Anthropic | OK | **OK** — `{"type":"error","error":{"type","message"}}` trên mọi error path `/v1/messages` kể cả count_tokens | `gpt/gateway/server.py:273-300`, `:305-310` |
| Status map + x-should-retry/retry-after | OK | **OK+** — `RateLimited → 429 rate_limit_error` (trước đây dừng ở 503/504); browser crash → 503 retryable riêng | `gpt/gateway/server.py:280-295`, `:2382-2410` |
| 529 Overloaded | MISS | **VẪN MỞ** — `status_by_error` không có 529; RateLimited luôn 429 | `gpt/gateway/server.py:281-294` |

---

## 2. Ba điểm hay quên (kiểm tra nhanh bắt buộc)

**(a) Response headers:** CHƯA CÓ. Không có `request-id`, không có `anthropic-ratelimit-*` nào được phát (grep 0 hit cả 2 server). Chỉ có `x-should-retry`, `retry-after` (429), `x-webgpt-session-id`. CLI sống được nhưng mất kênh hiển thị rate-limit/backoff và khó truy vết request.

**(b) Error envelope shape:** ĐÚNG spec Anthropic cho mọi error path đồng bộ (`_anthropic_error`). Ngoại lệ có chủ ý: lỗi đến SAU khi stream mở (HTTP đã 200) bị đóng giả lập turn thành công sạch (`_anthropic_no_retry_close`, R4-DOUBLING — chống SDK retry nhân bản generation web). Hệ quả: CLI không phân biệt được "turn chết giữa đường" với "model trả lời ngắn" — xem gap G3.

**(c) SSE ordering khi correction/failover giữa stream:** PHÁT HIỆN GAP MỚI. `event_task` forward delta được tạo MỘT lần trước vòng correction và chỉ cancel khi thoát loop (`gpt/gateway/runtime.py:1792-1794`, send lại trong loop `:2156`) ⇒ delta của các lần gửi TRƯỚC correction (vd prose FALSE_COMPLETION "I've created X") đã stream tới client. Khi turn sửa lỗi cuối cùng có text khác prefix đã stream, remainder reconciliation rơi vào nhánh else và re-emit TOÀN BỘ final_text (`gpt/gateway/server.py:1657-1665`) ⇒ text trùng/lệch trong cùng content_block index 0. Chưa có test nào phủ kịch bản này.

---

## 3. Gap còn lại xếp hạng theo tác động CLI

| # | Gap | Tác động CLI | Evidence |
|---|---|---|---|
| G1 | Text trùng/lệch khi correction xảy ra sau khi prose đã stream (mục 2c) | Output block đầu có thể chứa prose attempt hỏng + full text turn sau; CLI đọc nội dung sai | runtime `:1792-1794,:2156`; server `:1657-1665` |
| G2 | Ảnh chỉ placeholder, chưa upload thật lên web thread | CLI paste/screenshot/Read ảnh → model chỉ biết "có ảnh", phải tự nói không xem được; flow vision chết | `promptcompat.py:69-90` |
| G3 | Lỗi muộn giữa stream bị hoá thân thành response "thành công" end_turn | CLI tin việc đã xong với output cụt; không có signal lỗi nào trên wire (đánh đổi R4-DOUBLING) | `gpt/gateway/server.py:1715-1738` |
| G4 | `stop_sequences`/`metadata`/thinking-history silent-drop; PDF/document drop hoàn toàn | Thấp hơn G1-G3: CLI chạy mode không-thinking, metadata chỉ là user_id; nhưng PDF là dạng drop câm hoàn toàn | `protocol_adapters.py:349-351,398-412,60-79` |
| G5 | `stop_reason:"refusal"/"max_tokens"` không tồn tại; refusal → 502 | CLI không phân biệt kết thúc bất thường; refusal dai dẳng thành step-fail thay vì end_turn+kèm lời từ chối | `server.py:2393`; `protocol_adapters.py:546` |
| G6 | Không `request-id`/`anthropic-ratelimit-*` response headers | Mất quan sát rate-limit phía client; cosmetic-ops | grep 0 hit |
| G7 | `input_json_delta` 1 cú burst | Cosmetic trừ tool input rất lớn (Write cả file) | `server.py:1883-1884` |
| G8 | `ping` là comment thay vì `event: ping` | Cosmetic (SDK bỏ qua) | `server.py:1633` |
| G9 | Không 529 Overloaded riêng biệt | Nhỏ: 429 hiện tại đã retry-backoff đúng nhờ BACKOFF-BREAKER | `server.py:281-294` |
| G10 | `temperature` không copy vào synthetic (không validate) | Vô hại; audit #bonus cũ | `protocol_adapters.py:400-411` |

Đã đóng so với audit gốc: P0-1 (usage wire) ✅ · P0-2 giảm mạnh (3-layer classifier + layered cap + anti-repeat + breaker, 502 cố ý giữ lại) · P1-1 ✅ · P1-2 ✅ phần drop-câm (placeholder; upload thật còn ngỏ) · P1-3 ✅ bounded (default 3) · P1-4 ✅ is_error · P1-5 ✅ count_tokens align · P1-9 ✅ mixed-sentinel (opener sieve + remainder + block sau text). P1-6 (compact im lặng) giảm nhẹ thực dụng: usage thật khiến CLI tự compact trước khi gateway phải cắt; `[WEBGPT:BUDGET-TRIM]` chỉ nhìn thấy ở tầng prompt. P1-7/P1-8 còn như bảng trên.

---

## 4. Row ROADMAP đề xuất

| ID đề xuất | Mô tả | Effort | Vùng file |
|---|---|---|---|
| STREAM-CORRECT-DEDUP | Chặn text trùng khi correction giữa stream: đánh dấu ranh giới attempt trong session.events (hoặc chỉ bật live-delta sau classification pass đầu), làm remainder reconciliation per-attempt; thêm test FALSE_COMPLETION-mid-stream | M | `gpt/gateway/runtime.py:1792-1794,2156`; `gpt/gateway/server.py:1640-1710` |
| IMAGE-UPLOAD-WEB | Upload ảnh thật lên ChatGPT Web (attachment endpoint) cho path Anthropic + tool_result, fallback placeholder khi fail; ghép chung research với CODEX-IMG-INPUT đang DEFER | L | `gpt/transport/*`, `gpt/api/protocol_adapters.py`, `gpt/utils/promptcompat.py` |
| LATE-FAIL-SURFACE | Khi `started_content == False`, phát `event: error` chuẩn thay vì close sạch (an toàn vì chưa deliver gì); khi đã stream thì giữ close sạch + log metric `late_failure_masked` để đo tần suất | S | `gpt/gateway/server.py:1715-1810` (cả 2 server) |
| ANTHROPIC-FIELDS-EXPLICIT | `parse_anthropic_request`: 400 rõ ràng cho `stop_sequences` non-empty / `thinking` enabled; `metadata` accept-and-ignore có ghi nhận; placeholder cho block `document` | S | `gpt/api/protocol_adapters.py:318-412` |
| STOP-REASON-REFUSAL | Terminal refusal (breaker đã trip) trả 200 `end_turn` + text thay vì 502, hoặc thêm `stop_reason:"refusal"` — cân nhắc lại với dữ liệu breaker mới | M | `gpt/gateway/runtime.py` (raise points), `gpt/gateway/server.py:2393` |
| HEADER-PARITY | Phát `request-id` (echo uuid nội bộ) + `anthropic-ratelimit-*` advisory tĩnh từ breaker state | S | `gpt/gateway/server.py`, `gpt/api/server.py` (middleware/response headers) |
| PING-WIRE | `": ping\n\n"` → `_sse_event("ping", {"type":"ping"})` | S | `gpt/gateway/server.py:1633`, `gpt/api/server.py:1385` |
| JSON-DELTA-CHUNK | Chunk `partial_json` thành nhiều miếng ~512 ký tự để mô phỏng streaming progress | S | `gpt/gateway/server.py:1875-1890` |
| OVERLOADED-529 | Map RateLimited-có-flag-overload → HTTP 529 | S | `gpt/gateway/server.py:281-294,2382+` |

Ưu tiên gợi ý cho wave tới: STREAM-CORRECT-DEDUP → LATE-FAIL-SURFACE → ANTHROPIC-FIELDS-EXPLICIT (ba row S/M rẻ, đóng hết mục 2); IMAGE-UPLOAD-WEB cần research trước (ghép CODEX-IMG-INPUT).

---

## 5. Chấm lại parity

Theo đúng ma trận 37 hàng của audit gốc, cập nhật trạng thái:

| Nhóm | OK | PART | MISS |
|---|---|---|---|
| 1.1 SSE (10) | 7 | 3 | 0 |
| 1.2 Tool use (7) | 5 | 2 | 0 |
| 1.3 Request fields (7) | 3 | 2 | 2 |
| 1.4 stop_reason (4) | 2 | 0 | 2 |
| 1.5 count_tokens (1) | 1 | 0 | 0 |
| 1.6 usage (1) | 1 | 0 | 0 |
| 1.7 Content types (4) | 1 | 1 | 2 |
| 1.9 Errors (3) | 2 | 0 | 1 |
| **Tổng (37)** | **22** | **8** | **7** |

- **Full green: 22/37 ≈ 59%** (audit gốc 18/37 ≈ 49%; parity-claims giữ mốc đó).
- **Chạy được chức năng (OK+PART): 30/37 ≈ 81%** (gốc ≈ 78%).
- Trọng số theo mức CLI thực sự gọi (usage/count_tokens/tool loop/SSE mỗi turn; PDF/thinking/529 hiếm): hiệu lực thực dụng ước **~85-88%**.
- Hai gap mới phát hiện (G1 correction-dup, G3 late-fail masking) nằm ngoài ma trận gốc; nếu tính vào, trừ thêm ~2-3 điểm phần trăm ở nhóm "đúng ngữ nghĩa stream".

Kết luận: hai trụ cột P0 hành vi (usage thật trên wire, count_tokens nhất quán) đã ĐÓNG và có test pin; vòng agent đa bước của CLI chạy đầy đủ. Phần còn lại tập trung vào tính trung thực của stream khi gặp sự cố (G1/G3) và multimodal thật (G2).
