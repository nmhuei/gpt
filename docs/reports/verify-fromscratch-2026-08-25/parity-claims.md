# Verify: Parity Claims vs Code hiện hành

- Ngày: 2026-08-25
- Phạm vi: đối chiếu `docs/reports/api-parity-audit-2026-08-24.md` (ma trận parity + gap P0×2/P1×8) với code tại working tree; stealth protocol vs `docs/automation/DECISIONS.md`; SSE v1 parser; sentinel wire.
- Phương pháp: READ-ONLY — đọc code + tests + docs/automation + systemd unit; KHÔNG pytest, KHÔNG đụng gateway 18000. Không chạy live verify.

---

## 1. Đối chiếu P0 gaps

### P0-1 — usage tokens = 0 → CLI không auto-compact → 400 overflow: **VẪN MỞ trên wire (fix mới tới tầng adapter)**

Infrastructure chars÷4 đã implement sau audit:

- Helper block "Usage estimation (PARITY-P0-1)": `gpt/api/protocol_adapters.py:75-135` — `_ESTIMATED_CHARS_PER_TOKEN=4` (:86), `estimate_tokens_from_chars` ceil(n/4) min 1 (:89-95), `estimate_text_chars_to_tokens` (:98-104), `estimate_text_tokens` (:107-112), `anthropic_usage` đầy đủ cache fields =0 (:115-126), `_response_output_chars` tính content + tool arguments (:129-135).
- `response_to_anthropic(response, *, prompt_text=None)` (`api/protocol_adapters.py:428-474`): output_tokens = chars÷4 trên text + serialized tool arguments (:458-462); input_tokens chỉ khi caller truyền `prompt_text` (:463-464). Docstring tự nhận: call site chưa thread prompt_text sẽ báo 0 "until the SSE/server layer is wired (follow-up wave)".
- `StreamUsageEstimator` incremental cho SSE (`api/protocol_adapters.py:138-167`) — **không có caller production nào** (grep toàn repo: chỉ `tests/test_usage_estimation.py`).
- OpenAI envelope cũng hỗ trợ estimate từ text: `gpt/api/openai_types.py:60-95` (`format_openai_usage_chunk` nhận prompt_text/completion_text).

Nhưng wiring production chưa nối:

- Non-stream `/v1/messages`: mọi call site gọi `response_to_anthropic(response)` KHÔNG có prompt_text → input_tokens = 0 vĩnh viễn: `gpt/gateway/server.py:1308,1332,1363`; `gpt/api/server.py:881,937,968`.
- SSE stream: `message_start` hardcode `"usage": {"input_tokens": 0, "output_tokens": 0}` (`gateway/server.py:1437`); `message_delta` hardcode `"usage": {"output_tokens": 0}` (:1589 và :1631 replay path).
- Overflow path không đổi: compact deterministic trước, vẫn `ValueError` 400 khi vượt cap (`gateway/runtime.py:1390-1421`); default cap 200000 (`runtime.py:997`) — nhưng systemd unit đang chạy 250000.
- count_tokens giữ estimator cũ: JSON-dump bytes÷4 (`api/protocol_adapters.py:365-385`, route `gateway/server.py:1264-1266`), chưa hiệu chỉnh wrapper/tool-contract như đề xuất #7 của audit.

**Kết luận:** claim "usage luôn 0" trong audit đã LỆCH một nửa — tầng adapter fix xong, nhưng CLI streaming (đường chính) vẫn thấy 0/0 → auto-compact vẫn không bao giờ trigger. Về mặt hành vi CLI, P0-1 chưa giải quyết.

### P0-2 — refusal heuristic false-negative → 502: **GIẢM RỘNG, nhưng terminal 502 fail-closed GIỮ NGUYÊN (by design)**

Classifier giờ 3 lớp (`gpt/gateway/runtime.py`), mạnh hơn nhiều so với mô tả "marker list thủ công" của audit:

