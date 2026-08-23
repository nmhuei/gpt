# WebGPT Gateway Acceptance Report

Date: 2026-08-15  
Plan: `../plans/PLAN_WEBGPT_OPENAI_GATEWAY.md`<br>
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
pytest -q                         78 passed (development update 2026-08-16)
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
- request normalization, explicit model aliases, opt-in conversation-store persistence/TTL, and mutable-DOM stream revision fixtures.

The 2026-08-16 development update did not run live browser checks. Future live
checks must use `free_anonymous` for ordinary chat/reliability scenarios and
`plus_model_matrix` only for model-picker/reasoning-effort scenarios; the
artifact and acceptance entry must state that mode.

## Protocol adapter and Claude Code development verification — 2026-08-16

| Check | Result | Evidence |
| --- | --- | --- |
| Responses API adapter | OFFLINE PASS | Request/response/stream and previous-response fixtures pass through the shared fake browser runtime. |
| Anthropic Messages adapter | OFFLINE PASS | Text, SSE event shape, tool-use/tool-result continuation and `claude-*` protocol-model mapping are covered by contract tests. |
| M-BOOT browser doctor | PASS (`free_anonymous`) | Fresh ephemeral headful browser reported `auth_status=anonymous`, semantic composer available and connected browser; no prompt or picker action was sent. |
| M-CHAT Free smoke | PASS (`free_anonymous`) | Ephemeral headful CLI request returned exact `WEBGPT_FREE_SMOKE_OK` in 10,058 ms; no Plus profile was used. |
| Claude Code local fake smoke | PASS | Claude Code 2.1.233 completed a local fake `ANTHROPIC_BASE_URL` call using `HEAD /api/hello` and `POST /v1/messages?beta=true`; no browser or cloud provider API was used. |
| Claude Code RelayQueue browser benchmark | BLOCKED | Ran only with `free_anonymous`, never Plus. Tool-result correlation and transcript deduplication failures were fixed with regression tests; later attempts reached the tool loop but hit anonymous Free rate limit/5xx before a project could be graded. Runner stops on 429 or repeated gateway 5xx, kills its owned Claude child, and preserves artifacts. No benchmark PASS is claimed. |

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

No full PASS is claimed. For the current anonymous-free phase, the remaining safe work is limited by the upstream quota. In the later account-backed phase, establish an authenticated profile with `gpt-web setup`, or use `gpt-web brave-launch` followed by `gpt-web setup --cdp-url http://127.0.0.1:9222`; then run the 3-restart auth test, reload/close recovery, 50-request soak, and five fresh coding-agent runs.


## Implementation/debug update — 2026-08-16 18:14–18:25 ICT

Scope: apply the SuperPro/V2 hardening work directly in `/home/light/GitHub/gpt` while treating the authenticated Plus profile as already working and focusing live checks on Free/anonymous.

### Code changes completed in this pass

- Restored automated username/password/TOTP login after operator feedback. `AutoLoginManager.login()` and `generate_totp_code()` are supported again for normal sign-in; CAPTCHA, phone verification, Turnstile, and security-challenge bypass remain out of scope and must surface clear operator action.
- Added worker-factory prewarming: `ChatGPTWorkerFactory.start()` now creates the configured warm workers once, and the API lifespan starts the factory when `--max-workers > 1`.
- Added mandatory manual-verification status tracking: `gpt.verification` now exposes default manual requirements and `gpt-web manual-status` reports missing/pass/fail coverage.
- Fixed trace-file writing when `--trace-file` is inside a policy-owned parent such as `/tmp`; parent `chmod` permission errors no longer fail requests, while the trace file itself is still mode `0600`.

### Automated verification

```text
bash scripts/verify.sh
→ 141 passed
→ ruff check . PASS
→ mypy gpt --ignore-missing-imports PASS
→ python -m compileall -q gpt PASS
```

Focused regression checks:

```text
pytest tests/test_auth.py tests/test_factory.py tests/test_tracing.py tests/test_verification.py -q
→ 17 passed
```

### Live Free/anonymous checks

No Plus profile was used in this pass.

```text
gpt-web doctor --free --headful --browser --timeout-ms 8000
→ ok=true
→ auth_status=anonymous
→ composer=available
```

Direct CLI smoke:

```text
gpt-web send --free --headful --json --text "Reply exactly: WEBGPT_FREE_IMPL_SMOKE_8A2D"
→ text=WEBGPT_FREE_IMPL_SMOKE_8A2D
→ state=ready
→ duration_ms=3581
```

API smoke with ephemeral Free/anonymous browser:

```text
GET /healthz
→ 200 {"ok":true,"status":"ok"}

GET /v1/models
→ 200; stable model alias chatgpt-web

POST /v1/chat/completions
→ content=WEBGPT_API_FREE_SMOKE_4C1F
→ model=chatgpt-web
→ finish_reason=stop
```

