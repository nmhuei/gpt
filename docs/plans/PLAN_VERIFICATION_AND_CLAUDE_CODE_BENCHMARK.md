# GPT Web Toolkit — Verification and Claude Code Benchmark Plan

**Created:** 2026-08-16  
**Purpose:** Code is not accepted merely because it compiles or unit tests pass. This document defines the manual evidence, automated contracts, and an end-to-end Claude Code CLI benchmark required to validate the tool.

## 1. Acceptance rules

1. Every capability has separate states: **implemented**, **offline-verified**, and **live-verified**. Only the last state supports a live reliability claim.
2. `PASS` requires expected result, process exit status, protocol output, and required evidence. Quota, unavailable UI, or missing evidence is `BLOCKED` or `FAIL`, never an implicit pass.
3. Each live/manual run records test ID, revision, timestamp, account mode, browser mode, command, redacted request/response, evidence, and cleanup.
4. Client API keys are local compatibility placeholders. They must never be sent to or stored for OpenAI/Anthropic cloud APIs.
5. Tests use isolated profiles, state stores, worktrees, and local ports. They must not disturb a user browser, shared profile, tunnel, or repository files.

## 2. Mandatory account policy

| Test group | Required mode | Permitted work |
| --- | --- | --- |
| Chat, Responses API, Anthropic API, Claude Code, stream, tools, recovery, rate limit, and soak | `free_anonymous` | Fresh isolated ephemeral Free browser profile |
| Model discovery/selection and reasoning-effort selection | `plus_model_matrix` | Dedicated authenticated Plus profile; picker actions only |
| Automated login | Explicitly approved non-Plus test profile | Authentication flow only |

The Plus profile must never be used for normal prompts, Claude Code, tools, soak, retry experiments, or exploratory chat. Every artifact names its account mode.

## 3. Verification infrastructure to implement first

### 3.1 Layout and isolation

~~~
tests/
  contract/       # protocol fixtures; no browser
  integration/    # gateway with fake browser runtime
  live/           # named, opt-in browser cases
  benchmark/
    relayqueue_spec.md
    grader/       # generated outside agent workspace when run
scripts/
  verify.sh
  verify-contracts.sh
  run-live-case.sh
  run-claude-code-benchmark.sh
~~~

- Each run creates explicit temporary directories for profile, state store, worktree, and reports.
- Listeners bind only to loopback; the runner captures PID and closes only processes it started.
- Live test selection is explicit (for example, `run-live-case.sh M-CHAT`), not an accidental batch.
- Benchmark grader is outside Claude Code's writable project directory.
- Reports/artifacts are redacted, gitignored, and contain no browser storage, password, TOTP, cookie, token, or raw network capture.

### 3.2 Evidence record

Each completed card writes a redacted JSON result:

~~~
{
  "id": "M-CHAT-001",
  "status": "pass|fail|blocked",
  "account_mode": "free_anonymous",
  "protocol": "chat_completions|responses|anthropic_messages",
  "browser_mode": "ephemeral|persistent|cdp",
  "started_at": "ISO-8601",
  "duration_seconds": 0,
  "request_count": 0,
  "expected": "short contract",
  "observed": "redacted result",
  "artifacts": ["redacted relative paths"],
  "cleanup": "passed|failed"
}
~~~

## 4. Offline contract catalogue

All cases below run before a browser/live test. New bugs always add a regression fixture crossing a public boundary, not only a mock of implementation details.

| ID | Subject | Required coverage |
| --- | --- | --- |
| OFF-REQ | Normalized request | Roles/content, invalid JSON/body, aliases, effort precedence, stream type, ignored temperature, and error shape |
| OFF-CONV | Runtime/conversation | New/history append, explicit ID, retry idempotency, divergent history, tool/model mismatch, TTL/restart |
| OFF-TOOLS | Tool safety | All `tool_choice` forms, malformed sentinel, prose/code sentinel, bad JSON, duplicate and forged IDs, multi-turn result |
| OFF-STREAM | SSE | Prefixes, DOM revisions, incomplete response, duplicate completion, cancellation, one stop, and one `[DONE]` |
| OFF-UI | UI driver | Missing composer, login, rate limit, no picker, nested menu, stale locator, selected-state failure, and reload |
| OFF-SESSION | State machine | Crash, timeout, protocol fallback, no unsafe resend, selected model/effort exactly once, and send serialization |
| OFF-STORE | Persistence | Schema validation, corruption, expiry, atomic write, permissions, size bounds, competing writers, and secret-free errors |
| OFF-AUTH | Login/profile | Parsing, redaction, MFA, rejected login, challenge detection, timeout, authenticated profile, and loopback CDP |
| OFF-OAI | OpenAI adapters | Chat Completions + Responses request/event/error mappings and no bypass of normalized runtime |
| OFF-ANT | Anthropic adapter | Messages, system, stream ordering, tool use/result, stop reasons, and unsupported fields |
| OFF-CC | Claude Code shim | Configuration, local placeholder auth, fake runtime smoke path, timeout, and worktree isolation |

Required gate:

~~~
bash scripts/verify.sh
bash scripts/verify-contracts.sh
git diff --check
~~~