- Lớp 1 hard markers: `_looks_like_tool_refusal` :37-128 (~70 marker).
- Lớp 2 soft-refusal 5 category (counter_question / apology_decline / alternative_offer / conditional_deferral / hedged_inability), gồm cả tiếng Việt: `_SOFT_REFUSAL_SIGNALS` :137-229, `_looks_like_soft_tool_refusal` :244-246 — chỉ áp cho prose-only trong tool-directed task nên không bắn nhầm hội thoại thường.
- Lớp 3 false-completion: `_ACTION_CLAIM_MARKERS` :266+ ("i've created", "Đã tạo..."...) — claim tự nhận đã làm việc mà 0 tool call là bằng chứng độc lập ngôn ngữ; classify tại `_tool_correction_issue` :619-697.
- Correction prompt nhúng ORIGINAL USER TASK (:1527, task_context) + nhánh DISCOVER-FIRST cho counter-question (:830, :1612-1619); soft protocol dùng `_soft_correction_prompt` giọng hội thoại (:879+, :935-940).

Terminal behavior không đổi theo khuyến nghị #6 của audit (trả end_turn kèm text thay vì 502):

- Budget cạn → `raise MalformedToolCall` :1552-1555; refusal dai dẳng → raise sớm :1572-1575; hard reason lặp → raise sớm :1595-1597. Map ra HTTP **502 `malformed_model_tool_call`** (`gateway/server.py:2230`). Comment code khẳng định đây là chủ ý ("fail closed so the client can retry").
- `max_corrections` default 2 (`runtime.py:994`); unit đang chạy `WEBGPT_MAX_CORRECTIONS=4`.

**Kết luận:** P0-2 về xác suất fail đã giảm mạnh (3 lớp classifier + task-context correction), nhưng kịch bản cuối vẫn 502 — claim audit "correction budget rồi 502" về cơ bản CÒN ĐÚNG.

## 2. Stealth protocol vs DECISIONS.md — KHỚP

DECISIONS [2026-08-24]: "STEALTH PROTOCOL… KHÔNG khai báo tools qua API, KHÔNG chèn banner protocol; một câu giao kèo… + parser tag thuần văn bản (<cmd>/<json>)". Đối chiếu:

| Điểm | Bằng chứng file:line | Verdict |
|---|---|---|
| Không chèn banner/tool contract khi soft | `gpt/utils/toolcall.py:860-864` — `build_tool_instructions()` return `""` cho protocol `soft`; `gpt/utils/promptcompat.py:165-177` — `render_messages` bỏ cả bootstrap lẫn tool contract khi soft | OK |
| Handshake text soft | `gpt/gateway/runtime.py:853-865` `_SOFT_HANDSHAKE_TEXT`; append sau compaction để sống sót turn đầu: :1422-1425 `_with_soft_handshake`; điều kiện re-teach khi web thread mới: `_soft_handshake_needed` :1272+ (R5 BUG-B) | OK |
| SOFT-COMPACT policy ~77 từ | Toàn handshake 110 từ; riêng đoạn SOFT-COMPACT (từ "Two things worth knowing…") đo đúng **77 từ** | OK (claim đúng nếu scope đoạn SOFT-COMPACT) |
| Parser tag thuần `<cmd>`/`<json>` | `gpt/utils/toolcall.py:500-503` regex; `_extract_soft_candidates` :506-560 (<cmd> ưu tiên → <json> → fallback json-fn fence/bare; attempt hỏng raise MalformedToolCall fail-closed); nhánh soft trong `parse_tool_calls` :1110-1115 | OK |
| WEBGPT_TOOL_PROTOCOL | Code default **xml** (`runtime.py:709`, `toolcall.py:577`); giá trị hợp lệ xml/json-fn/**both**/**soft** (:38). Live systemd unit `~/.config/systemd/user/webgpt-gateway.service`: **`Environment=WEBGPT_TOOL_PROTOCOL=soft`** | Claim "=both" trong đề bài LỆCH — "both" là trạng thái trung gian (ROADMAP tick 9), đã bị stealth pivot thay; config hiện hành = soft |