Trace/ledger artifact check:

```text
trace lines = 7
trace mode = 600
conversation store mode = 600
```

### Manual verification status

`gpt-web manual-status` currently reports all 12 required manual gates as `MISSING`. This is intentional and correct under the new rule: automated tests and live smoke checks are evidence, but they do not promote a feature to DONE until a human/operator records at least one manual verification pass for the relevant gate.

### Current status after this pass

- Plus/authenticated path: treated as previously working; not retested here.
- Free/anonymous boot/composer: LIVE AUTO PASS.
- Free/anonymous direct CLI chat: LIVE AUTO PASS.
- Free/anonymous OpenAI API chat: LIVE AUTO PASS after trace chmod fix.
- Heavy Free/anonymous benchmark/soak: still expected to be limited by upstream anonymous quota and remains not fully accepted.
- Full project PASS: still blocked by manual verification records and larger recovery/soak/agent benchmarks.

## Implementation/debug update — 2026-08-16 18:35–18:50 ICT

Scope: restored automated login policy after operator correction; tested Claude Code direct API path; reduced prompt-send overhead.

### Rule correction

- Restored `AutoLoginManager` and `gpt-web login` to the original automated username/password/TOTP behavior.
- `gpt-web login --help` again exposes `-u/--username`, `-p/--password`, `-2fa/--two-factor`, `--cred`, `--stdin`, and env-based credential input.
- Updated `AGENTS.md`: auto-login/TOTP is allowed for normal sign-in. CAPTCHA, phone verification, Turnstile and security-challenge bypass remain disallowed and must surface operator action.

### Claude Code direct API verification

- Fake Anthropic smoke: Claude Code 2.1.233 called `HEAD /api/hello` and `POST /v1/messages?beta=true`; result `OK`; PASS.
- Browser-backed Free/anonymous gateway: started `gpt-web api-server --ephemeral --headful --prewarm` on a temporary port; `/readyz` returned `ready=true`, `auth_status=anonymous`; Claude Code returned exact `WEBGPT_CLAUDE_DIRECT_FREE_7D9E`; PASS.
- Evidence from live run: `duration_api_ms=4113`, `time_to_request_ms=13`, trace file mode `0600`, conversation store mode `0600`, trace lines `7`.
- This was live automated evidence, not a MANUAL_PASS. Manual verification still must be recorded separately before claiming full DONE.

### Prompt latency reduction

- Added API-server `--prewarm` option so browser/session startup happens during daemon boot instead of the first request.
- Optimized `CompletionRuntime.position_session`: a freshly booted blank ChatGPT page no longer pays an unnecessary `new_conversation()` click before the first prompt.
- Added selected model/reasoning-effort cache checks in `ChatGPTWebSession`: if the requested model or effort is already selected, the UI picker/slider is not opened again.
- Current recommended low-latency path is a long-running daemon, not repeated `gpt-web send` process invocations:
  `python -m gpt.debug api-server --port 4000 --persistent --headful --prewarm` for an authenticated profile, or `--ephemeral` for Free/anonymous smoke.

### Automated gates after changes

```text
python -m pytest tests/test_auth.py -q                         2 passed
python -m pytest tests/test_session.py tests/test_model_effort.py -q 16 passed
bash scripts/verify.sh                                         140 passed; ruff PASS; mypy PASS; compile PASS
```

## Tool-debug update — Claude Code PCAP benchmark, XML prompt-debug pass

Date: 2026-08-16 ICT

Scope:
- Tested Claude Code CLI against the local WebGPT Anthropic-compatible gateway using Free/anonymous ChatGPT Web.
- Project target: PCAP Analysis Automation benchmark from SPEC.md.
- Constraint preserved: Claude Code CLI was the only actor allowed to create project files; no direct manual implementation of the benchmark project was performed.

Gateway/tooling changes implemented:
- Added `--prompt-debug-dir` and `WEBGPT_PROMPT_DEBUG_DIR` for redacted pre-GPT prompt dumps.
- Each prompt sent from the Claude Code/API adapter to ChatGPT Web can now be inspected as a mode-0600 file with size/hash/tool_count/session metadata.
- Added `prompt_debug_written` structural trace event linking a trace sequence to the prompt-debug file path.
- Changed tool prompt priority to plain XML `<tool_calls><invoke><parameter><![CDATA[]]>` for Claude Code; DSML remains parser compatibility only.
- Added controller-correction behavior for tool refusal / false completion after forced Read.
- Added detection for post-Read work that still requires Bash/Edit/Write.
- Added Bash heredoc repair for collapsed one-line heredocs and `mkdir -p` prelude insertion for `cat > package/file.py` targets.

Automated repo gate:
- `bash scripts/verify.sh` PASS
- 143 tests passed
- ruff PASS
- mypy PASS
- compile PASS