## 5. Manual verification cards

### M-BOOT — Startup, health, and cleanup

**Mode:** no account before browser interaction.

**Procedure:** start gateway with fresh temporary profile and no persistent store; call health/doctor before and after the first request; verify loopback binding; stop the owned process; check port/profile cleanup.

**Pass evidence:** documented state transitions, no false authenticated status, structured redacted diagnostics, and no leaked process.

### M-CHAT — OpenAI Chat Completions

**Mode:** `free_anonymous`.

**Procedure:** use official OpenAI client and raw HTTP for one bounded marker prompt; verify status, response object, finish reason, and `x-webgpt-session-id`; send full-history follow-up; retry same request with explicit session ID; send divergent history.

**Pass evidence:** correct response and context, one logical user turn after retry, and divergent history returns safe conflict.

### M-STREAM — OpenAI SSE

**Mode:** `free_anonymous`.

**Procedure:** capture raw SSE for a short marker and markdown/code response; parse output with a separate verifier; test early client disconnect.

**Pass evidence:** valid JSON chunks, aggregate final text, one finish chunk, one `[DONE]`, no mutable-DOM revision leakage, and safe cleanup.

### M-RESP — OpenAI Responses API

**Mode:** `free_anonymous`.

**Procedure:** use official OpenAI client against local endpoint; test text input, instructions, stream events, prior-response correlation, and supported function-tool round trip; submit background mode, hosted tools, encrypted content, and unsupported multimodal input.

**Pass evidence:** supported subset has documented item/event shapes; unsupported features return explicit local errors; no client key occurs in artifacts.

### M-ANTH — Anthropic Messages API

**Mode:** `free_anonymous`.

**Procedure:** use official Anthropic SDK or wire-compatible fixture client against local `/v1/messages`; test system text, user content, event order, tool use, and `tool_result`; submit server tools, batches, prompt cache, and unsupported blocks.

**Pass evidence:** valid supported Messages behavior through the same browser runtime; clear unsupported errors; no cloud request is made.

### M-MODEL — Picker and effort matrix

**Mode:** `plus_model_matrix` only; no chat prompt.

**Procedure:** record redacted DOM/ARIA evidence; discover models; select every advertised label; close/reopen picker to verify selected state; repeat for each effort; test unavailable values; reload and rediscover.

**Pass evidence:** all advertised controls are confirmed real; absent picker never causes tier/model inference; failures leave session usable.

### M-RECOVERY — Browser/session reliability

**Mode:** `free_anonymous`.

**Procedure:** send one marker; reload between turns; close a gateway-owned page or use controlled test hook; force timeout/selector failure; submit a later request.

**Pass evidence:** safe documented recovery, no ambiguous prompt resend, and no duplicate user turn.

### M-TOOLS — Tool-loop safety

**Mode:** `free_anonymous`.

**Procedure:** expose deterministic arithmetic tool; validate tool call and call ID; submit matching result; then use fixtures for malformed JSON, unknown tool, forged result ID, and sentinel-looking prose/code.

**Pass evidence:** only valid advertised calls become executable `tool_calls`.

### M-STORE — Persistence/privacy

**Mode:** `free_anonymous` for bounded chat; otherwise local-only.

**Procedure:** start with isolated explicit store and 24-hour TTL; complete one turn; restart and continue; load corrupt and expired copies; check only permissions and redacted metadata in report.

**Pass evidence:** valid history restores; bad state cannot block startup or leak content; store/profile cleanup follows runner policy.

### M-AUTH — Automated-login safety

**Mode:** approved non-Plus test profile only.

**Procedure:** exercise already-authenticated, rejected password, MFA required with/without code, timeout, and security challenge; inspect redacted output for secrets.

**Pass evidence:** actionable classified error, no CAPTCHA solving/bypass, no secret in process output/logs/artifacts. This card is blocked until a dedicated test account is approved.

## 6. Stability suites

### S-LOCAL — Deterministic stress

With fake browser runtime run 1,000 mixed request parses, 500 resolve/commit/restart cycles, 100 revision/failure stream sequences, 100 tool continuations, store corruption/competing-writer cases, and concurrent HTTP requests. Report duration, failure categories, leaked tasks/processes, and memory growth where practical.

### S-BROWSER — Bounded browser soak

**Mode:** `free_anonymous`. Run only after M-CHAT, M-STREAM, M-TOOLS, and M-RECOVERY pass. Set fixed budget before start (initial recommendation: 10–20 short turns), alternate stream/non-stream, include at most one tool loop, and stop immediately on rate limit. Rate limit is `BLOCKED`, not a reason to use Plus.

## 7. Claude Code CLI integration benchmark

### 7.1 Objective

Validate the Anthropic-compatible local gateway with a real coding client. Claude Code CLI is the implementer only; an immutable independent black-box grader determines success. No human repair, second prompt, follow-up agent pass, or grader edit is allowed after the initial assignment.

### 7.2 Project assignment: RelayQueue

Claude Code creates a Python 3.10+ standard-library-only project named `relayqueue`: a durable SQLite task queue CLI. Standard-library runtime removes package-download availability as a confounding dependency.

