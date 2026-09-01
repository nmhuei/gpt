# Header/payload diff — real browser vs synthetic curl_cffi on authenticated `POST /backend-api/f/conversation` (2026-08-24)

Goal: explain why the synthetic curl_cffi request gets **403 "Unusual activity"** with a full
sentinel set (see `sentinel-sdk-probe-2026-08-24.md`) by capturing the request the real
ChatGPT page sends when logged in and diffing it byte-by-byte against what the repo builds.

Method: copy of the logged-in CloakBrowser profile (`/tmp/profile-diff-A`), fully headless,
`context.on("request"/"response")` hooks for `*f/conversation*`, one single UI prompt
("Say exactly: pong") sent through the repo's own `UIDriver` (`gpt/drivers/ui.py`), exactly as
mandated. Synthetic side read from `_build_headers` / `_build_conversation_payload` in
`gpt/transport/curl_transport.py` (read-only).

## Headline result

| Path | Client | Status | Content-Type |
|---|---|---|---|
| `POST /backend-api/f/conversation` | **real page, in-browser** | **200** | `text/event-stream; charset=utf-8` — live SSE |
| `POST /backend-anon/f/conversation` | curl_cffi, full sentinel set (prior probe) | 200 | event-stream |
| `POST /backend-api/f/conversation` | curl_cffi, full sentinel set (prior probe) | **403** | application/json, "Unusual acti…" |

The same profile, same machine, same IP streams fine through the browser. The 403 is
therefore **request-shape-differentiated**, not a blanket IP/device ban.

## New protocol element found: the prepare→conduit handshake

The page never posts to `/f/conversation` cold. It first calls:

```
POST /backend-api/f/conversation/prepare        (no body)
→ 200 {"status":"ok","conduit_token":"eyJ…[REDACTED]","conduit_location":"10.x.x.x:830x",
       "cluster":"unified-1xx", …}              (JWT, ~350 chars, TTL ≈ 60 s)
```

then repeats `/prepare` (now carrying `x-conduit-token` itself) while sentinel work runs,
and finally sends the conversation POST with header **`x-conduit-token: <that JWT>`**.
This endpoint is distinct from `/backend-api/sentinel/chat-requirements/prepare` used by
the repo's token manager and by prior probes.

## Diff table — request headers

Real header order as captured (names complete, values ≤12 leading chars kept):

| # | Header (real order) | Real request | Synthetic (`_build_headers`) | Verdict |
|---|---|---|---|---|
| 1 | `oai-language` | `en-US` | present | OK |
| 2 | `sec-ch-ua-platform` | `"Windows"` | absent (curl_cffi chrome impersonation may add its own) | MISSING |
| 3 | `authorization` | `Bearer eyJhbGciOiJk[REDAC len=1873]` | present | OK |
| 4 | `x-conduit-token` | `eyJhbGciOiJFUzI1NiIs[REDAC len=350]` | absent — no prepare handshake exists in transport | **MISSING** |
| 5 | `sec-ch-ua` | `"Chromium";v="146","Not-A.Brand";v="24","Google Chrome";v="146"` | absent | MISSING |
| 6 | `x-oai-is-client-observation` | `v1.s.p.xwpD5cMb01QH` (12-char prefix of ~22) | absent | MISSING |
| 7 | `sec-ch-ua-mobile` | `?0` | absent | MISSING |
| 8 | `oai-client-build-number` | `9758774` | absent | MISSING |
| 9 | `openai-sentinel-proof-token` | `gAAAAABWzMwMDAsIk1vb[REDAC len=681]` | present when minted | OK |
| 10 | `accept` | `text/event-stream` | present | OK |
| 11 | `content-type` | `application/json` | present | OK |
| 12 | `oai-client-version` | `prod-180ca8b8699a733aef330b7026892aee9bf85fbe-ctl` | absent | MISSING |
| 13 | `oai-echo-logs` | `0,1527,1,78980` | absent | MISSING |
| 14 | `oai-session-id` | `c17eaaad-abde-[REDAC len=36]` (stable across all turns of the page session) | absent | MISSING |
| 15 | `referer` | `https://chatgpt.com/` | present | OK |
| 16 | `openai-sentinel-chat-requirements-token` | `gAAAAABqi9sYXCwhs8UH[REDAC len=2572]` | present | OK (shape) |
| 17 | `x-oai-turn-trace-id` | `ec22cfd3-e30d-[REDAC len=36]` (one per turn) | absent | MISSING |
| 18 | `x-openai-target-path` | `/backend-api/f/conversation` | absent | MISSING |
| 19 | `oai-telemetry` | `[1,null]` | absent in `_build_headers`; probes sent a timing array | DIFFERENT |
| 20 | `oai-device-id` | `75b27bf0-121c-[REDAC len=36]` | present | OK |
| 21 | `x-openai-target-route` | `/backend-api/f/conversation` | absent | MISSING |
| 22 | `openai-sentinel-turnstile-token` | `ShAcAhcCAQwOG3BwWYcb[REDAC len=12636]` | present when minted | OK |
| 23 | `user-agent` | `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36` | `"Mozilla/5.0"` bare token | **DIFFERENT** |
| — | `openai-sentinel-so-token` | **not sent by the real page either** | omitted | OK (confirmed optional) |