Lưu ý phụ phát hiện: `gpt/gateway/adapters.py` (361 dòng) là bản copy cũ của `api/protocol_adapters.py` — `response_to_anthropic` ở đó vẫn hardcode usage 0 (:360) và **không ai import** (grep toàn repo + tests: 0 hit). Dead duplicate, dễ gây nhầm khi tra cứu.

## 3. SSE v1 delta_encoding parser — KHỚP test

`tests/test_stream_delta_v1.py` mô tả reconstruct text/model/completion từ capture thật; implementation khớp từng điểm:

- Entry: `CurlCffiTransport._consume_record` (`gpt/transport/curl_transport.py:615-659`) — `[DONE]`→complete (:623-624); error event → `ProtocolChanged` (:631-632); route sang v1 khi có `type`/`v` mà không có `message` (:633-637).
- `_consume_v1_record` :661-727: `message_stream_complete` → complete + conversation_id (:672-676); `server_ste_metadata` → model_slug/conversation_id (:677-686); typed events vô hại (delta_encoding/resume_conversation_token/message_marker/conversation_detail_metadata) (:692-694); bare-string delta append vào parts (:700-705, path `"/message/content/parts/0"` hằng `_V1_PARTS_PATH` :77); patch batch: append parts + status replace ∈ `_COMPLETION_STATUSES` = {finished_successfully, finished, complete} (:75, :711-725).
- Snapshot `add`: `_consume_v1_message` :729-778 — chỉ assistant mang text (role filter :761,:769); system/user snapshot không leak; channel filter LIVE-F4: channel ≠ "final" chỉ metadata (:762-766, `_FINAL_CHANNEL` :90); model_slug/resolved_model_slug → model (:753-757).
- `SSEDecoder` (`gpt/reverse/stream_parser.py:11-42`): tách record theo `\n\n`, gộp dòng `data:`, UTF-8 incremental an toàn; curl_transport import dùng tại :20/:485. Test `test_sse_decoder_passes_event_lines_through_to_records` khớp.
- Dedup cumulative-vs-append xử lý qua `_merge_candidate` (legacy gửi lại full parts, v1 append thuần) — test legacy `test_legacy_format_still_parses_after_v1_change` vẫn khớp nhánh cũ :641-659.

Verdict: parser reconstruct đúng như test mô tả (deltas nối đủ câu, model=gpt-5-6-mini, complete=True ở patch status + message_stream_complete + [DONE]).

## 4. Sentinel wire

- Header gắn tại `CurlCffiTransport._build_headers` (`curl_transport.py:412-456`): `openai-sentinel-chat-requirements-token` bắt buộc (:447, thiếu thì `AuthRequired` :428-436); `openai-sentinel-proof-token` (:453-454) và `openai-sentinel-turnstile-token` (:455-456) — comment verified live 2026-08-24: POST conversation chỉ stream 200 khi ĐỦ cả 3 sentinel header, requirements-only bị 403.
- Mint nguồn: in-page SentinelSDK script (`token_manager.py:96-130`) — load sdk.js, init flow 'chatgpt', token() trả `{p,t,c}` (proof/turnstile/chat-requirements).
- **`WEBGPT_SENTINEL_SDK` mặc định = ON** (opt-out): `_sentinel_sdk_enabled` `token_manager.py:620-631` — chỉ `0/false/no/off` mới tắt. Kèm sentinel cache ON mặc định (:613-616), TTL fallback 480s (:64-66), margin 60s (:68-71).
- ROADMAP T-SENTINEL-WIRE "done 2026-08-24" khớp code (STATE.md ghi targeted tests 7/7 + 22/22 xanh).

## 5. Chấm lại ma trận parity

Theo code hiện hành (37 hàng định lượng từ các bảng 1.1–1.9 của audit, cập nhật trạng thái):

