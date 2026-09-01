# codex13 fix A — transport (curl_transport / codex_auth)

Date: 2026-08-26 · Scope: Codex review #13 findings 1–2 only
Files touched: `gpt/transport/curl_transport.py`, `gpt/transport/codex_auth.py`,
`tests/test_codex_auth.py`, `tests/test_codex_auth_integration.py`,
`tests/test_fconv_prepare.py`. No gateway/api-server/runtime/toolcall/protocol_adapters/utils changes.

## Verdict

| # | Finding | Verdict |
|---|---------|---------|
| 1 | High — codex 401 "rotate once" replays the same bearer; second 401 leaves OAuth source reusable | **CONFIRMED** — fixed |
| 2 | Medium — fconv conduit 401/403 swallowed without credential invalidation; header-build failure invalidates wrong cache | **CONFIRMED** — fixed |

Both were verified by failing tests first (9 RED tests on the pre-fix tree), then fixed minimally.

## Finding 1 — evidence + fix

Evidence: `CodexAuthManager.invalidate()` only drops the in-memory snapshot;
the next `get_access_token()` reloads auth.json and serves the same token while its JWT
`exp` is fresh (`codex_auth.py::_ensure_state`). So the 401 retry at `curl_transport.py`
re-sent the identical bearer; a second 401 fell through to `_raise_for_status`, which
only invalidated the browser-side web-session cache — the OAuth source kept serving the
rejected snapshot on later turns.

Fix:
- `codex_auth.get_access_token(*, force_refresh=False)` — forces the locked refresh
  critical section; `_locked_refresh(force=True)` skips the adopt-fresh-disk early
  return and always posts the refresh grant (rotation persisted atomically under flock).
- New non-terminal distrust latch: `mark_untrusted(reason)` / `untrusted_reason` /
  `mark_trusted()`. While latched every fetch performs a REAL refresh; cleared by one
  fully accepted codex request (`send()` marks trusted after `_raise_for_status` passes).
  Transport hooks `_mark_codex_auth_untrusted/_trusted` are guarded getattr (stubs no-op).
- `curl_transport.py`: first 401 → invalidate + `_get_codex_bearer(force_refresh=True)`
  → single retry; second 401 → `mark_untrusted(...)` then existing AuthRequired fall-through.
- DEAD-latch contract preserved: `_dead_reason` checked before everything, invalid_grant
  still latches terminal DEAD even when forced; distrust never revives DEAD.

Caveat (documented in code): forcing right after another process rotated can post the
just-spent grant → reuse detection → DEAD latch here. Conservative outcome, acceptable
for a credential the resource server already refuses; unit is single-process (systemd).

## Finding 2 — evidence + fix

Evidence:
- Conduit stage (`_prepare_fconv_turn`): any non-200 (incl. 401/403) only logged and the
  turn continued — access credentials stayed cached, so the SSE POST reused the bearer
  the server had just refused on prepare.
- Header-build `except AuthRequired` in `send()` branched only on `codex`; for fconv it
  dropped the sentinel cache the fconv envelope never reads, inconsistent with
  `_raise_for_status(codex or fconv)` (round 12).

Fix:
- Conduit branch gained an explicit `{401,403}` elif → `_invalidate_access_credentials()`
  while staying non-fatal (turn still completes without conduit token).
- Header-build handler now `if codex or fconv: _invalidate_access_credentials()`.

## Tests

New/extended (all RED pre-fix, GREEN post-fix): 9
- `test_codex_auth.py`: force_refresh bypasses fresh disk bundle; force honours DEAD latch;
  mark_untrusted forces real rotation until trusted (3).
- `test_codex_auth_integration.py`: rotate-once asserts forced fetch (extended); second 401
  marks source untrusted; accepted request clears latch (3; stubs extended with kwarg+latch).
- `test_fconv_prepare.py`: conduit 401/403 invalidates but stays non-fatal (parametrized ×2);
  header-build fconv failure hits Bearer cache not sentinel (2).

Results: `test_codex_auth.py` + `test_codex_auth_integration.py` + `test_fconv_prepare.py`
= 63 passed; wider transport set (curl_transport, codex_sse, cf_resilience, sentinel_sdk_mint,
file_upload, …) = 137 passed.

## Notes

- `tests/test_usage_poller.py::test_success_poll_advises_breaker_above_threshold` fails
  PRE-EXISTING and unrelated (payload `reset_at: 1` vs RESET-AWARE-COOLDOWN imminent-reset
  skip; breaker/usage_poller outside this task's file scope). Not touched.
- ruff/mypy not installed in `.venv` of this environment; `compileall` syntax check clean.
- Image-upload region (`file_upload.py` wiring) untouched; upload short-circuit verified.
