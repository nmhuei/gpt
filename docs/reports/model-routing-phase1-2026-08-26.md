# MODEL-ROUTING-PHASE1 — Env alias-map + effort-first + chống silent downgrade (2026-08-26)

Implement row S từ `docs/reports/model-routing-research-2026-08-26.md`.
Opt-in hoàn toàn: env OFF ⇒ mọi payload byte-identical như trước, không đụng
ModelRegistry (vẫn ignore `claude-*` có chủ đích), không bật env ở đâu,
không live call. **Cần entry DECISIONS.md trước khi operator bật trên unit**
(registry hiện ignore claude-* một cách chủ ý — model_registry.py:36-46).

## Đã làm

### 1. Env alias-map `WEBGPT_MODEL_ALIAS` (gpt/transport/curl_transport.py)

Module-level helpers mới cạnh `logger`:

- `parse_model_alias_env(raw) -> dict[str, ModelRoute]` — 2 format:
  - JSON object: `'{"claude-sonnet-4-5": "gpt-5-5-thinking:low"}'`
  - pair list:   `'claude-sonnet-4-5=gpt-5-5-thinking:low,claude-haiku-4-5=gpt-5-5-mini'`
  - value grammar `<slug>` hoặc `<slug>:<effort>`; key casefold+strip giống
    ModelRegistry; input malformed → `ValueError("WEBGPT_MODEL_ALIAS …")`
    (fail-loud, không degrade im lặng).
- `ModelRoute(slug, effort)` dataclass frozen.
- `_model_route_for(requested)` — lookup theo id rồi (fconv) label.
- Áp dụng tại cả 2 payload builder:
  - `_build_conversation_payload` (f/conversation): match → rewrite root
    `"model"` thành slug; KHÔNG match → code path cũ nguyên vẹn.
  - `_build_codex_payload` (codex/responses): cùng env map; operator tự đưa
    slug dạng DOT (`gpt-5.2`) cho path này — không auto-convert dash↔dot
    (tránh đoán). Không thêm field reasoning vào codex envelope (spec-pinned).

### 2. EFFORT-FIRST policy

Trong `_build_conversation_payload`, precedence:
`request.reasoning_effort` (client gửi được) > alias effort pin > bỏ trống.
Lý do phải pin ở gateway: Anthropic thinking blocks bị 400 tại ingress
(protocol_adapters.py:379-383) nên Claude Code không truyền effort được.

### 3. Chống silent downgrade (research §B3)

`_stream_sse`: track xem server có thực sự publish `model_slug`/
`resolved_model_slug` hay không (so giá trị model trước/sau mỗi record —
label seed ban đầu không tính là served). Nếu served ≠ requested (casefold)
→ `logger.warning("MODEL-ROUTING mismatch …")` trên logger
`gpt.transport.curl`. KHÔNG fail-hard, KHÔNG đổi text stream.

Telemetry: `TurnResult.requested_model` (field mới, default None) trong
gpt/utils/types.py = chuỗi model chính xác đã gửi upstream sau alias
(`"auto"` ⇒ None). fconv set ở `_stream_sse`; codex set id/alias-slug
(endpoint đó chưa expose served slug ⇒ chưa kết luận được mismatch).

## Files

- `gpt/transport/curl_transport.py` — helpers + 2 builder + verify trong
  `_stream_sse` (+ requested_model cho codex TurnResult).
- `gpt/utils/types.py` — `TurnResult.requested_model: str | None = None`.
- `tests/test_model_routing_phase1.py` — 9 test mới (mục dưới).
- Không đụng: ModelRegistry, gateway/runtime, api server, protocol_adapters,
  hybrid, token_manager (prepare body giữ nguyên recipe đã verify).

## Tests (10 mới, fake SSE — không network)

`parse_model_alias_env`: JSON/pair forms + casefold key; empty/malformed ×8 → ValueError ·
alias map fconv model + effort pin · unmapped byte-identical (uuid per-turn
loại khỏi so sánh; shape legacy `action/conversation_mode/content_type`
nguyên vẹn) · client effort override alias effort · không nguồn effort ⇒
không có key `thinking_effort` · codex dotted slug áp đúng, bare request vẫn
fallback `gpt-5` · mismatch WARNING + requested_model/model đúng · matched
hoặc absent slug ⇒ không warning.

Chạy: `.venv/bin/python -m pytest tests/test_model_routing_phase1.py -q`
(9 passed). Suite liên quan curl_transport/codex_sse/file_upload/fconv_prepare/
model_effort/session/hybrid/normalize: 102 passed. **Full suite: 1255 passed.**
ruff check clean (touched files); mypy 0 error tại curl_transport/types
(23 lỗi pre-existing ở gateway/api server, ngoài phạm vi).

## Env format chính xác (dán cho owner)

```bash
# Pair form (khuyến nghị):
WEBGPT_MODEL_ALIAS='claude-sonnet-4-5=gpt-5-5-thinking:low,claude-haiku-4-5=gpt-5-5-mini'

# JSON form tương đương:
WEBGPT_MODEL_ALIAS='{"claude-sonnet-4-5":"gpt-5-5-thinking:low","claude-haiku-4-5":"gpt-5-5-mini"}'
```

Key = model string CLI gửi (id, casefold); value = slug ChatGPT (fconv dash /
codex dot), `:effort` tuỳ chọn ∈ instant|low|medium|high|max. Rỗng/unset ⇒
hành vi cũ 100%.

## Out of scope (phase 2/3)

Precheck `capabilities()` + fallback chain, pin per-conversation, feed breaker
từ mismatch signal, bảng retired→replacement.
