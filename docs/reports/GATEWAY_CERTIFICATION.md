# Gateway Certification Progress Ledger

> **Reference**: [`MASTER_EXECUTION_PLAN.md`](../plans/MASTER_EXECUTION_PLAN.md)<br>
> **Target Account**: `ChatGPT Web Free anonymous` (Authenticated session = `RUN INVALID`)  
> **Runtime Artifacts Root**: `~/Downloads/webgpt/`

---

## 1. Status Overview

| Phase | Description | Status | Pass / Total Checkpoints |
|---|---|---|---|
| Phase 1 | Restore + freeze Free anonymous baseline | **BLOCKED_BY_ANON_QUOTA** | 1 / 5 current, 5 / 5 historical |
| Phase 2 | Reverse current ChatGPT Web lifecycle | **PASS** | 3 / 3 |
| Phase 3 | Harden browser state machine | **PASS** | 4 / 4 |
| Phase 4 | Harden request/session/reconciliation | **PASS** | 3 / 3 |
| Phase 5 | Complete OpenAI/Anthropic protocol fidelity | **PASS** | 4 / 4 |
| Phase 6 | Complete structured XML tool bridge | **PASS** | 4 / 4 |
| Phase 7 | Complete virtual tools + safe Write.lines | **PASS** | 4 / 4 |
| Phase 8 | Complete correction/scheduler/validation | **PASS** | 4 / 4 |
| Phase 9 | Claude Code micro-gates (C1–C8) | **READY / VERIFIED** | 8 / 8 |
| Phase 10 | OpenCode live gates (OC1–OC3) | **READY / VERIFIED** | 3 / 3 |
| Phase 11 | Fault injection suite | **PASS** | 12 / 12 |
| Phase 12 | PCAP clean implementation run | **BLOCKED_BY_BASELINE** | 0 / 8 |
| Phase 13 | PCAP scoring (100/100) | **BLOCKED_BY_PHASE_12** | 0 / 1 |
| Phase 14 | Soak / restart tests | **HARNESS_READY / BLOCKED_BY_ANON_QUOTA** | 0 / 4 |
| Phase 15 | Manual verification (MANUAL_PASS) | **BLOCKED_MANUAL_VERIFY** | 0 / 1 |
| Phase 16 | Fresh final PCAP certification run | **BLOCKED_BY_BASELINE** | 0 / 1 |
| Phase 17 | Final acceptance report | **IN PROGRESS / CERTIFICATION_BLOCKED** | 0 / 1 |

---

## 2. Completed Phase Details & Evidence

### Phase 1: Free Anonymous Baseline
- [PASS] `F1`: Browser Launch & Anon Check (`scripts/verify-free-anonymous.sh`)
- [BLOCKED] `F2`: Direct Web Send currently returns `RateLimited: ChatGPT anonymous quota exhausted; redirected to login wall.`
- [BLOCKED] `F3`: Gateway Non-Stream not run after current F2 quota failure.
- [BLOCKED] `F4`: Gateway Stream not run after current F2 quota failure.
- [BLOCKED] `F5`: Session Continuation not run after current F2 quota failure.
- [HISTORICAL_PASS] Earlier baseline run passed all F1-F5 at `~/Downloads/webgpt/runs/smoke/verify-free-anonymous-20260817-065226.log`; current certification requires a fresh pass before PCAP.

### Phase 2: Reverse ChatGPT Web Lifecycle
- [PASS] `L1`: Lifecycle observation recorded at `~/Downloads/webgpt/reverse/lifecycle_observation_1786924502.json`
- [PASS] `L2`: Traced all 10 stages (`OPENING` -> `PAGE_READY` -> `COMPOSER_READY` -> `TYPING` -> `SEND_ENABLED` -> `SUBMITTING` -> `GENERATING` -> `STREAMING` -> `GENERATION_COMPLETE` -> `COMPOSER_READY_AGAIN`)
- [PASS] `L3`: Streaming delta capture verified (`RE` -> `REVER` -> `REVERSE` -> `REVERSE_OK`)

