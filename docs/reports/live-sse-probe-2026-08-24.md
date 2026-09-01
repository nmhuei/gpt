# Live SSE probe — 2026-08-24

Question: is the direct `POST /backend-api/f/conversation` (SSE stream) still accepted
by ChatGPT, or has traffic been forced through a conduit prepare + WebSocket flow?

Method: live probes against chatgpt.com from this machine, max 2 conversation sends
(prompt: "Say exactly: pong"). All tokens/cookies below are redacted to <= 12 leading chars.

## Environment

- Profile used: `/home/light/Downloads/webgpt/profiles/personal` (copy at /tmp, since a
  running gateway process holds the original). The default `cloak-profile` session was
  expired (`/api/auth/session` 200 with `accessToken` absent).
- Browser: cloakbrowser headless Chromium, persistent profile copy.
- External HTTP client: curl_cffi impersonate chrome.
- Account state caveat: every sentinel response returned `"persona": "chatgpt-noauth"`
  with `"force_login": true`, even for `/backend-api/...` variants. The access token
  itself was present and non-empty (1866 chars), but the backend sentinel service did
  not treat the session as authenticated.

## Probe 1 — sentinel mint (browser page context)

| Endpoint | Status | Shape |
|---|---|---|
| `POST /backend-anon/sentinel/chat-requirements` | 200 | `{persona, token: "gAAAAABqi84Y…", expire_after: 540, force_login: true, turnstile:{required:true,dx}, proofofwork:{required:true,seed,difficulty:"0687a0"}, so:{...}}` |
| `POST /backend-api/sentinel/chat-requirements/prepare` | 200 | `{persona, prepare_token: "gAAAAABqi84Y…", turnstile{required:true}, proofofwork{required:true}}` |
| `POST /backend-api/sentinel/chat-requirements/finalize` body `{"p": <pt>}` | **500** | `{detail: "Internal Server Error"}` |

Key finding: the finalize body shape `{"p": ...}` (used by
`gpt/transport/token_manager.py:_sentinel_requirements`) returns HTTP 500.
The working shape discovered in probe 2:

| Endpoint | Body | Status | Shape |
|---|---|---|---|
| `POST /backend-anon/sentinel/chat-requirements/finalize` | `{"prepare_token": <pt>}` | **200** | `{persona, token: <requirements token>, expire_after, expire_at, force_login}` |
| same endpoint | `{"p": <pt>}` or `{"pt": <pt>}` | 500 | `{detail}` |

Legacy single-step sentinel still works on both `/backend-anon` and `/backend-api`.

## Probe 2 — conduit prepare

| Endpoint | Status | Detail |
|---|---|---|
| `POST /backend-api/f/conversation/prepare` body `{}` (curl_cffi) | 422 | `{"detail": "Invalid conv…" (32 chars)}` |
| `POST /backend-anon/f/conversation/prepare` body `{}` (curl_cffi) | 422 | same |
| `/backend-anon/f/conversation/prepare` with `{"conversation_id": <uuid>}` (in-page) | 422 | same |
| same with `{"parent_message_id": <uuid>}` (in-page) | 422 | same |
| `/backend-api/f/conversation/prepare` body `{}` (in-page) | 422 | same |

No conduit_token could be minted; the required request shape is unknown (the error
suggests it wants a specific conversation identifier format we did not supply).

## Probe 3 — direct conversation POST (send #1, external curl_cffi)

`POST https://chatgpt.com/backend-api/f/conversation`
Headers: Bearer access token, full cookie jar incl. `cf_clearance`, `oai-device-id`,
`openai-sentinel-chat-requirements-token` (legacy anon token), `Accept: text/event-stream`.

- Status: **403**
- Content-Type: `application/json`
- Body: `{"detail":"Unusual activity has been detected from your device. Try again later. (7f4bc306-…)"}`
- No retry with a finalize token was possible at that point (finalize was failing 500).

## Probe 4 — direct conversation POST (send #2, inside real browser page)

Identical POST executed via `fetch()` in the page context of the logged-in browser
(sentinel obtained fresh via prepare -> finalize `{"prepare_token": ...}` = 200,
token attached):

- Status: **403**
- Content-Type: `application/json`
- Body: `{"detail":"Unusual activity has been detected from your device. Try again later. (20141ecf-…)"}`

Note: neither probe attached `openai-sentinel-proof-token` /
`openai-sentinel-turnstile-token`, which the sentinel envelope marks `required: true`
and which the site's own JS computes in-page. This may be the proximate cause of the 403.

## Conclusion

1. **Direct SSE POST is gated, not confirmed dead.** Both attempts returned an
   application-level 403 JSON ("unusual activity"), never an SSE stream and never a
   WebSocket redirect instruction. We could not demonstrate a single successful SSE
   byte under any credential combination available without running ChatGPT's own
   sentinel JS (PoW + turnstile).
2. The repo's current transport assumption ("mint credentials in browser, then stream
   SSE over curl_cffi") no longer works as-is: the same 403 hits from inside the
   genuine browser context once PoW/turnstile artifacts are absent.
3. Conduit prepare (`/f/conversation/prepare`) exists but returns 422 for all payload
   shapes tried; conduit_token not obtained, so the WS/conduit path could not be
   validated either.
4. Repo bug found: `token_manager.py` finalize step sends `{"p": prepare_token}` and
   gets 500; the accepted shape is `{"prepare_token": prepare_token}`
   (`gpt/transport/token_manager.py:_sentinel_requirements`).
5. All sentinel responses carry `persona: "chatgpt-noauth"` / `force_login: true`,
   suggesting the account session is currently seen as unauthenticated by the backend;
   this may independently explain the 403s and should be re-checked after a fresh
   interactive login before treating conclusion 1 as final.

## Raw redacted evidence files

- /tmp/probe_sentinel.json — first mint shapes (redacted)
- /tmp/probe_sentinel2.json — both-backend sentinel variants (redacted)
- /tmp/probe_prepare2.json — finalize shape matrix (redacted)
- /tmp/probe_prepare.json — conduit prepare 422s (redacted)
- /tmp/probe_final.json — final conversation probe result (no secrets)
