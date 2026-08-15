# WebGPT Gateway Acceptance Report

Date: 2026-08-15  
Plan: `PLAN_WEBGPT_OPENAI_GATEWAY.md`  
Environment: Linux desktop, Playwright Chromium, ChatGPT Web anonymous mode

## Result

Overall status: **PARTIAL — not a full project PASS yet**.

The core model gateway, strict tool protocol, standard OpenAI client loop, streaming, conversation correlation, and 10-step tool continuation are working. This verification phase intentionally uses ChatGPT Free without login at the user's request. Persistent-auth testing is deferred to a later account-backed phase; anonymous quota stopped the coding-agent benchmark, and the required 50-request soak/recovery matrix was therefore not completed.

An additional operational path is now implemented but not yet account-verified:
`gpt-web brave-launch` starts a dedicated Brave profile with loopback CDP, and
the gateway can attach with `--cdp-url http://127.0.0.1:9222`. It is designed
to preserve the browser-owned login without exporting credentials or cookies.
This does not change the overall status until the authenticated manual matrix is
completed.

## Manual re-verification — 2026-08-15 22:04–22:13 ICT

This pass was performed one request at a time with the real CLI, browser UI, and
`curl`. No `pytest`, acceptance runner, soak script, or scripted oracle was used
as evidence for the results in this section.

| Manual check | Result | Direct observation |
|---|---|---|
| UI reconnaissance | PASS | Real ChatGPT page exposed semantic composer, send button, new-chat link, model picker, and login button; no Cloudflare challenge |
| Auth status | DEFERRED BY USER | This phase intentionally uses ChatGPT Free without login; UI reported `anonymous_free` |
| Persistent startup | ENVIRONMENT BLOCKED | An independent `app.webgpt` process owned the shared persistent profile; it was left untouched and this run used an isolated ephemeral profile |
| Health state | PASS | Before first chat: `browser=not_started`; after chat: `browser=ready`, `backend=ready`, `authenticated=false` |
| Non-stream completion | PASS | HTTP 200 and exact content `MANUAL_GATE_OK_82C1` |
| Dynamic model registry | PASS after fix | Initially advertised unusable `ChatGPT`; fixed endpoint now advertises stable `chatgpt-web` with display name `ChatGPT` |
| Invalid-model recovery | PASS after fix | Fake model returned 400 `model_unavailable`; immediately following `chatgpt-web` request returned exact `MODEL_RECOVERY_OK_2F11` |
| Streaming | PASS after fix, completion-buffered | Initial live run emitted a truncated prefix then DOM-revision error; fixed run emitted two content chunks whose concatenation was exactly `MANUAL_STREAM_CHUNK_ONE_TWO_THREE_FOUR_FIVE_91E7`, then one `stop` and `[DONE]` |
| Tool selection/output shape | PASS | `multiply` returned `assistant.tool_calls`, `finish_reason=tool_calls`, arguments `{"x":37,"y":19}`, and a unique call id |
| Tool-result continuation | PASS | Manually calculated `703`, returned it as `role=tool` with the same call id, and received final content `703` in the same gateway session |
| Multi-turn context | PASS | A later turn after the tool cycle asked for the original operands and returned exact `37 and 19` |
| Ordinary prose with tools present | PASS | `tool_choice=none` produced normal assistant content and `finish_reason=stop`; no tool call was inferred |
| Divergent conversation protection | PASS | A request omitting committed history was rejected instead of being appended to the wrong context |
| Unknown session error | PASS after fix | Initially returned 500; non-stream now matches stream and returns 404 `session_not_found` |
| Rate limit mapping | PASS | Anonymous quota was surfaced as HTTP 429 `rate_limited` |
| Ephemeral browser shutdown | PASS after fix | Removed double-close ownership race; a manual headful probe opened and exited cleanly without Playwright future warnings |

Not re-verified in this manual pass: 10-turn context, page reload/reopen recovery,
three authenticated restarts, 50-request soak, concurrent sessions, 10 sequential
tool cycles, and the full external coding-agent benchmark. Previous automated/live
evidence below remains recorded, but these items are not promoted to a fresh
manual PASS.