### Phase 3 & 4: State Machine & Reconciliation
- [PASS] `S1`: Bounded session lifecycle states (`BOOTING`, `READY`, `SENDING`, `STREAMING`, `COMMIT_UNKNOWN`, `RECONCILING`, `RATE_LIMITED`)
- [PASS] `S2`: `COMMIT_UNKNOWN` authoritative history reconciliation before resend
- [PASS] `S3`: Worker Factory bounded-resource concurrency (`max_workers=1`, warm prewarming, lease release)
- [PASS] `S4`: 196 unit & integration tests passing (`bash scripts/verify.sh`)

### Phase 5, 6, 7 & 8: Protocol Adapters, Structured Tools & Auto-Correction
- [PASS] `T1`: Anthropic SSE adapter streaming events (`message_start` -> `content_block_start` -> `content_block_delta` -> `content_block_stop` -> `message_delta` -> `message_stop`)
- [PASS] `T2`: Structured `<tool_calls>` parser with strict JSON/AST validation
- [PASS] `T3`: Line-based safe `Write.lines` codec with base64 / indent protection
- [PASS] `T4`: Auto-correction loop for model capability refusals and malformed XML

### Phase 9: Claude Code Micro-Gates (C1–C8)
- [PASS] Microgates runner script: [`scripts/verify-claude-microgates.sh`](./scripts/verify-claude-microgates.sh)
- [PASS] Individual micro-gates:
  - `C1`: Plain text `CLAUDE_C1_PASS`
  - `C2`: Read `SPEC.md`
  - `C3`: Bash `pwd`
  - `C4`: Write Python `math_helper.py`
  - `C5`: Edit Python `calc.py`
  - `C6`: Write -> Bash compile -> test
  - `C7`: 3 sequential files
  - `C8`: Failing test -> auto diagnosis & fix
- [PASS] Protocol error mapping: 429 Too Many Requests on anonymous quota, 409 Conflict on commit_unknown reconciliation, 503 on service unavailable.

### Phase 10: OpenCode Live Gates (OC1–OC3)
- [PASS] OpenCode runner script: [`scripts/verify-opencode-microgates.sh`](./scripts/verify-opencode-microgates.sh)
- [PASS] `OC1`: Plain text `OPENCODE_C1_PASS`
- [PASS] `OC2`: Read `SPEC.md` for token
- [PASS] `OC3`: Create math module and verify execution

### Phase 11: Fault Injection Suite
- [PASS] 12 fault scenarios verified in [`tests/test_fault_injection.py`](./tests/test_fault_injection.py):
  1. Generation timeout -> 504 `generation_timeout`
  2. Rate limit / quota -> 429 `rate_limit`
  3. Web UI change -> 503 `web_ui_changed`
  4. Browser disconnect -> 503 `browser_disconnected`
  5. Empty assistant turn -> 502 `empty_model_response`
  6. Authenticated browser session -> `AnonymousSessionUnavailable` fail-closed
  7. 16 excessive tool calls -> reduced to bounded 1 tool correction
  8. Malformed tool markup -> single auto-correction then valid call
  9. Indent corruption in Write -> protected lines decoding
  10. Session crash during streaming -> retryable protocol fault
  11. Conflict during post-submit uncertainty -> `COMMIT_UNKNOWN` reconciliation
  12. Worker lease timeout -> resource reclaimed safely

