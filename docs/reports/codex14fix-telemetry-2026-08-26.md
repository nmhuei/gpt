# Codex14 Fix — Request-level correction telemetry parity

**Date:** 2026-08-26
**Source finding:** `~/Downloads/webgpt/codex-reviews/codex14-afternoon-fixes-2026-08-26.md` #1 (Medium)
**Scope:** telemetry derive only — `gpt/gateway/server.py`, `gpt/api/server.py`, tests

## Verdict on the finding: CONFIRMED

Verified at all three points:

1. `gpt/gateway/runtime.py:2178-2197` — runtime increments `correction_count` and emits
   `tool_correction` **before** the CORRECTION-TIGHTEN anti-repeat digest check (`:2214`).
2. On the second identical repeat, the abort path decrements its own pre-check increment
   back (`:2222-2224`, Codex13 #4 rollback) before emitting `persistent_correction_repeat`
   and raising — so both terminal events carry the net spend:
   `submit_failed_before_commit_unknown` metadata `correction_count` (`:2354`) and
   `submit_completed` metadata `correction_count` (`:2382`).
3. Both `_RequestTraceMiddleware.finalize()` implementations derived request-level
   `request_completed.correction_count` by counting raw `tool_correction` events ⇒ +1
   overcount per anti-repeat abort; and 0 undercount when error responses carried no
   session header (the whole session-filtered loop was skipped).

## Fix applied (both middlewares, identical)

Chosen approach: derive from terminal runtime metadata (codex option A), with codex
option B kept only as a defensive fallback.

- `submit_completed` / `submit_failed_before_commit_unknown` branches now capture
  `metadata["correction_count"]` into `runtime_correction_count` (last terminal event wins).
- The no-session-header best-effort fallback loop (TURN-ID-FAILURE-TRACE path) also
  captures `correction_count` from unfiltered failure events — fixes the undercount symptom.
- Resolution after the loops: prefer `runtime_correction_count`; if absent (e.g.
  `commit_unknown` terminal has no correction metadata), fall back to raw event count minus
  `persistent_correction_repeat` count, clamped at 0. Error paths never crash when
  metadata is missing (plain `isinstance(int)` guards throughout).

Untouched, per constraints: runtime.py, toolcall, curl_transport, protocol_adapters,
accounts, conftest; late-fail/ping/chunk/529/header-middleware/refusal regions merged today.

## Test

Added `test_anti_repeat_abort_request_telemetry_matches_terminal_count`
(`tests/test_api_server.py`, end of file): full-stack `/v1/messages` run with three
identical prose claims → escalation hint → fail-fast abort. Pins:
raw `tool_correction` indices `[1, 2, 3]`, exactly one `persistent_correction_repeat`,
and `request_completed.correction_count == 2` (net terminal spend, previously 3).

## Results

- Targeted: `tests/test_api_server.py` + `tests/test_tracing.py` +
  `tests/test_correction_tighten.py` — 58 passed.
- Regression: gateway/stream suites (delta_tooluse_and_handshake, stream_polish,
  stop_reason_refusal, stream_close_and_crash, stream_correct_dedup,
  gateway_agent_loop, fault_injection, refusal_detection, tool_protocol_variants) —
  140 passed.
- ruff: 2 findings in touched files are pre-existing from today's earlier merges
  (`I001` import sort in gateway/server.py header; `SIM201` at test_api_server.py:770
  ping test) — outside this fix's file scope, left alone. HEAD versions lint clean.
- mypy: no errors in the edited regions; 23 pre-existing errors elsewhere in the repo.

Not committed; gateway not restarted.
