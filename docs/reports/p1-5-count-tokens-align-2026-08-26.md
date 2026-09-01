# P1-5-COUNT-TOKENS-ALIGN — Báo cáo (26/08/2026)

> Mục tiêu (TODO S trong `docs/automation/ROADMAP.md`): `/v1/messages/count_tokens`
> và `usage.input_tokens` của stream Anthropic phải dùng **cùng một pipeline**
> render → ceil(chars/4), vì trước đây hai đường tự mâu thuẫn khiến Claude Code
> thấy hai con số khác nhau giữa pre-flight count và stream usage.

## 1. Khác biệt tìm được giữa hai đường đếm

| # | Khía cạnh | count_tokens (cũ) | Stream usage (StreamUsageEstimator) | Hệ quả |
|---|-----------|-------------------|-------------------------------------|--------|
| 1 | Nguồn văn bản | Hash JSON-normalized `{system_and_messages, tools, tool_choice}` (`json.dumps(sort_keys)` → bytes/4) | Prompt text đã qua `render_messages(initial=True, tools, tool_choice)` → chars/4 | Cùng payload cho 2 số khác nhau: hash tính cả cấu trúc JSON (`{`, `"`, dấu phẩy) còn render có scaffold; chiều dài lệch theo định dạng chứ không theo nội dung |
| 2 | Tool schema | Tính nguyên JSON dump của tools/tool_choice | Tuỳ tool protocol: protocol `soft` không inject tool nào vào prompt text | Lệch lớn khi client gửi nhiều tools |
| 3 | Bootstrap/role framing | Không có (hash thô) | Có (`WEBGPT SESSION BOOTSTRAP` + `<WEBGPT_MESSAGE>` khi protocol ≠ soft) | count_tokens thấp hơn usage ngay cả với prompt rỗng |
| 4 | Floor-to-one | `max(1, …)` trên hash | `estimate_tokens_from_chars` floor 1 trên prompt text | Nhất quán về công thức nhưng khác đầu vào nên vẫn lệch |
| 5 | Non-stream envelope | `response_to_anthropic` cũ không nhận `prompt_text` → input_tokens=0 | Estimator luôn > 0 | Non-stream vs stream cũng tự mâu thuẫn |

Audit bổ sung (không phải bug, chỉ ghi nhận phạm vi): các call site
`_messages_prompt_text(messages)` còn lại trong `gpt/api/server.py` /
`gpt/gateway/server.py` đều nằm trên đường OpenAI chat-completions
(`format_openai_chat_response`, `format_openai_usage_chunk`) — contract
OPENAI-USAGE-WIRE riêng, ngoài phạm vi align Anthropic. Đường Anthropic stream
dùng `_request_prompt_text(adapted.request)` → helper dùng chung.

## 2. Cách align

Đặt helper dùng chung trong `gpt/api/protocol_adapters.py` (file được phép sửa,
tránh circular import — server import ngược vào đây):

- `rendered_request_prompt(request)` — **single source of truth**:
  `render_messages(request.messages, initial=True, tools=…, tool_choice=…)`
  (import từ `gpt.api.messages`, vốn re-export `gpt.promptcompat`).
- `estimate_tokens_from_chars(chars)` = `ceil(chars/4)`, floor 1 — cùng công thức
  `StreamUsageEstimator` dùng cho input.
- `estimate_anthropic_input_tokens(body)` mới: `parse_anthropic_request(body)`
  → `rendered_request_prompt` → `estimate_tokens_from_chars`. Bỏ hoàn toàn path
  hash JSON.
- `response_to_anthropic(…, prompt_text=None|str)` — non-stream nhận rendered
  prompt do server truyền vào; `None` = không quan sát được → 0, `""` → floor 1.
- Server (đã wire sẵn, không sửa trong task này): `_request_prompt_text(request)`
  delegate sang `rendered_request_prompt`, cả live stream lẫn fallback payload
  dựng estimator từ cùng text đó.

Kết quả: count_tokens ≡ `StreamUsageEstimator(rendered).snapshot()["input_tokens"]`
với mọi body hợp lệ (cùng hàm, cùng tham số → chênh lệch bằng 0, tolerance ±1
chỉ để phòng rounding tương lai).

## 3. Property-style test (`tests/test_count_tokens_align.py`)

Tham chiếu độc lập `_reference_input_tokens(body)`: parse → `render_messages`
→ `StreamUsageEstimator(...).snapshot()["input_tokens"]`, so với
`estimate_anthropic_input_tokens(body)`, assert `|count − reference| ≤ 1`.

6 payload shape (mục tiêu ≥5):

1. `plain-text` — 1 message text thuần.
2. `system+tools+toolloop` — system + tools + tool_use/tool_result round-trip.
3. `forced-tool-choice` — tool_choice ép tool, content 500 ký tự (rounding).
4. `long-system` — system ~2 800 ký tự.
5. `long-history` — 24 lượt user/assistant xen kẽ.
6. `empty-content` — `messages=[{"role":"user","content":""}]`.

Ghi chú phát hiện khi viết test: payload "rỗng" ở tầng client vẫn cho prompt
render **không rỗng** (bootstrap + role framing luôn được inject), nên assertion
đúng là *hai đường bằng nhau* (57 == 57), không phải floor == 1. Floor-to-one
cho prompt rendered thật sự rỗng (`prompt_text=""`) đã có test riêng trong
`tests/test_api_server.py::test_non_stream_known_empty_prompt_floors_at_one_input_token`.
Ngoài ra: scale theo kích thước prompt, `{}` → `RequestValidationError`, và
assert công thức đúng là `ceil(len(rendered)/4)`.

## 4. Regression & kết quả chạy

Không phải cập nhật kỳ vọng nào ở test cũ: các expectation kiểu hash đã được
thay bằng contract render-based từ trước trong nhánh làm việc
(`test_api_server.py`: "Render-based contract", Gate-1 conformance chỉ assert
`int > 0`); tất cả pass với hành vi mới.

| Suite | Kết quả |
|-------|---------|
| `tests/test_count_tokens_align.py` | 10 passed |
| `test_protocol_adapters.py` + `test_api_server.py` + `test_claude_code_conformance.py` + `test_gateway_agent_loop.py` | 91 passed |
| **Full suite** `.venv/bin/python -m pytest -q` | **944 passed, 0 failed** (~24s) |

Lint/typecheck: `ruff`/`mypy` chưa cài trong `.venv` hiện tại → bỏ qua bước này.

## 5. File thay đổi trong task này

- `tests/test_count_tokens_align.py` — mở rộng parametrize lên 6 shape, thêm
  test empty-content agreement, sửa assertion floor sai ban đầu của tôi.
- `docs/reports/p1-5-count-tokens-align-2026-08-26.md` — báo cáo này.

Phần align lõi (`gpt/api/protocol_adapters.py`: `rendered_request_prompt`,
`estimate_rendered_input_tokens`, `estimate_anthropic_input_tokens`,
`StreamUsageEstimator`, `response_to_anthropic(prompt_text=…)`) đã nằm sẵn
trong working tree (uncommitted) trước phiên này; phiên này audit lại toàn bộ
chuỗi end-to-end, xác nhận không còn đường đếm nào lệch contract, và hoàn thiện
phần test property. Không động vào `gateway/runtime.py`, `utils/assistantturn.py`,
`api/openai_types.py`, hay bất kỳ `server.py` nào.
