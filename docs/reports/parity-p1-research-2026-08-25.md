# Parity P1 Research — gap còn mở sau loạt fix 2026-08-25

- Ngày: 2026-08-25
- Phạm vi: RESEARCH read-only. Đầu vào: `docs/reports/api-parity-audit-2026-08-24.md`, `docs/reports/verify-fromscratch-2026-08-25/parity-claims.md`, đối chiếu code working tree + tra cứu spec Anthropic/OpenAI và cơ chế attachment của ChatGPT Web.
- Loại khỏi danh sách (đã fix, xác nhận bằng code):
  - **Usage wire (P0-1/P1-1)**: `gpt/gateway/server.py:1346,1375,1410` (non-stream thread `prompt_text`), `:1478-1489` (`StreamUsageEstimator` cho `message_start`), `:1655-1662` (message_delta/error path). OpenAI envelope hỗ trợ sẵn `format_openai_usage_chunk`.
  - **Refusal classifier 3 lớp** (`gateway/runtime.py:37-128,137-229,266+`; terminal 502 giữ nguyên by design).
  - **Stealth soft protocol khớp DECISIONS.md**; **P1-9 mixed-prose** đã fix (sieve opener-aware `gateway/server.py:1442-1504`).

---

## 1. Phân tích từng gap còn mở

### P1-4 — `is_error` của tool_result không truyền

- **Hiện trạng**: `parse_anthropic_request` (`api/protocol_adapters.py:330-342`) chỉ đọc `tool_use_id` + text; grep toàn repo `is_error` = 0 hit. Payload render `promptcompat.py:596-603` serialize cứng `{"id","content"}`.
- **Tác thực CLI**: Bash/Edit fail được CLI đánh dấu `is_error:true`. Nội dung thường kèm stderr nên model vẫn đoán được phần lớn; nhưng khi content ngắn/generic, model có thể coi thất bại là thành công → tăng đúng lớp FALSE_COMPLETION mà classifier đang phải chống lại ở phía bên kia. Đây là gap rẻ nhất có giá trị sửa lỗi-chung-quyết.
- **Khả thi trên Web**: hoàn toàn — chỉ là thêm 1 field JSON trong sentinel payload, không đụng giới hạn nào của Web.
- **Task đề xuất**: `P1-4-IS-ERROR` — đọc `is_error` trong nhánh tool_result của `parse_anthropic_request`, truyền qua canonical `role=tool` message, render vào `<WEBGPT_TOOL_RESULT>` payload. File chạm: `gpt/api/protocol_adapters.py`, `gpt/utils/promptcompat.py` (+ `normalize_message` nếu cần field mới). Test: mở rộng `tests/test_protocol_adapters.py` (assert chuỗi prompt chứa `"is_error": true`) + test promptcompat. **Effort: S.**

### P1-2 — Ảnh/screenshot bị drop âm thầm (messages + tool_result)