## Automated offline checks

```text
pytest -q                         41 passed
ruff check .                     PASS
mypy gpt --ignore-missing-imports PASS
python -m compileall -q gpt      PASS
```

Covered offline:

- redaction including Cloudflare query secrets;
- safe artifact permissions and normalized traces;
- incremental SSE parsing and missing-completion failure;
- state machine and protocol-to-UI fallback;
- fail-closed tool parser malformed/unknown/prose/duplicate fixtures;
- multiple distinct explicit tool calls;
- standard `openai` client 10-step tool loop with correlated IDs;
- explicit-session retry idempotency (same request plus `x-webgpt-session-id` returns cache);
- divergent history and wrong `tool_call_id` rejection;
- system/user/tool message rendering and sentinel injection escaping.

## Live checks performed

| Check | Result | Evidence |
|---|---|---|
| Headless probe | BLOCKED as expected | Cloudflare challenge detected; no bypass attempted |
| Headful probe | PASS | Composer and model picker detected |
| Authentication | DEFERRED for R10 | Profile was intentionally anonymous (`Log in` visible); account-backed phase will be run later |
| Profile permissions | PASS | Profile corrected to mode `0700` |
| Direct UI send/completion | PASS | Exact response `WEBGPT_LIVE_OK_7F31`, ~4.2 s |
| Dynamic model discovery | PASS with current access | Stable selectable alias `chatgpt-web`; current UI label exposed as display name `ChatGPT` |
| `/health` | PASS | Distinguished not-started/ready state without secrets |
| `/v1/models` | PASS | Standard OpenAI client parsed list |
| Non-stream completion | PASS | Standard OpenAI client received `ACK` |
| Multi-turn context | PASS | Follow-up returned remembered marker `WEBGPT_CTX_4C92` |
| Streaming | PASS | Concatenated content `STREAM_OK_91D2`, one `stop` finish; latest implementation buffers mutable DOM until completion before emitting deterministic SSE chunks |
| One tool round | PASS | `multiply(37,19)` call, correlated result, final `703` |
| 10 sequential tool cycles | PASS | Exact call sequence `1..10`, then final concatenated all pieces |
| Read-only external agent | PASS | Agent answer and independent oracle both `b.txt:3:WEBGPT_NEEDLE_91A7` |
| Coding bug-fix agent | FAIL/PARTIAL | Corrected first bug; anonymous quota stopped run; independent verifier: 1 passed, 1 failed |
| Rate-limit behavior | FIXED after observation | `modal-no-auth-rate-limit` now maps to `RateLimited`/HTTP 429 rather than click timeout/500 |
| 50-request soak >=98% | NOT RUN / FAIL gate | Anonymous quota was reached before 50 requests |
| Reload/reopen recovery | NOT VERIFIED | Anonymous conversation had no stable `/c/<id>` identity |
| 2/4 independent session isolation | NOT VERIFIED live | Correctness store tests pass offline; live quota prevented benchmark |
| Persistent login across 3 restarts | DEFERRED | User requested anonymous-free verification first; account-backed phase will cover this gate |

## PASS gate assessment

- Level 1 — Chat Backend: **FAIL** (50-request soak and reload recovery outstanding; authenticated persistence is explicitly deferred to the later account-backed phase).
- Level 2 — Tool Calling: **PASS for exercised V1 path** (including 10 cycles and malformed-output fail-closed).
- Level 3 — External Agent Compatibility: **PARTIAL** (read-only pass; coding task interrupted by backend quota).
- Level 4 — Real Agent Behavior metrics: **NOT ENOUGH RUNS**.
- Level 5 — Hosted Agent Mode: **NOT APPLICABLE** (optional and not implemented).

No full PASS is claimed. For the current anonymous-free phase, the remaining safe work is limited by the upstream quota. In the later account-backed phase, log in manually with `gpt-web setup` (do not pass credentials/cookies to the tool), or use `gpt-web brave-launch` followed by `gpt-web setup --cdp-url http://127.0.0.1:9222`; then run the 3-restart auth test, reload/close recovery, 50-request soak, and five fresh coding-agent runs.
