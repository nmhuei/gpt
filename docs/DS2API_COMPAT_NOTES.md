# DS2API compatibility notes

## Scope

This project presents an OpenAI-compatible facade over ChatGPT Web.  It does
not copy ds2api's credential or token model.

## What was examined

The upstream [ds2api repository](https://github.com/CJackHwang/ds2api) logs
into the DeepSeek API directly: `internal/deepseek/client/client_auth.go`
sends an email/mobile and password payload, receives a service token, then the
account resolver refreshes and persists that token.  That is specific to
DeepSeek's documented/observed API contract; it is not a reusable ChatGPT Web
authentication mechanism.

The useful architectural lessons retained here are:

| ds2api pattern | WebGPT equivalent |
| --- | --- |
| one explicit authentication boundary | browser profile is the authentication boundary |
| session/token refresh is owned by the upstream client | ChatGPT Web owns session refresh in the browser |
| completion runtime is isolated from transport | `ChatGPTWebSession` isolates UI/protocol driver from gateway |
| adapters normalize to an OpenAI-shaped API | `gpt.api.server` maps the supported OpenAI subset |

`ds2api`'s "solver" is not a Turnstile/CAPTCHA solver: it solves DeepSeek's
server-issued `DeepSeekHashV1` proof-of-work nonce and emits an
`x-ds-pow-response` header.  ChatGPT Web neither exposes that DeepSeek
protocol nor needs this component in the browser-driven gateway.

## Authentication decision

ChatGPT Web uses its own browser session and security challenges.  The gateway
therefore does not accept, export, or persist passwords, TOTP secrets, cookie
dumps, access tokens, or browser `storage_state` snapshots.

Two supported modes preserve login across gateway restarts:

1. `gpt-web setup` launches a dedicated persistent profile for a user to
   authenticate in the normal browser UI.
2. `--cdp-url http://127.0.0.1:9222` attaches to a user-started Chromium/Brave
   profile.  The CDP endpoint is restricted to loopback because it grants full
   browser access.  On shutdown the tool drops its Playwright connection and
   does not close the user's browser or its default context.

Codex's device/browser OAuth flow likewise cannot be converted into a ChatGPT
Web cookie: it is authorization for Codex services, not a transferable
ChatGPT-Web browser session.  The comparable design principle is user-mediated
browser authorization followed by durable local session ownership.

## Compatibility boundaries

Supported gateway behavior remains limited to the documented V1 mapping:

- chat messages and conversation correlation;
- dynamic model discovery from the visible UI;
- best-effort `tools` sentinel translation, fail-closed;
- buffered OpenAI-shaped SSE when `stream=true`.

It deliberately does not claim compatibility for direct upstream protocol
replay, account rotation, cookie/token import/export, uploads, voice, image
generation, Deep Research, GPTs, or CAPTCHA/security-challenge automation.
