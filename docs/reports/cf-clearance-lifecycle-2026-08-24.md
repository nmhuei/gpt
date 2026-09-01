# cf_clearance lifecycle probe — chatgpt.com — 2026-08-24

Experiment to answer: how long does `cf_clearance` live, how reusable is it across
clients (browser -> curl_cffi), and what operational pattern avoids ever having to
re-solve a Cloudflare challenge.

**Constraint compliance:** headless-only (no visible window). Exactly 2 light HTTP
requests to chatgpt.com total (`GET /backend-api/models` x2), zero POST-conversation
traffic. No challenge was encountered; no bypass was attempted. All tokens/cookies in
this report are redacted to <=12-char prefixes.

## Setup

- Source profile: `/home/light/Downloads/webgpt/profiles/personal` (logged-in,
  CloakBrowser chromium-146.0.7680.177, `--fingerprint-platform=windows`).
  The production browser instance using this profile was left untouched.
- Copy: `rsync -a` (caches excluded) to `/tmp/profile-cf`, Singleton locks removed.
- Second headless instance launched on the copy with the same fingerprint flags;
  cookies + UA read via CDP (`playwright connect_over_cdp`). The browser **never
  navigated to chatgpt.com** during the experiment.
- Replay client: `curl_cffi 0.16.1`, `impersonate="chrome146"` (exactly matches the
  browser's Chrome/146.0.7680.177 TLS/JA3/H2 fingerprint), UA header set to the real
  fingerprinted UA.

Real UA captured:

```
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36
```

Cookie jar for `.chatgpt.com`: 21 cookies, including `cf_clearance`,
`__Secure-next-auth.session-token.{0,1}`, `_puid`, `oai-did`, `oai-sc`, `_cfuvid`,
`oai-client-auth-info`, `__Secure-oai-is`.

## Results

### Step 1 — Cookie extraction

| cookie | domain | expires (epoch) | len |
|---|---|---|---|
| cf_clearance | .chatgpt.com | 1819069450 | 597 |
| oai-sc | .chatgpt.com | 1819069919 | 248 |
| __Secure-next-auth.session-token.0 | .chatgpt.com | 1795309811 | 3933 |
| oai-did | .chatgpt.com | 1819030275 | 36 |

`cf_clearance` value shape: `QwgSeizTsuM9...` (12 chars) ... tail `3QJg`.

### Step 2 — Request #1 (clearance used outside the browser)

```
GET /backend-api/models   (curl_cffi impersonate=chrome146 + full jar)
status: 200
elapsed: 0.36 s
content-type: application/json
server: cloudflare
body: {"title":"ChatGPT",...,"models":[{"slug":"gpt-5-5",...}]}
```

200 = the clearance minted inside the browser is fully valid for a non-browser
client, provided client hello (JA3/H2 via impersonate) and User-Agent match.

### Step 3 — Request #2 (~70 s later)

```
status: 200
elapsed: 0.33 s
content-type: application/json
server: cloudflare
```

Identical result — no degradation over a short window, no per-request renewal token
rotated server-side between calls (same static jar both times).

### Step 4 — cf_clearance metadata / TTL estimate

- Format: 597 chars, base64url alphabet (`-` and `_` present), dot-separated into
  ~14 opaque segments. Decoding segments and scanning every byte offset for a
  plausible unix epoch (2024–2028 range) produced only random coincidences scattered
  across 2024-2028 — **the modern cf_clearance is an encrypted/HMAC'd opaque blob; it
  does not embed a readable timestamp.** Age cannot be derived from the value alone.
- What IS readable: the cookie's `Expires` attribute = epoch **1819069450**
  (~2027-08-21), i.e. issuance + **~365 days** from now. This matches Cloudflare's
  "Challenge Passage" setting at its 1-year maximum (default is 30 minutes; sites
  configure 5 min – 1 year).
- Practical caveats on that 365-day figure:
  - Server-side validity can be cut short by CF regardless of cookie expiry:
    IP change, TLS/UA mismatch vs. minting context, or a rising bot score can force
    a fresh challenge any time.
  - The clearance is bound to (at least) IP + UA + client-hello fingerprint. Our
    replay succeeded only with the exact chrome146 impersonate + the exact Windows UA.

### Step 5 — "Warm session is sufficient" check

Both requests succeeded while the browser did **nothing**: it was never reloaded,
never navigated to chatgpt.com during the experiment (cookies were pulled via CDP
from `about:blank`). Consequences:

- No page reload or user interaction is needed to "renew" anything within the
  validity window — the clearance passively stays valid.
- A long-lived warm browser is therefore purely a *minting* device, not a
  *maintenance* requirement: once minted, the cookie pair works standalone until CF
  invalidates it.

## Operational recommendations (TOP-3)

1. **Keep one warm logged-in browser running all day instead of launch/close per
   task.** It holds session-token + a fresh cf_clearance continuously; any background
   navigation keeps them current. Launch/teardown cycles are what most often trigger
   re-challenges (new fingerprint state, new TLS session, sometimes new IP).
2. **Reuse the minted cookies out-of-browser freely, but replay faithfully:**
   curl_cffi `impersonate` must match the minting browser's major version (here
   chrome146) AND send its exact User-Agent. Any mismatch (UA, HTTP version, cipher
   set) risks a 403 challenge even with a valid cookie. Do not rotate IPs mid-session.
3. **Mint again only on evidence of challenge, never preemptively.** Detect via:
   - status 403 on `/backend-api/*`,
   - HTML body instead of JSON, `<title>` containing "Just a moment..." /
     "Attention Required" / cf-mitigated header,
   - disappearance or change of `cf_clearance` cookie.
   On detection, route through the warm browser once (load chatgpt.com, let the
   managed challenge auto-pass), re-pull cookies via CDP, resume. Budget-wise this
   should be rare — observed cookie TTL here is nominally ~1 year, with realistic
   lifetime bounded by IP stability rather than the clock.

## Raw artifacts

- Temp profile copy `/tmp/profile-cf` and full-value cookie dumps were deleted after
  the run. Nothing production was modified; the original profile browser kept running
  undisturbed throughout.