| Nhóm | OK | PART | MISS |
|---|---|---|---|
| 1.1 SSE stream (10 hàng) | 6 | 4 | 0 |
| 1.2 Tool use (7) | 4 | 3 | 0 |
| 1.3 Request fields (7) | 3 | 2 | 2 |
| 1.4 stop_reason (4) | 2 | 0 | 2 |
| 1.5 count_tokens (1) | 0 | 1 | 0 |
| 1.6 usage (1) | 0 | 1* | 0 |
| 1.7 Content types (4) | 1 | 0 | 3 |
| 1.9 Errors (3) | 2 | 0 | 1 |

\* usage: nâng từ MISS lên PART chỉ nhờ tầng adapter; wire streaming vẫn 0.

- **Full green ≈ 49%** (18/37); **chạy được chức năng (OK+PART) ≈ 78%** (29/37). Hai giá trị stop_reason sống còn (end_turn/tool_use), SSE schema, tool correlation đều đúng → CLI đa bước vẫn chạy (khớp mốc VERIFY-R7d/T5/T6 trong DECISIONS).
- **P0 còn mở:** P0-1 (usage trên wire vẫn 0 → auto-compact không hoạt động; overflow 400 vẫn xảy ra, cap thực tế 250000 theo unit) — infra đã có, thiếu wiring server/stream. P0-2 coi như mitigated-but-fail-closed (502 cố ý).
- P1 còn mở: ảnh/PDF drop âm thầm; is_error mất; parallel calls cấm trừ fan-out; count_tokens chưa hiệu chỉnh; compact im lặng; stop_sequences/metadata/thinking-history silent-drop; tool_choice guarantee phụ thuộc budget. **P1-9 (mixed prose+tool fail giữa stream) đã FIX lớn** ở `_anthropic_live_stream`: sieve opener protocol-aware giữ lại từ tag đầu (`emit_openers` gồm `<cmd>`/`<json>` — `gateway/server.py:1442-1504`), remainder reconciliation re-emit text an toàn (:1564-1577), block sau text vẫn phát tiếp (:1582-1583) — audit viết trước fix R5 BUG-A nên mục này đã lỗi thời.

## 6. Các claim phát hiện LỆCH so với code

1. **"WEBGPT_TOOL_PROTOCOL=both"** (đề bài verify): sai hiện trạng — code default `xml`, unit live chạy `soft`. "both" chỉ là cấu hình trung gian trước stealth pivot.
2. **Audit "usage luôn 0" (P0-1/P1-1)**: nửa đúng — adapter đã estimate output chars÷4 + có StreamUsageEstimator, nhưng không call site production thread `prompt_text`, stream hardcode 0 → CLI vẫn thấy 0.
3. **Audit P0-2 "marker list thủ công"**: mô tả đã cũ — classifier giờ 3 lớp + task-context + DISCOVER-FIRST; phần kết 502 fail-closed thì vẫn đúng.
4. **Audit P1-9 mixed-sentinel fail giữa stream**: đã fix cho live Anthropic stream (R5 BUG-A sieve + remainder reconciliation) — audit chưa cập nhật.
5. **`gpt/gateway/adapters.py`**: dead duplicate của `api/protocol_adapters.py` (usage vẫn 0, không ai import) — nên xoá hoặc đồng bộ để tránh misleading.
6. Số dòng trong audit đã drift nặng (vd `_anthropic_live_stream` L1425→1421; `runtime.py` refusal L29-130 → 37-128; response_to_anthropic L333→428) — nội dung claim phần lớn vẫn định vị được.

## 7. Việc còn treo để đóng P0-1 (theo chính docstring code)

Thread `prompt_text` đã render (hoặc TurnResult.estimated_tokens, có sẵn ở `runtime.py:1436`) qua `_complete_anthropic` → `response_to_anthropic(prompt_text=…)`, và dùng `StreamUsageEstimator.snapshot()` cho `message_start`/`message_delta` trong `_anthropic_live_stream` + `_anthropic_content_events`. Verify: assert `usage.input_tokens > 0` trên stream thật; `claude --verbose` thấy % context tăng.
