# Live SSE probe 2 — 2026-08-24 (fresh login)

Follow-up to `live-sse-probe-2026-08-24.md`. Hypothesis under test: the earlier
403 "Unusual activity" was caused by the backend treating the session as noauth.
This run performed a **fresh automated headless login** before retrying
`POST /backend-api/f/conversation` (SSE), to decide the T3A/T3B roadmap branch.

Method: cloakbrowser Chromium, `headless=True` for every step (no window, no
headful fallback). Login driven against the ChatGPT-hosted `/auth/login` form;
sentinel minted in page context; conversation POSTs from external curl_cffi
(impersonate chrome) with bearer + full cookie jar + `oai-device-id`.
Prompt: "Say exactly: pong". Probe quota: 2 conversation POSTs total, both used.

## Step 1 — fresh login (profile `/home/light/Downloads/webgpt/cloak-profile`)

- Repo `AutoLoginManager` could not complete this flow as-is:
  - On the new ChatGPT-hosted `/auth/login` page its fallback selector
    `button:visible:has-text("Continue")` matches **"Continue with Google"**
    first → run #1 was redirected to `accounts.google.com/v3/signin/identifier`.
  - Run #2 stayed on `chatgpt.com/`: clicking `[data-testid="login-button"]`
    did not navigate at all (SPA handler, no navigation in headless context).
  - The MFA step uses `locator.is_visible(timeout=...)`, which returns
    immediately without waiting; the `auth.openai.com/mfa-challenge/<id>` page
    mounts later than that check, so TOTP was silently skipped.
- A /tmp driver replicating the same flow with exact selectors succeeded:
  email (`input#email`) → `button[type=submit]` Continue (not Google) →
  password (`input[type=password]`) → submit → poll up to 60s for MFA input →
  TOTP submitted → landed on `https://chatgpt.com/` authenticated.
- Post-login verification (in-page): `GET /api/auth/session` → **200**,
  `accessToken` present (1866 chars), `user` present,
  session `expires` 2026-11-22T05:26Z. Fresh cookies include `_puid`,
  `cf_clearance`, `oai-did`, `__Secure-next-auth.session-token.{0,1}`.

Login is NOT a challenge blocker anymore; credentials + TOTP work headless.

## Step 2 — sentinel mint (in-page, authenticated fresh session)

| Endpoint | Status | Shape |
|---|---|---|
| `POST /backend-api/sentinel/chat-requirements/prepare` `{}` | 200 | `{persona:"chatgpt-noauth", prepare_token:"gAAAAABqi9…", turnstile{…}, proofofwork{…}}` |
| `POST /backend-api/sentinel/chat-requirements/finalize` `{"prepare_token": …}` | 200 | `{persona:"chatgpt-noauth", token:<req token>, expire_after:540, expire_at, force_login:true}` |

Key negative finding: **even immediately after a verified fresh login, the
sentinel service still returns `persona: "chatgpt-noauth"` and
`force_login: true`.** The previous report's "stale session" hypothesis is
therefore refuted — the persona label does not follow web-session auth state
from this client/IP.

## Step 3 — conversation POSTs (external curl_cffi, impersonate chrome)

Headers on both: bearer (send 1 only), full cookie jar incl. `cf_clearance`
and `_puid`, `oai-device-id`, `Accept: text/event-stream`, and
`openai-sentinel-chat-requirements-token` = finalize token minted seconds
earlier (age ≈ 0 s, well inside `expire_after: 540`).

| # | Endpoint | Auth | Status | Content-Type | Shape |
|---|---|---|---|---|---|
| 1 | `POST /backend-api/f/conversation` | Bearer AT + sentinel token (fresh) | **403** | `application/json` | `{"detail":"Unusual acti…[REDACTED len=108]"}` (121 bytes) |
| 2 | `POST /backend-anon/f/conversation` | none + sentinel token (fresh) | **403** | `application/json` | same "Unusual activity…" detail (121 bytes) |

Neither attempt returned a single SSE byte, an HTTP redirect to a WebSocket/
conduit endpoint, or any non-"unusual activity" error. No
`openai-sentinel-proof-token` / `openai-sentinel-turnstile-token` header was
attached (the sentinel envelope marks both `required: true`; computing them
requires running ChatGPT's own in-page JS).

## Conclusion

**SSE_GATED_NEED_POW_TURNSTILE** (with an unresolved identity caveat):

1. Direct SSE POST is gated, not dead: application-level 403 JSON
   ("Unusual activity"), never a stream, never a WS redirect instruction.
2. A fresh authenticated session does **not** clear the gate — this rules out
   "session expired" as the cause and confirms the gate is driven by the
   missing PoW + turnstile artifacts and/or by device/IP reputation, not by
   login state alone.
3. `persona: chatgpt-noauth` / `force_login: true` persist even when
   `/api/auth/session` proves a valid authenticated account. Either the
   backend now requires the full sentinel artifact set before it credits the
   session's identity, or this IP/device fingerprint is flagged.

## What the operator (user) can do next

- Nothing repo-side will fix this by re-logging-in; next lever is executing
  the site's own sentinel JS in-page (PoW solver + turnstile token via the
  `sentinel.openai.com/.../frame.html` iframe) and attaching
  `openai-sentinel-proof-token` + `openai-sentinel-turnstile-token` to the
  conversation POST — i.e., implement the T3A prerequisite.
- If that still 403s, try once from a different IP (mobile hotspot) or after
  a manual interactive login+chat on this machine, to test IP-reputation
  flagging.

## Evidence files (redacted)

- /tmp/sse2_mint.json — session + prepare/finalize shapes (secret-free)
- /tmp/sse2_conv.json — both conversation POST results (redacted bodies)
- /tmp/sse2_secrets.json — ACCESS RESTRICTED (bearer/cookies/tokens); delete
  after use
- /tmp/sse2_authpage.json, /tmp/sse2_mfapage.json — login form structures
- Scripts: /tmp/sse2_step1d.py (login+mint), /tmp/sse2_step2.py (probes)
