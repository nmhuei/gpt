# CODEX-AUTH-INTEGRATION — wire codex_auth into the CODEX-SSE branch

Date: 2026-08-26
Task: CODEX-AUTH-INTEGRATION (follow-up to CODEX-AUTH-TOKEN-SOURCE, report `codex-auth-token-source-2026-08-26.md`)
Status: DONE — code + tests complete, flags remain default OFF

## What was wired

`gpt/transport/codex_auth.py` (previously dormant) is now consulted by the
codex/responses branch of `CurlCffiTransport`. The Bearer token on
`POST /backend-api/codex/responses` comes from the codex OAuth bundle
(`auth.json`, with flock-serialized rotation) **only when both**:

- `WEBGPT_CODEX_SSE=1` (codex branch opt-in), and
- `WEBGPT_CODEX_AUTH_JSON=<path>` (OAuth bundle opt-in)

are set. With either unset the behavior is byte-for-byte identical to before:
web-session access token as Bearer, same envelope, same error paths.

Cookies / `cf_clearance` still come from the browser-minted TokenBundle in
every mode — the OAuth bundle replaces only the Bearer.

## Changes (`gpt/transport/curl_transport.py` only)

1. Constructor: new keyword-only `codex_auth=None` (test injection point;
   production leaves None → a `CodexAuthManager` is lazily constructed on
   first use via lazy import — the module stays unloaded when unused).
2. `send()`: after the branch flags are computed,
   `if codex and self._codex_auth_json_enabled(): codex_bearer = await self._get_codex_bearer()`.
3. `_build_headers(..., bearer_token=None)`: `bearer_token` overrides
   `bundle.access_token` as the Bearer; `None` keeps the old code path
   untouched (existing callers unaffected).
4. 401 handling (codex+OAuth only): bare 401 from codex/responses → close
   response, drop the cached OAuth snapshot (`manager.invalidate()`),
   re-fetch the token (forces reload + refresh under the flock) and retry
   the POST **exactly once**. Semantics:
   - rotation succeeds → retry proceeds normally;
   - refresh rejected (400/401/403 = invalid_grant family) → `CodexAuthDead`
     propagates out of `send()` unchanged (terminal latch inside
     codex_auth; no retry loop);
   - second attempt also 401 → falls through to the pre-existing
     `_raise_for_status` path (invalidate web credentials + `AuthRequired`),
     never a third POST.
5. New helpers: `_codex_auth_json_enabled()` (lazy import, ImportError-safe),
   `_get_codex_bearer()`, `_invalidate_codex_auth_cache()` (duck-typed
   invalidate; deliberately does not clear a DEAD mark).

No defaults flipped; no env files touched; no other modules modified
(runtime.py / toolcall.py / api/server.py / protocol_adapters.py untouched).

## Test evidence (fake HTTP only, no live probes)

`tests/test_codex_auth_integration.py` (new, 8 tests):

1. SSE on, AUTH_JSON unset → Bearer stays web-session AT; OAuth source never
   constructed.
2. AUTH_JSON set, SSE off → legacy /f/conversation path; stub raises if the
   OAuth source were ever consulted.
3. Both flags, real-shaped auth.json (fresh JWT) via env, no injection →
   end-to-end lazy `CodexAuthManager`; Bearer = bundle access_token; no
   sentinel mint; cookies preserved.
4. 401 → rotate once → retry once: 2 posts, expired then fresh Bearer,
   `invalidate_calls==1`, `get_calls==2`, stream completes.
5. Second 401 → `AuthRequired`, exactly 2 posts, web creds invalidated once.
6. invalid_grant after 401 → `CodexAuthDead` propagates ("invalid_grant"
   matched), exactly 1 post, no loop.
7. DEAD grant before first POST → fails fast with zero posts.
8. (covered within 3–4) header/URL shape assertions.

Full targeted run: `pytest -q tests/test_codex_auth_integration.py
tests/test_codex_sse.py tests/test_codex_auth.py tests/test_curl_transport.py
tests/test_cf_resilience.py tests/test_sentinel_sdk_mint.py
tests/test_fault_injection.py` → **92 passed**. Ruff clean on both files.
Mypy: no errors in the two changed files (pre-existing unrelated errors in
gateway/server.py & api/server.py belong to other agents' scopes).

## Uncertainties / deferred

- The real codex backend's exact 401 body/status behavior is unverified by
  design (no live probe allowed this iteration); the reactive rotate-retry
  assumes a plain JSON 401 does NOT classify as a Cloudflare challenge —
  `classify_http_challenge` runs first and keeps its existing precedence.
- On the CF-challenge remint retry path the SAME OAuth bearer is reused (the
  bundle is independent of the browser snapshot); a challenge followed by a
  401 still gets its one rotate-retry afterwards.
- `CodexAuthTransient` (refresh network/5xx) propagates raw rather than being
  converted into a gateway-level retryable error — left for the live-verify
  iteration to decide.
- Next step (out of scope here): flip both flags in a controlled live probe
  against a real Plus account to verify 200 + SSE end-to-end.
