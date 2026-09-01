# codexfix-a — soft parser: fence immunity + placeholder excision (2026-08-26)

Scope: Codex review #12 (`~/Downloads/webgpt/codex-reviews/codex12-yesterday-fixes-2026-08-26.md`) findings #2 and #5, both in `gpt/utils/toolcall.py`. Verify-first workflow; both findings confirmed ĐÚNG before any code change.

## Finding 2 (High) — soft scan trên text gốc thay vì masked: **ĐÚNG**

Bằng chứng (verify):
- `parse_tool_calls()` tính `masked = _mask_markdown_code(text)` và dùng nó cho legacy/DSML/XML, nhưng truyền text GỐC vào `_extract_soft_candidates()`.
- Bên trong, `<cmd>`/`<json>` được count và regex-match trên text gốc → ```` ```\n<cmd>rm -rf /tmp/important</cmd>\n``` ```` cho ra tool call thật `Bash(command="rm -rf /tmp/important")` (failing test chứng minh trước khi fix).
- Handshake soft (`runtime.py::_SOFT_HANDSHAKE_TEXT`) dạy emit plain unfenced → mask không ảnh hưởng emission hợp lệ.

Fix:
- `_extract_soft_candidates(text, masked, definitions)` — đếm tag + tìm match trên `masked` (offset giữ nguyên); body lệnh/payload JSON trích từ text GỐC giữa 2 literal tag theo offset match (`match.start()+len(open) : match.end()-len(close)`). Lưu ý: dùng `group(1)` bounds tính trên masked sẽ cắt mất phần body bị mask (đã bắt được qua test backtick-substitution).
- Fallback bare/fence JSON vẫn scan text gốc — fence ```json là emission shape chính thức của json-fn ở mọi protocol.
- Edge đã verify thủ công: quoted `<json>` trong backtick không còn kích nhầm lỗi mixed-shape; fenced `<cmd>evil</cmd>` + cmd thật ngoài fence → chỉ cmd thật chạy; fence không đóng → passthrough prose.

## Finding 5 (Low) — placeholder span không được trim khi raw_calls rỗng: **ĐÚNG**

Bằng chứng: placeholder branch ghi span với comment "so the quote is excised from the visible prose", nhưng `_finalize_parse()` trả `text` nguyên vẹn khi `raw_calls` rỗng → `<cmd>"..."</cmd>` vẫn hiện trong reply. Test r9 cũ thậm chí đang khoá cứng hành vi sai (`prose == text`).

Fix: `_finalize_parse()` — khi `raw_calls` rỗng, không phải markup, mà `spans` khác rỗng → blank các span rồi `.strip()` trả về. Chỉ soft path đi tới nhánh này với spans (json-fn `[]` rỗng bị return sớm trước finalize). Test r9 cập nhật kỳ vọng: `<cmd>` và body placeholder biến mất khỏi prose, phần ack xung quanh giữ lại.

## Diff summary

- `gpt/utils/toolcall.py`: signature `_extract_soft_candidates` thêm `masked`; count/match trên masked; body từ original offsets; docstring ghi lý do codex12#2. `_finalize_parse` thêm nhánh span-trim khi raw_calls rỗng (codex12#5).
- `tests/test_stealth_protocol.py`: +5 test (fence/inline-quote cmd + json không execute; backtick-substitution body vẫn parse; placeholder-only bị excise).
- `tests/test_gateway_agent_loop.py`: test r9 đổi assertion `prose == text` → excision assertions.

## Tests

- RED trước khi fix: 4 failed / 1 passed (3 injection + 1 excision fail đúng như finding; guard backtick pass).
- Sau fix, targeted regression nguyên văn: `227 passed in 1.63s`
  (test_stealth_protocol, test_tool_transpiler, test_gateway_agent_loop, test_tool_protocol_variants, test_delta_tooluse_and_handshake, test_client_fixtures, test_claude_bootstrap_full_tools, test_discover_policy).

## Lint/type (ngoài phạm vi, không xử lý)

- ruff: 19 findings đều pre-existing ngoài vùng sửa (RUF001 :513, E402/RUF005/RUF059/B007/I001 trong phần import/test cũ).
- mypy: 3 errors trong toolcall.py (:745 Match|None, :1233/:1234 no-redef) — pre-existing ở `_parse_markup_blocks`/nhánh markup, không phải do thay đổi lần này.

Không commit, không restart gateway, không đụng runtime.py/curl_transport.py/token_manager.py/api/server.py.
