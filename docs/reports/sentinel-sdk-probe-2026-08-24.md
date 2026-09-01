# Sentinel SDK probe — 2026-08-24 (proof + turnstile via in-page SentinelSDK)

Follow-up to `live-sse-probe-2-2026-08-24.md`, which concluded that
`POST */f/conversation` returned 403 "Unusual activity" because
`openai-sentinel-proof-token` and `openai-sentinel-turnstile-token`
(envelope `required: true`) were missing. Goal of this run: produce both tokens
by running ChatGPT's own SentinelSDK JS in page context — used exactly as the
page uses it, no out-of-browser PoW reversal — then retry conversation POSTs
(quota: max 2) for a final SSE alive/dead verdict.

Method identical in discipline to prior probes: cloakbrowser Chromium,
`headless=True` everywhere, no window ever, no headful fallback. Session was
still authenticated via the persistent profile (`/api/auth/session` → 200,
accessToken present), so no new login was needed.

## Step 1 — surface survey (negative results first)

In-page enumeration of `Object.getOwnPropertyNames(window)` against
`/sentinel|turnstile|proof|arkose|challenge/i`:

- **No sentinel/turnstile globals exist before SDK load.** Only hit:
  `WakeLockSentinel` (unrelated platform API). Probed and absent:
  `__sentinel`, `__sentinelSDK`, `sentinel`, `Sentinel`, `turnstile`,
  `__turnstile`, `openai`.
- No `sentinel.openai.com` iframe at rest or within a 30 s postMessage listen
  window after requesting chat-requirements; zero message events captured.
  The only challenge-ish script is Cloudflare JSD:
  `/cdn-cgi/challenge-platform/scripts/jsd/api.js?onload=jsdOnload`.
- Requirements envelope (`POST /backend-api/sentinel/chat-requirements/prepare`)
  now carries **three** required blocks: `turnstile {required:true, dx:<29560
  char blob>}`, `proofofwork {required:true, seed:"0.67…", difficulty:"06ee18"}`,
  and a new `so {required:true, collector_dx, snapshot_dx}` block.

## Step 2 — finding the loader (bundle analysis)

Captured all 358 JS responses on page load; grep for markers found the
sentinel machinery in `/cdn/assets/4813494d-kn665p32hm33twfy.js`. Key original
strings (verbatim):

```js
Qyt = `https://chatgpt.com/backend-api/sentinel/sdk.js`
rkt = `https://sentinel.openai.com/backend-api/sentinel/sdk.js`
// lazy loader appends <script src=…> then:
sX.SentinelSDK?.init(`calpico_init`)
```

The SDK is **lazily injected as a script tag** from
`https://chatgpt.com/backend-api/sentinel/sdk.js` (mirror on
`sentinel.openai.com`) and exposes itself as **`globalThis.SentinelSDK`** —
the exact key name is `SentinelSDK`, which the earlier window survey had not
probed. Header constants verbatim from the bundle:

```
OpenAI-Sentinel-Chat-Requirements-Token / …-Prepare-Token
OpenAI-Sentinel-Turnstile-Token
OpenAI-Sentinel-Proof-Token
OpenAI-Sentinel-SO-Token        (SessionObserver)
OpenAI-Sentinel-Token           (combined fallback)
OAI-Telemetry                   (timing array, e.g. "[1,109.3,…]")
```

## Step 3 — SentinelSDK API (verbatim keys, live in-page)

After injecting `sdk.js` via the same `<script>` mechanism the page itself
uses, `window.SentinelSDK` exposes (own + prototype chain):

| Member | Signature observed |
|---|---|
| `init` | `async init(flow)` |
| `token` | `async token(flow)` — returns JSON string `{p, t, c, id, flow}` |
| `requirementsToken(flow)` | sync accessor (returned null here; throws inside iframes) |
| `sessionObserverToken(flow)` | returns null here (SO not active for this session) |
| `timing()` | returns array, e.g. `[1,109.3,12,74,24,2,0,113]` |

`token('chatgpt')` (any flow string accepted, including `'calpico_init'`,
`undefined`) resolves in ~1–2 s and internally does the whole envelope:
fetches chat-requirements, solves the PoW (`gAAAAAB…`, ~620–680 chars),
evaluates the turnstile `dx` blob (~12 372 chars result). Returned object
keys, verbatim: **`p`** (proof), **`t`** (turnstile), **`c`**
(chat-requirements token, ~1950 chars), plus `id`, `flow`.
Redacted shapes are in `/tmp/sent_sdk_calls.json` and
`/tmp/sent_conv_results.json`.

## Step 4 — final conversation POSTs (quota: exactly 2, both used)

External curl_cffi (impersonate chrome) with full cookie jar (incl.
`cf_clearance`, `_puid`), `oai-device-id`, `Accept: text/event-stream`,
prompt "Say exactly: pong", tokens minted seconds earlier (age ≈ 0,
expire_after 540).

Headers sent on both: `openai-sentinel-chat-requirements-token`,
`openai-sentinel-proof-token`, `openai-sentinel-turnstile-token`,
`oai-telemetry`. (`openai-sentinel-so-token` omitted: minted value null.)

| # | Endpoint | Auth | Status | Content-Type | Result |
|---|---|---|---|---|---|
| 1 | `POST /backend-api/f/conversation` | Bearer AT + full sentinel set | **403** | application/json | `{"detail":"Unusual acti…[REDACTED len=108]"}` (121 bytes), no SSE bytes |
| 2 | `POST /backend-anon/f/conversation` | no bearer, fresh full set | **200** | `text/event-stream; charset=utf-8` | **live SSE stream, 8458 bytes captured**, first event `data: {"type":"resume_conversation_token",…}` with conduit JWT |

## Conclusion — `SSE_ALIVE_WITH_FULL_SENTINEL` (anon path)

1. **SSE is alive.** With proof + turnstile + requirements all minted by
   ChatGPT's own in-page SentinelSDK, `POST /backend-anon/f/conversation`
   streams normally (HTTP 200, event-stream, real events). The gate was
   exactly what probe 2 concluded: missing PoW + turnstile artifacts.
2. **Authenticated path stays gated even with the full sentinel set.** Send #1
   still got 403 "Unusual activity". This matches the persistent
   `persona: "chatgpt-noauth"` / `force_login: true` from prepare/finalize:
   the backend does not credit this client/IP's web-session identity, so the
   bearer route remains blocked independently of sentinel correctness. Likely
   device/IP-reputation flagging (headless fingerprint, shared IP).
3. **Practical consequence for the roadmap (T3A):** the working recipe is
   inject `sdk.js` → `await SentinelSDK.token(flow)` → split `{p,t,c}` into
   the three `OpenAI-Sentinel-*` headers. All of it must run in browser page
   context; nothing needs reversing outside the browser.

## Evidence files

- /tmp/sent_survey.json — window survey, requirements envelope shape, 30 s postMessage log (empty)
- /tmp/sent_js_resources.json, /tmp/sent_dom_scripts.json — bundle inventory
- /tmp/sent_bundle_hits.json — marker hits → /tmp/sent_bundles/resp_*.js (raw vendor bundles)
- /tmp/sent_sdk_calls.json — SentinelSDK member list + call shapes (redacted)
- /tmp/sent_conv_results.json — both send results (redacted bodies)
- Scripts: /tmp/sent1_login_survey.py, /tmp/sent2_find_bundles.py,
  /tmp/sent3_capture.py, /tmp/sent4_sdk_probe.py, /tmp/sent5_final_send.py
- Secrets file /tmp/sent_conv_secrets.json written during step D has been shredded.