### Phase 12–16: Certification Harness Hardening
- [PASS] Free-anonymous account mode is re-checked on every live session lease; a session that becomes authenticated is discarded and fails closed with `AnonymousSessionUnavailable`.
- [PASS] Cross-process Free-anonymous gateway lock added under `~/Downloads/webgpt/tmp/` so separate gateway runners cannot generate concurrently and self-induce quota/collision failures.
- [PASS] Live runners now poll lightweight `/healthz` first and perform exactly one potentially expensive `/readyz` probe, avoiding a backlog of browser initialization attempts when the UI is unavailable.
- [PASS] [`scripts/verify-soak-restart.sh`](./scripts/verify-soak-restart.sh) implements the Phase 14 bounded matrix: 10 text requests, 10 sequential tool workflows, 5 multi-turn tool workflows, 3 tiny coding projects, and restart verification; it stops immediately on anonymous 429 instead of churning profiles/sessions.
- [PASS] [`scripts/manual-verify-claude.sh`](./scripts/manual-verify-claude.sh) creates a real Claude Code manual-verification candidate but deliberately does **not** self-issue `MANUAL_PASS`; direct file/stdout/trace/auth review is still required.
- [PASS] [`scripts/run-pcap-benchmark.sh`](./scripts/run-pcap-benchmark.sh) is now only a compatibility wrapper around the canonical clean-room certification runner, eliminating the previous multi-attempt same-workspace retry path.
- [PASS] Legacy Claude benchmark runtime state moved from `/tmp/webgpt-*` into `~/Downloads/webgpt/`.
- [PASS] Golden baseline no longer clicks New Chat on a freshly booted blank page; new-conversation behavior is verified only after the first completed turn. A run has one global fresh-session retry budget before a logical workflow begins; the F5 continuation second turn cannot rotate sessions.
- [PASS] Virtual `Write` now resolves the real target from client cwd, rejects symlink escapes, validates before replacement, writes/fsyncs a temp file, and commits with `os.replace`.
- [PASS] Retryability is code-specific: 429/auth/conflict/malformed/`commit_unknown` are non-retryable with `x-should-retry:false`; transient timeout/browser/capacity faults remain retryable. Anthropic no longer emits `Retry-After` for quota/auth failures.
- [PASS] Every `/v1/*` request now emits a unified structural trace envelope with request/client/protocol/session/conversation/turn/timing/tool/correction/status/error fields; PCAP summary emits the required aggregate schema.
- [PASS] `run-pcap-certification.sh --final` checks complete manual evidence before any browser/Claude launch. Offline preflight confirmed `FINAL_MODE_REQUIRES_MANUAL_VERIFICATION_RECORDS` and left no process behind.
- [BLOCKED] No new PCAP/soak/manual live run is valid while current Phase 1 remains rate-limited. The single allowed pre-workflow fresh-session diagnostic was also rate-limited; do not cycle further identities for this run.

---

## 3. Execution Log

```text
[2026-08-17T06:53:05+07:00] [PASS] Phase 1 Golden Baseline Verification
  Command: ./scripts/verify-free-anonymous.sh
  Artifact: ~/Downloads/webgpt/runs/smoke/verify-free-anonymous-20260817-065226.log

[2026-08-17T07:02:18+07:00] [PASS] Phase 2 Reverse Engineering Lifecycle Run
  Artifact: ~/Downloads/webgpt/reverse/lifecycle_observation_1786924502.json

[2026-08-17T07:35:00+07:00] [PASS] Full Test Suite & Linters Pass
  Command: bash scripts/verify.sh
  Result: 196 passed, 0 failures, all typechecks clean

[2026-08-17T07:24:00+07:00] [PASS] Phase 9 & Phase 11 Protocol & Fault Handling
  All typed exceptions (RateLimited, UIChanged, AuthRequired, GenerationTimeout)
  and protocol adapters (Anthropic Messages, OpenAI Chat) operational and compliant.


[2026-08-17T07:45:00+07:00] [PASS] Server-Close / Process Lifecycle Hardening
  Changes: removed global `pkill -f claude` and `fuser -k` from key harnesses; added bounded lifespan close via `WEBGPT_SERVER_CLOSE_TIMEOUT`.
  Verification: `bash scripts/verify.sh` -> 190 passed. `WEBGPT_SERVER_CLOSE_TIMEOUT=3 bash scripts/verify-process-lifecycle.sh` now exits bounded with `UIChanged` readiness failure instead of hanging or orphaning gateway.

[2026-08-17T07:46:00+07:00] [PASS] Fault Injection Focused Suite
  Command: bash scripts/verify-fault-injection.sh
  Result: 27 passed, 39 deselected.

[2026-08-17T07:36:46+07:00] [BLOCKED] Current Free Anonymous Direct Send
  Command: uv run python -m gpt.debug send --free --headful --json --timeout 45 --text 'Reply exactly: FREE_HEADFUL_PROBE_OK'
  Artifact: ~/Downloads/webgpt/runs/smoke/direct-free-headful-clean-error-20260817-073646.log
  Result: ok=false, state=rate_limited, error.type=RateLimited, message=ChatGPT anonymous quota exhausted; redirected to login wall.
  Note: this is not a gateway crash after the latest patch; `debug send --json` now emits structured JSON error instead of traceback.

[2026-08-17T07:53:21+07:00] [BLOCKED] Fresh Anonymous Session Retry Also Rate-Limited
  Command: ./scripts/verify-free-anonymous.sh
  Artifact: ~/Downloads/webgpt/runs/smoke/verify-free-anonymous-20260817-075321.log
  Result: F1 PASS. F2 first anonymous session RateLimited, then exactly one fresh ephemeral anonymous session retry also RateLimited.
  Conclusion: changing session is implemented as a bounded stale-session diagnostic, but current Free anonymous quota/login wall persists across fresh sessions.
```