- **Hiện trạng**: `_text_blocks(content, {"text"})` tại `protocol_adapters.py:309,338` bỏ mọi block không-text; user message chỉ-có-ảnh thì không append gì cả (`:311`). Transport: interface `send()` đã có param `files` (`transport/session.py:463-494`) nhưng `hybrid.py:117-118` raise `"Hybrid transport does not support file attachments."`; curl_transport/browser chưa implement. Grep `upload|attachment|image` trong curl_transport = 0 hit.
- **Tác thực CLI**: paste/drag screenshot vào Claude Code → image block trong user message; Read file ảnh → image block trong tool_result. Hiện tại model nhận rỗng → trả lời "tôi không thấy ảnh", hoặc loop retry vô ích. Toàn bộ workflow screenshot-driven debugging chết im lặng.
- **Khả thi trên Web**: Web hỗ trợ ảnh native qua composer. Trên protocol path, endpoint tồn tại: `POST backend-api/files` (+ biến thể `process_upload_stream` — xác nhận qua search) → `file_id` → tham chiếu attachment trong payload conversation. Chưa ai reverse đầy đủ flow upload này cho repo; rủi ro trung bình (auth/sentinel cho endpoint upload, MIME limits).
- **Tasks đề xuất (2 tầng)**:
  - `P1-2A-IMAGE-PLACEHOLDER` (Effort **S**): thay drop bằng placeholder có cấu trúc `[image omitted: <mime>, N bytes]` trong cả user content lẫn tool_result, để model ít nhất biết user đã gửi ảnh và trả lời tử tế. File chạm: `api/protocol_adapters.py` (2 call site `_text_blocks`), test `tests/test_protocol_adapters.py` + debug prompt.
  - `P1-2B-REAL-IMAGE-UPLOAD` (Effort **L**): implement upload thật trên curl_transport (mint file_id → attach vào conversation payload), mở khoá `files=` cho hybrid, fallback DOM nếu protocol chặn. File chạm: `transport/curl_transport.py`, `transport/hybrid.py`, `transport/session.py`, `token_manager.py` (sentinel cho endpoint mới), tests `tests/test_fault_injection.py` style. Verify: live test gửi PNG nhỏ, model mô tả đúng nội dung.

### P1-3 — Parallel tool calls bị cấm (trừ fan-out Agent regex)

- **Hiện trạng**: `runtime.py:658-660` — >1 call chỉ chấp nhận khi fan-out Agent khớp regex `_fanout_requested` (`:427-444`, bắt "parallel"/"in parallel"); còn lại correction `MULTI_TOOL` ép 1 call. Parser sentinel đã parse được nhiều invoke (`assistantturn.py:35-54` trả list).
- **Tác thực CLI**: system prompt của Claude Code chủ động khuyến khích batch các Read/Grep độc lập trong 1 turn. Với policy hiện tại mỗi batch bị ép thành N turn browser tuần tự (mỗi turn vài giây round-trip) → mất tốc độ trên MỌI phiên, đồng thời correction "exactly one allowed" dạy ngược model khỏi batch giữa phiên. Regex heuristic dễ miss các phát ngữ fan-out không khớp pattern.
- **Khả thi trên Web**: đây là policy nội bộ gateway, không phải giới hạn Web — parser đã hỗ trợ; chỉ là quyết định chấp nhận bao nhiêu call trước khi malformed rate tăng.
- **Task đề xuất**: `P1-3-BOUNDED-MULTI-TOOL` — thay vì correction ngay khi len(calls)>1, chấp nhận tối đa N call (env `WEBGPT_MAX_TOOL_CALLS_PER_TURN`, default 3–5, chỉ áp protocol non-cert); giữ strict-mode cho certification. File chạm: `gateway/runtime.py` (`_tool_correction_issue`), config env, `tests/test_gateway_agent_loop.py` (mock text 2 invokes → 2 tool_use blocks ra CLI). **Effort: M** (phải đo malformed-rate sau bật).

### P1-5 — count_tokens lệch + MỚI: hai estimator tự mâu thuẫn

- **Hiện trạng**: `estimate_anthropic_input_tokens` (`protocol_adapters.py:365-385`) vẫn JSON-dump bytes÷4, không trừ overhead wrapper, không cộng tool-contract/handshake. Sau khi wire usage, `message.usage.input_tokens` giờ dùng rendered-prompt chars÷4 → **endpoint count_tokens và usage trả hai con số khác nhau cho cùng request** (mâu thuẫn mới sinh ra bởi chính fix P0-1).
- **Tác thực CLI**: số liệu hiển thị context %, `/cost`, quyết định compact tay lệch; hai nguồn lệch nhau làm user mất tin tưởng con số.
- **Khả thi**: 100% local, không phụ thuộc Web.
- **Task đề xuất**: `P1-5-COUNT-TOKENS-ALIGN` — đổi estimator count_tokens dùng cùng đường: parse → `render_messages(initial=True)` → chars÷4 (khớp công thức `StreamUsageEstimator`). File chạm: `api/protocol_adapters.py:365-385`. Test: property test so sánh `estimate_anthropic_input_tokens(body)` với `len(render_messages(...))//4` ± tolerance. **Effort: S.**

