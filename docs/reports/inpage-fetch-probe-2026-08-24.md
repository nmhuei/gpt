# In-page fetch probe — 2026-08-24 (POST /f/conversation from inside page context)

Question under test: for the AUTHENTICATED path that keeps returning
403-reputation when driven by external HTTP, does the request survive if it is
issued **by Chromium itself** — `page.evaluate(fetch(...))` with
`credentials:'include'` — i.e. with genuine browser TLS/device/cookie state?
If yes, future transport = one browser page per account, every turn an in-page
fetch stream, zero DOM automation.

Method: profile copied verbatim (`cp -a cloak-profile → /tmp/profile-diff-B`,
2.0 GB), cloakbrowser persistent context, `headless=True`, no window ever, no
headful fallback. Quota respected: **exactly 2 conversation POSTs total**.
All secrets stayed in process memory; every file output is redacted to ≤12 chars.

## Step 1 — launch + auth check (no POST)

- First `goto` failed once with `net::ERR_ABORTED` (navigation superseded after
  the profile copy); a bounded retry loop (4 attempts) landed cleanly. No other
  navigation quirks.
- In-page `GET /api/auth/session` → **200**, `accessToken` present (1866 chars,
  never written to disk), user present. Cookies confirmed present:
  `oai-did` (`75b27bf0-121..len36`), `cf_clearance`, `_puid`.

## Step 2 — sentinel mint in page context

Injected `/backend-api/sentinel/sdk.js` via the page's own `<script>` mechanism;
`window.SentinelSDK` present. `await SentinelSDK.token('chatgpt')` resolved in
~1–2 s, twice, returning keys verbatim `['p','t','c','id','flow']`:

| Mint | p (proof) | t (turnstile) | c (requirements) |
|---|---|---|---|
| 1 (for send A) | `gAAAAABWzMwM..len709` | `QhYbBxYBBgwN..len12344` | `gAAAAABqi9xt..len1912` |
| 2 (for send B) | `gAAAAABWzMwM..len761` | `TBsbCBsHDAwL..len12372` | `gAAAAABqi9xu..len1996` |

(`sessionObserverToken` was null this session; `openai-sentinel-so-token`
omitted, same as prior probes.)

## Step 3+5 — the two POSTs, both from inside `page.evaluate`

Request issued as
`fetch(endpoint, {method:'POST', credentials:'include', headers:{authorization?…,
oai-device-id, accept:'text/event-stream', content-type:'application/json',
openai-sentinel-chat-requirements-token, openai-sentinel-proof-token,
openai-sentinel-turnstile-token}, body})`. Body structure = the known
browser-compatible shape (`action:"next"`, one user message
`content_type:"text"` `"Say exactly: pong"`, `model:"auto"`,
`parent_message_id`, `conversation_mode:{kind:"primary_assistant"}`) — identical
to what the repo transport and all prior probes send. Stream read via
`response.body.getReader()` loop inside evaluate, cap 8 KB or `[DONE]`,
20 s idle / 45 s overall guard.

| # | Endpoint | Auth | Origin | Status | Content-Type | Result |
|---|---|---|---|---|---|---|
| A | `POST /backend-api/f/conversation` | Bearer AT + full sentinel set | **Chromium itself (in-page)** | **403** | `application/json` | 121 bytes JSON, no SSE bytes: `{"detail":"Unusual activity has been detected from your device. Try again later. (c5e918a8-c0d..len36)"}` — round-trip 343 ms, reader saw `done` normally |
| B | `POST /backend-anon/f/conversation` | no bearer, fresh mint | Chromium itself (in-page) | **200** | `text/event-stream; charset=utf-8` | live SSE, 8451 bytes captured (8 KB cap hit), first event `data: {"type":"resume_conversation_token", … "kind":"topic","token":"eyJhbGciOiJF…(conduit JWT)"}`, then real `message` frames (`author.role:"system"`, conversation_id present) |

Both responses surfaced via same-origin headers: `server: cloudflare`,
`x-content-type-options: nosniff`; no `cf-mitigated`, so B's 200 is the
application serving SSE, not a challenge pass-through.

Step 4 (one faithful-body retry on 403) was deliberately NOT spent: the only
known body artifact is the endpoint-name list in
`2026-08-24-live-protocol-findings.md` (the burp capture file is empty), so any
"faithful copy" retry would be byte-equivalent to send A and add nothing — the
quota's second POST went to the required anon control instead.

## Step 6 — Conclusion: `INPAGE_AUTH_GATED_DEVICE_OR_IP_REPUTATION`

1. **The in-page fetch pipeline itself works end-to-end**: mint-in-page →
   POST-in-page → `getReader()` streaming, all inside one evaluate, no buffering
   problems, no need for bindings/polling. The anon control streamed real SSE
   from a genuine Chromium-originated request.
2. **But moving the authenticated POST inside the browser changes nothing about
   the gate.** Send A carries everything a real chatgpt.com turn carries —
   genuine TLS/H2 fingerprint, client hints, the exact cookie jar the page uses,
   fresh full sentinel set minted by ChatGPT's own SDK seconds earlier — and
   still gets application-level 403 "Unusual activity … from your device".
   This refutes the remaining transport-fingerprint hypothesis: the flag follows
   the **identity** (account/persona `chatgpt-noauth` + IP/device history), not
   how the bytes are emitted.
3. Therefore in-page fetch is necessary but not sufficient for authenticated
   turns. The blocker sits upstream of transport entirely.

### Architectural implication

- The proposed "1 page per account, every turn an in-page fetch stream"
  transport is **technically validated as a mechanism** (streaming works,
  sentinel integration is trivial in-context, zero DOM automation needed).
- For the currently flagged account/IP it cannot deliver authenticated turns:
  expect 403 until reputation clears (different IP / aged device identity /
  organic usage history on that profile). Worth one control run from a clean IP
  (mobile hotspot) before investing further in transport work — the probe shows
  transport is no longer the variable.
- Interim consequence for T3A: anon-path streaming via the same in-page recipe
  remains fully alive and is the reliable baseline.

## Technical notes / limitations

- `page.evaluate` handles long async reader loops fine (Playwright awaits the
  returned promise); the 8 KB/[DONE] caps plus 20 s-idle/45 s-overall JS guards
  prevented any hang. No chunked-binding fallback was needed.
- One-shot `net::ERR_ABORTED` on first post-copy navigation; retried
  automatically, did not recur. Copying a live-profile while another Chromium
  instance may later use it is fine here because the original profile was closed.
- Forbidden-header limits apply in-page (UA/sec-ch-* set by the browser itself —
  which is exactly the point of this experiment).

## Evidence files (redacted)

- /tmp/inpage_results.json — both sends: status, response headers, redacted
  stream text, meta (session/did/cf_clearance/_puid booleans)
- Script: /tmp/inpage_probe.py · Profile copy: /tmp/profile-diff-B
- Prior context: docs/reports/sentinel-sdk-probe-2026-08-24.md,
  docs/reports/live-sse-probe-2-2026-08-24.md,
  docs/reports/2026-08-24-live-protocol-findings.md