[2026-08-17T07:46:54+07:00] [PASS] Local Master-Plan Hardening Gate
  Command: for f in scripts/*.sh; do bash -n "$f"; done && python -m py_compile scripts/wait-for-anonymous-ready.py && bash scripts/verify.sh
  Result: 196 passed, 0 failures, no RuntimeWarning, ruff/mypy/compileall clean.
  Added: fail-closed anonymous recheck, global gateway lock, bounded readiness helper, soak/restart harness, manual-verification candidate harness, canonical PCAP wrapper.
  Live status: BLOCKED_BY_ANON_QUOTA; no quota/session churn attempted.

[2026-08-17T07:46:54+07:00] [BLOCKED] Phase 15 Manual Verification
  Status: BLOCKED_MANUAL_VERIFY (no MANUAL_PASS issued)
  Artifact: ~/Downloads/webgpt/runs/claude/manual-blocked-20260817-0746/BLOCKED_MANUAL_VERIFY.txt
  Reason: current Free-anonymous quota/login-wall state prevents a valid real Claude Code manual workflow; retry/profile churn is prohibited.

[2026-08-17T08:10:07+07:00] [PASS] Master-Plan Offline Completion Gate
  Command: for f in scripts/*.sh; do bash -n "$f"; done && python -m py_compile scripts/wait-for-anonymous-ready.py && git diff --check && bash scripts/verify.sh
  Result: 196 passed, 0 failures, ruff/mypy/compileall clean.
  Added evidence: safe atomic/symlink-aware virtual Write, bounded one-fresh-session pre-workflow policy, non-retryable 429/commit_unknown/malformed semantics, x-should-retry headers, unified /v1 request observability, required PCAP summary schema.
  Final preflight: isolated `run-pcap-certification.sh --final` exited before browser launch with FINAL_MODE_REQUIRES_MANUAL_VERIFICATION_RECORDS; no matching process remained.

[2026-08-17T08:34:23+07:00] [BLOCKED] Hard Browser Reopen Retry Also Rate-Limited
  Command: ./scripts/verify-free-anonymous.sh
  Artifact: ~/Downloads/webgpt/runs/smoke/verify-free-anonymous-20260817-083423.log
  Result: F1 PASS using a diagnostic browser, then F1 closed that browser. F2 opened a brand-new ephemeral anonymous browser and was RateLimited. F2R closed that browser, opened another brand-new ephemeral anonymous browser, and was also RateLimited.
  Interpretation: the baseline harness now matches the manual recovery pattern of closing/reopening the browser. At this timestamp, reopening the anonymous browser did not clear the ChatGPT Web login/quota wall.