Cookies: Playwright hides the Cookie line from request headers, so names were enumerated
from the context jar (28 cookies for chatgpt.com). Notable ones the real request carries:
`_puid`, `oai-sc` (sentinel session cookie, `0gAAAAABqi9y…`, bound to the requirements token),
`__Secure-oai-is`, `__Secure-next-auth.session-token.{0,1}`, `cf_clearance`, `__cf_bm`,
`_cfuvid`, `__cflb`, `_uasid`, `_umsid`, `__oailb`, `oai-client-auth-info`, `oai-did`.
The synthetic builder sends only `bundle.cookies` + `cf_clearance` — whatever subset the
bundle snapshot captured, typically missing `oai-sc`, `__Secure-oai-is`, `__cf_bm`, `_puid`
freshness. Verdict: **DIFFERENT (subset)**.

## Diff table — JSON body

Real body (1145 bytes), keys in order; synthetic keys marked:

| Key | Real | Synthetic | Verdict |
|---|---|---|---|
| `action` | `"next"` | same | OK |
| `messages[].id` | uuid v4 | uuid v4 | OK |
| `messages[].author.role` | `"user"` | same | OK |
| `messages[].create_time` | `"1787550564.338"` | absent | MISSING |
| `messages[].content` | `{content_type:"text", parts:[…]}` | same | OK |
| `messages[].metadata` | `{selected_sources:[], serialization_metadata:{custom_symbol_offsets:[]}}` | absent | MISSING |
| `parent_message_id` | literal string **`"client-created-root"`** | random uuid | **DIFFERENT** |
| `model` | `"gpt-5-6-thinking"` | model id/label | OK-ish |
| `client_prepare_state` | `"sent"` | absent | MISSING |
| `timezone_offset_min` | `"-420"` | absent | MISSING |
| `timezone` | `"Asia/Saigon"` | absent | MISSING |
| `conversation_mode` | `{kind:"primary_assistant"}` | same | OK |
| `enable_message_followups` | `"True"` | absent | MISSING |
| `system_hints` | `[]` | absent | MISSING |
| `model_response_contracts` | `[{id:"photo_upload_action.v1", protocol_version:"1", presets:[…]}]` | absent | MISSING |
| `supports_buffering` | `"True"` | absent | MISSING |
| `supported_encodings` | `["v1"]` | absent | MISSING |
| `client_contextual_info` | `{is_dark_mode, time_since_loaded, page_height/width, pixel_ratio, screen_*, app_name:"chatgpt.com", has_web_push_capabilities, web_push_notification_permission}` | absent | MISSING |
| `paragen_cot_summary_display_override` | `"allow"` | absent | MISSING |
| `force_parallel_switch` | `"auto"` | absent | MISSING |
| `thinking_effort` | `"standard"` | only when reasoning_effort set | DIFFERENT (conditional) |
| `local_function_names` | `["local.continue_in_work"]` | absent | MISSING |

Note the many string-typed booleans (`"True"`/`"False"`) — the page serializes them as strings.

## Suspects ranked (what most likely causes the authenticated 403)

1. **Missing `x-conduit-token` + the entire `/f/conversation/prepare` handshake.** The authed
   route now binds each turn to a conduit JWT minted seconds earlier via a dedicated endpoint
   the transport never calls. It is per-turn (TTL ≈ 60 s), tied to cluster/conduit_location,
   and appears on both the second `/prepare` call and the conversation call. A missing or
   stale binding is exactly the kind of thing that returns "Unusual activity" instead of a
   plain 400. Highest-probability cause.
2. **Client-fingerprint inconsistency: `User-Agent: "Mozilla/5.0"` bare token vs full UA +
   matching `sec-ch-ua`/`sec-ch-ua-platform`/`sec-ch-ua-mobile`.** A UA that contradicts the
   TLS fingerprint (curl_cffi chrome impersonation) and lacks client hints is a classic
   bot score input. Cheap to fix, high suspicion.
3. **Incomplete cookie envelope.** Missing `oai-sc` (the sentinel-side cookie whose value
   shares the `0gAAAAAB…` family with the chat-requirements token), `__Secure-oai-is`,
   `__cf_bm`, `_puid` etc. If `oai-sc` must pair with the requirements token presented in the
   header, its absence breaks server-side correlation → flagged.
4. **Missing session-correlation headers:** `oai-session-id` (stable per page session),
   `x-oai-turn-trace-id` (per turn), `x-oai-is-client-observation`, `oai-client-build-number`,
   `oai-client-version`, `x-openai-target-path/route`, `oai-echo-logs`. Individually weak,
   collectively a strong "not the real web client" signal.
5. **Body shape.** Missing ~15 fields incl. `client_prepare_state`, timezone pair,
   `client_contextual_info`; `parent_message_id` is the literal `"client-created-root"`, not a
   uuid. Less likely to trigger 403 (servers usually tolerate lean bodies) but should be
   replicated once 1–4 are fixed.
6. **Sentinel tokens themselves — unlikely the cause.** Real proof (681 chars) matches ours;
   turnstile (12 636) matches ours (~12 372); the page does not send `so` either. One nuance:
   real chat-requirements token is 2572 chars vs ~1950 from the repo's
   `/sentinel/chat-requirements/prepare` flow, and the real page pairs it with `oai-sc` —
   covered by suspect 3.

## Practical recipe implied

Mimic the page exactly: `POST /f/conversation/prepare` (Bearer + device id + client headers)
→ take `conduit_token` → send conversation POST with `x-conduit-token`, full UA/client-hint
set, full cookie jar (esp. `oai-sc`), `oai-session-id`/turn-trace ids, and the enriched body.
All mintable headlessly with the existing profile; no new UI prompts needed beyond this probe's one.

## Evidence files (redacted)

- /tmp/hdrdiff_redacted.json — full capture: 3× prepare pairs, 1× conversation pair (headers in original order, redacted bodies)
- /tmp/hdrdiff1_capture.py — capture script (headless, single UI send)
- Raw unredacted capture was chmod 600 and shredded after redaction.