Public CLI:

~~~
python -m relayqueue --db <path> init
python -m relayqueue --db <path> enqueue --payload <json> [--key <idempotency-key>]
python -m relayqueue --db <path> claim --worker <name> [--lease-seconds <n>]
python -m relayqueue --db <path> ack --task <id> --lease-token <token>
python -m relayqueue --db <path> fail --task <id> --lease-token <token> --retry-after <seconds>
python -m relayqueue --db <path> list [--state ready|leased|succeeded|dead]
python -m relayqueue --db <path> stats
~~~

Success writes exactly one JSON object to stdout. Errors write structured JSON to stderr and use non-zero exit status. Logging cannot contaminate stdout.

Required behavior:

1. `init` is idempotent and creates versioned schema metadata.
2. `enqueue` accepts JSON object payloads, persists a task, and implements idempotency: same key/same payload returns same task; different payload fails.
3. `claim` atomically leases oldest eligible task, returns unguessable lease token, honors expiry, and returns documented empty result when no task exists.
4. `ack` accepts only current token and changes one task to succeeded; wrong, duplicate, or expired acknowledgement cannot mutate state.
5. `fail` increments attempts, schedules retry, and moves task to `dead` at configurable documented max attempts.
6. State survives process restart; expired lease becomes eligible without losing attempts; competing workers cannot both successfully claim one task.
7. SQL uses parameters; no `eval`, shell execution, or external runtime package.
8. Project provides README, own tests, and practical type annotations.

### 7.3 Immutable agent prompt and sandbox

Runner writes the full RelayQueue specification read-only outside the target worktree. Claude Code receives exactly this instruction:

> Implement RelayQueue in the assigned empty project directory according to the immutable specification. Create source, tests, README, and project metadata only inside that directory. Run your own tests. Do not access or modify the grader, runner, gateway configuration, parent directory, or files outside the assigned project. Stop when implementation is complete.

The target project cannot edit grader files. The runner asks no question and provides no repair prompt after launch.

### 7.4 Gateway and Claude Code preparation

1. At execution time, verify official current Claude Code documentation for supported provider/base-URL configuration; record CLI version and exact documented configuration in artifact. Do not guess or patch the CLI.
2. Start local Anthropic Messages endpoint on loopback with temporary state store and fresh `free_anonymous` profile.
3. Configure only the benchmark child process with a local placeholder key. Do not use cloud Anthropic credentials.
4. Run fake-runtime Claude Code smoke test first to validate endpoint, headers, event parsing, and workspace isolation without consuming Free quota.
5. Run one real implementation attempt with one clean worktree, no manual interaction, maximum 45 minutes, and fixed gateway request budget. Stop on rate limit, repeated protocol failure, or browser safety failure.

### 7.5 Independent black-box grader

The external grader runs after Claude Code exits and owns separate SQLite files per case.

| ID | Black-box check |
| --- | --- |
| RQ-01 | Clean-process `python -m relayqueue --help` and initialization |
| RQ-02 | Schema version and idempotent `init` |
| RQ-03 | One JSON stdout object on success; structured stderr/non-zero on error |
| RQ-04 | Invalid JSON/scalar/missing input never mutates database |
| RQ-05 | FIFO eligible claim order |
| RQ-06 | Same-key idempotency and different-payload conflict |
| RQ-07 | Wrong, duplicate, and expired lease-token safety |
| RQ-08 | Lease-expiry requeue behavior |
| RQ-09 | Retry delay, attempt count, and dead-letter transition |
| RQ-10 | Restart durability |
| RQ-11 | Four worker processes claim 100 seeded tasks without duplicate success |
| RQ-12 | Adversarial SQL/shell-like payload/key remains data only |
| RQ-13 | Agent tests pass; no outside-worktree modifications; no `eval`, shell execution, or external runtime dependency |

Grader uses process timeouts and retains failing worktree/database evidence for review.

### 7.6 Benchmark result

`PASS` requires all RQ-01 through RQ-13 checks, one unassisted Claude Code run through the local gateway, no cloud provider key, no out-of-worktree change, clean browser/gateway shutdown, and a redacted `free_anonymous` artifact.

Partly correct code, human help, rate limit, altered grader, or protocol failure is `PARTIAL`, `FAIL`, or `BLOCKED` as appropriate. Never retry with Plus.

After one pass, repeat the immutable task three times on fresh sessions/days, subject to Free quota. Report pass rate, median duration, request count, browser recovery count, gateway failures, and every grader failure. One pass proves basic compatibility; three independent passes provide initial stability evidence.

## 8. Final release gate

1. Offline contract and quality gates pass.
2. M-BOOT, M-CHAT, M-STREAM, M-RESP, M-ANTH, M-TOOLS, M-STORE, and M-RECOVERY have live evidence or explicit documented block reason.
3. M-MODEL passes only in `plus_model_matrix` and contains no chat prompt.
4. Claude Code fake-runtime smoke test passes before real benchmark.
5. No secret-leak finding remains open.
6. `../reports/ACCEPTANCE_REPORT.md` records executed test IDs and evidence status without overwriting historical results or claiming unsupported provider parity.