### P1-6 — Compact giữa history im lặng

- **Hiện trạng**: vượt `WEBGPT_MAX_PROMPT_CHARS` (unit chạy 250000) → `compact_messages` cắt deterministically, trace `prompt_compacted` chỉ internal. Sau wire usage, `input_tokens` ước từ request gốc (trước compact) nên CLI thấy ~≤62K token ≈ 31% window 200K mà nó tin → **auto-compact của CLI vẫn không bao giờ kích hoạt**, gateway-side compact remains silent net.
- **Tác thực CLI**: phiên dài model "quên" chỉ dẫn đầu phiên, CLI không hề hay biết — lỗi khó tái hiện, user mất niềm tin. Không crash.
- **Khả thi**: local hoàn toàn. Không thể ép CLI compact (nó tin window 200K); cách sạch nhất là báo cho model/người dùng biết.
- **Task đề xuất**: `P1-6-COMPACT-NOTICE` — khi TurnResult mang flag compacted (có sẵn vùng `runtime.py:1436`), prepend một text block đầu response: `[webgpt: older history trimmed to fit context]` (cả non-stream lẫn stream, trước sieve). File chạm: `gateway/runtime.py` (flag), `gateway/server.py` (`_anthropic_live_stream` + non-stream), tests mock oversized request. **Effort: M.**

### P1-7 — `stop_sequences` / `metadata` / thinking-history silent-drop

- **Hiện trạng**: `parse_anthropic_request` chỉ copy max_tokens; `stop_sequences`/`metadata`/thinking blocks không bao giờ tới synthetic body. `stop` nằm trong `_KNOWN_FIELDS` nhưng không có enforcement nào (grep 0 hit).
- **Tác thực CLI**: `metadata.user_id` — bỏ được, vô hại. Thinking-history — chỉ đau khi user bật extended/interleaved thinking trong CLI (context suy luận mất giữa phiên). `stop_sequences` — hiếm nhưng khi gửi thì model vượt điểm dừng mong muốn, output sai format.
- **Khả thi**: `stop_sequences` enforce được LOCAL (khác audit tưởng): cắt text tại lần xuất hiện sớm nhất của sequence trước khi trả (giống API thật). Streaming cần buffer đến an toàn. Metadata/thinking: theo audit #8, 400 rõ ràng hoặc marker text.
- **Task đề xuất**: `P1-7A-STOP-SEQ-LOCAL` (Effort **M** — streaming buffer là phần khó): truncate post-turn trong runtime, map stop_reason="stop_sequence" khi trúng. `P1-7B-REJECT-UNSUPPORTED` (Effort **S**): 400 invalid_request_error cho thinking-enabled body thay vì ignore.

### P1-8 — `tool_choice` any/tool không guarantee tuyệt đối

- Tác thực thấp: CLI gần như chỉ dùng auto; khi dùng `any` thì fail mode cuối là 502 sau budget (đã mitigated bởi classifier 3 lớp). **Không đề xuất tick riêng**; nếu làm thì gộp: raise riêng `max_corrections` cho MISSING_REQUIRED_TOOL (Effort S, value thấp).

---

## 2. Trục mới: OpenAI-compat `/v1/chat/completions` vs chuẩn OpenAI

Route có thật: handler `chat_completions` (`gateway/server.py:820-886`), routes `:2446-2447`; kèm `/v1/responses` (:1183), `/v1/models` (:770). Cùng `CompletionRuntime` với route Anthropic (không diverge logic).

