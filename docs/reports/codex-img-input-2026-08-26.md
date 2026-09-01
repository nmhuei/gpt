# CODEX-IMG-INPUT (ROADMAP row M) — /v1/responses input_image → codex input_image

Date: 2026-08-26
Status: DONE (fake-session tests only; no live probe, no env flip, no commit)

## Problem

The codex branch (`/v1/responses` → `POST /backend-api/codex/responses`)
silently dropped `input_image` content items: `parse_responses_request`
(gpt/api/protocol_adapters.py) extracts only text blocks. Claude Code CLI
Read()s screenshots and pastes image blocks into the gateway, so the model
never saw them.

## Design

Editing protocol_adapters.py was off-limits (owned by another agent), so the
image travels as a strict single-line text marker that survives the whole
render pipeline (`json.dumps(...).replace("<", "\\u003c")` escapes and
decodes cleanly):

```
<WEBGPT_IMAGE_DATA mime="image/png">iVBORw0KGgo=...</WEBGPT_IMAGE_DATA>
```

1. **Ingress** (`gpt/api/server.py`, `_inject_codex_image_markers`, wired in
   `responses()`): only when `WEBGPT_CODEX_SSE` is truthy (same flag + truthy
   set as `CurlCffiTransport._codex_sse_enabled`) rewrite each
   `input_image` / Anthropic-style `image` block in **user-role** items into
   an `input_text` marker, position-preserving.
   - Accepted sources: `image_url` data URL (`data:<mime>;base64,<b64>`,
     dict form tolerated) and `{"source": {"type": "base64", ...}}`.
   - Remote https URLs are never fetched; malformed mime/base64 → omission
     note (info log).
   - Oversize guard: base64 payload > `_CODEX_IMAGE_MAX_B64_CHARS`
     (20 MB ≈ 15 MB binary) → skip + WARNING log + omission note.
   - Flag off ⇒ body untouched byte-for-byte (non-codex paths unchanged).
2. **Transport** (`gpt/transport/curl_transport.py`):
   - `_build_codex_payload` now calls `_expand_image_markers` after
     `_strip_reasoning_items`: user message items whose input_text carries a
     marker are rebuilt interleaving `input_text` + `input_image`
     (`{"type":"input_image","image_url":"data:<mime>;base64,<b64>"}`);
     everything else returns identical objects (byte-identical payload).
     Strict regex ⇒ truncated/corrupted markers stay inert text instead of
    matching (e.g. budget-trimmed prompts).
   - Defense-in-depth oversize cap mirrored transport-side (warning + note).
   - Legacy `_build_conversation_payload` degrades markers to
     `[image omitted: <mime>]` so f/conversation never leaks megabytes of
     base64 into the pasted prompt.
3. Untouched by design: `_get_codex_bearer` / `_codex_auth_json_enabled`
   (CODEX-AUTH-INTEGRATION regions ~203/324/~1180+), runtime.py,
   toolcall.py, protocol_adapters.py, utils/*.

## Known limits

- The pure-DOM/browser session path (gpt/transport/session.py, owned
  elsewhere) does not strip markers; codex-off ingress is a no-op there
  anyway, so exposure requires codex-on + DOM fallback simultaneously.
- Images ride inside prompt text, so they count toward
  WEBGPT_PROMPT_BUDGET_CHARS if that env is set (budget disabled by default);
  a truncated marker degrades to inert text, never a broken upload.
- Assistant/tool-carried images are ignored at ingress (no codex semantics).

## Files

- gpt/api/server.py — ingress helpers + hook in responses()
- gpt/transport/curl_transport.py — marker regex/caps, `_expand_image_markers`,
  `_codex_image_part`, `_strip_image_markers`, payload wiring
- tests/test_codex_sse.py — 11 new tests

## Verification

- Targeted: `.venv/bin/python -m pytest tests/test_codex_sse.py -q`
  → 24 passed (13 pre-existing + 11 new).
- Full suite: `.venv/bin/python -m pytest -q` → 1020 passed, 0 failed.
- ruff check on the three files: clean. mypy: no errors in added lines
  (remaining repo errors pre-exist from other in-flight work).