Live evidence:
- Mini Claude Code `Read -> Bash side effect` PASS: file was created and response returned the exact requested marker.
- Mini Claude Code `Read -> Python file -> compileall` PASS: `hello.py` was created with valid indentation and `python -m compileall -q hello.py` passed.
- Prompt-debug files were written with mode 0600 under the run prompt_debug directory and showed the exact prompt converted from Claude Code to ChatGPT Web.

PCAP benchmark run status:
- Claude Code connected to `/v1/messages?beta=true` and used Free/anonymous ChatGPT Web.
- Claude Code created a partial PCAP project tree in project-run11, including package files, tests, README, and pyproject.
- The generated project did not pass compile/pytest: many source/test files still had indentation errors from model-generated large heredoc content.
- Final continuation hit Free/anonymous rate limiting: gateway log contained `429 Too Many Requests`, followed by repeated 502 failures from retries.

Current conclusion:
- Gateway transport/tool loop is materially improved and prompt debugging is now inspectable.
- The full PCAP benchmark is not yet accepted because generated project code does not compile and Free/anonymous rate limit stopped further live repair loops.
- Next engineering target is to reduce large heredoc failure modes by enforcing smaller file batches / stronger repair or adapter-side command validation, and to add a rate-limit circuit breaker so Claude Code does not retry into repeated 502 after a real 429.


## Master execution implementation update — 2026-08-17 07:46 ICT

Current certification policy is now strictly `free_anonymous`; authenticated Free/Plus/Pro sessions are invalid for live certification even if older historical sections of this report mention authenticated testing.

Implemented in this pass:
- Re-check anonymous auth state on every live session lease; if a previously valid browser becomes authenticated, discard it and fail closed with `anonymous_session_unavailable`.
- Add a host-wide cross-process Free-anonymous gateway lock under `~/Downloads/webgpt/tmp/` so separate runners cannot generate concurrently.
- Replace repeated one-second `/readyz` polling with lightweight `/healthz` polling followed by one bounded readiness probe; this prevents browser-init backlogs when the UI is unavailable.
- Add `scripts/verify-soak-restart.sh` for the required bounded 10 text / 10 sequential-tool / 5 multi-turn-tool / 3 tiny-project soak plus restart verification. It stops immediately on anonymous 429 rather than cycling profiles or retrying indefinitely.
- Add `scripts/manual-verify-claude.sh` to produce a real Claude Code verification candidate while keeping `MANUAL_PASS` a separate direct-review action.
- Make `scripts/run-pcap-benchmark.sh` a compatibility wrapper over the canonical clean-room `run-pcap-certification.sh`; removed the old five-attempt same-workspace retry loop.
- Move legacy Claude benchmark temporary state from `/tmp/webgpt-*` into `~/Downloads/webgpt/`.
- Update the golden baseline so a freshly booted blank ChatGPT page is not forced through New Chat before its first prompt.
- Remove trace-test coroutine warnings by avoiding invocation of async `drain_events` mocks from the synchronous trace drain path.
- Harden virtual `Write`: resolve the requested target from the real client cwd, reject symlink escapes, validate source/JSON/TOML before replacement, write a temp file, `fsync`, then `os.replace` atomically.
- Make retry semantics explicit: `rate_limit`, auth, conflict, malformed tool, and `commit_unknown` are non-retryable; transient browser/timeout/capacity faults remain retryable and expose `x-should-retry` consistently across OpenAI/Anthropic adapters.
- Add a unified `/v1/*` request trace envelope with request/client/protocol/session/conversation/turn/timing/tool/correction/status/error fields; PCAP `request-summary.json` now emits the required aggregate schema.
- Move `--final` manual coverage validation before any browser/Claude launch. Final mode now requires complete `manual-verification.jsonl` coverage via `manual-status --require-pass`, then derives the aggregate `MANUAL_PASS.txt` marker.

Automated evidence:

```text
for f in scripts/*.sh; do bash -n "$f"; done
python -m py_compile scripts/wait-for-anonymous-ready.py
bash scripts/verify.sh
→ 196 passed
→ ruff PASS
→ mypy PASS
→ compileall PASS
→ no RuntimeWarning
```

Current live status:
- Fresh anonymous baseline is blocked by upstream anonymous quota/login-wall redirect (`RateLimited`), as recorded in `GATEWAY_CERTIFICATION.md`.
- Per the amended master plan, a pre-workflow rate-limit may replace the ephemeral anonymous session exactly once for the entire run. The fresh-session diagnostic was also rate-limited, so Phase 1 remains blocked; PCAP, soak, final certification, and `MANUAL_PASS` must not advance. No further session/profile/IP/browser-identity cycling is allowed for that run.
- Therefore this update is an **implementation/local-regression PASS**, not a final gateway certification PASS.