| Điểm | Chuẩn OpenAI | Gateway hiện tại | Verdict |
|---|---|---|---|
| Non-stream `usage` | Object luôn có | `"usage": None` (`openai_types.py:41`) | Sai spec, client khoan dung |
| Stream usage chunk | Đúng token khi `include_usage` | Chunk emit NHƯNG 0/0 — `:2167-2168,2207-2208` gọi `format_openai_usage_chunk` KHÔNG truyền `prompt_text`/`completion_text` dù infra hỗ trợ | **Wire thiếu y như P0-1 cũ trên nhánh OpenAI** |
| Streaming tool_calls | Delta progressive có `index` | 1 burst duy nhất có `index` (`:2140-2145`) | Tolerated, clients accumulate OK |
| `finish_reason` | stop/tool_calls | Đúng (`:2146-2154`) | OK |
| MULTI_TOOL policy | Cho phép parallel | Áp dụng chung single-call policy | Như P1-3 |
| Error envelope | `{error:{message,type,param,code}}` | Có thêm `retryable`, thiếu `param` (`server.py:86-106`) | Tolerated |
| `stream_options.include_usage` | Có | Hỗ trợ (`normalized.stream_include_usage`) | OK |

Client thực tế dùng route này (aider/Cline-style) dùng usage để quản context window — usage 0 khiến chúng không bao giờ truncate → lỗi cuối phiên. **Task `OPENAI-USAGE-WIRE`** (Effort **S**): truyền `_request_prompt_text(...)` + text emitted vào 2 call site `format_openai_usage_chunk`; populate `response["usage"]` object trong `format_openai_chat_response` (thread prompt_text qua `complete_normalized` hoặc patch tại `chat_completions:874-876`). File chạm: `gateway/server.py`, `api/openai_types.py`. Test: mở rộng `tests/test_api_server.py` assert usage > 0 cả stream (include_usage) lẫn non-stream.

---

## 3. Xếp hạng TOP-5 (value-parity / effort)

| # | Task | Effort | Vì sao trước |
|---|---|---|---|
| 1 | `P1-4-IS-ERROR` | S | Rẻ nhất, đóng đúng lớp lỗi false-completion mà classifier đang bù thủng sech phía kia; giảm cả FALSE_COMPLETION false-positive. |
| 2 | `OPENAI-USAGE-WIRE` | S | Hoàn tất chuyện P0-1 cho cả hai protocol — infra đã có 100%, chỉ nối wire; mở cửa cho client OpenAI-compat thật. |
| 3 | `P1-3-BOUNDED-MULTI-TOOL` | M | Duy nhất tác động TỐC ĐỘ lên mọi phiên CLI (batch Read/Grep là hành vi mặc định của CLI); parser đã hỗ trợ, chỉ đổi policy + đo malformed-rate. |
| 4 | `P1-2A-IMAGE-PLACEHOLDER` | S | Chấm dứt data-loss âm thầm; bước đệm bắt buộc trước P1-2B upload thật (L). |
| 5 | `P1-5-COUNT-TOKENS-ALIGN` | S | Xóa mâu thuẫn 2-estimator vừa sinh ra bởi fix P0-1; local thuần, zero rủi ro Web. |

Dưới cut: `P1-6-COMPACT-NOTICE` (M, đáng làm sau top-5), `P1-7A/B` (M/S, tần suất thấp), `P1-8` (value thấp), `P1-2B-REAL-IMAGE-UPLOAD` (L — để ROADMAP riêng sau khi P1-2A ship).

## 4. Nguồn ngoài

- Anthropic tool-use docs (is_error/image trong tool_result, parallel tool use): platform.claude.com/docs/en/agents-and-tools/tool-use (fetch 2026-08-25).
- ChatGPT Web upload endpoint tồn tại: search xác nhận URL `chatgpt.com/backend-api/files/process_upload_stream` (flow file_id → attach cần reverse thêm khi làm P1-2B).
