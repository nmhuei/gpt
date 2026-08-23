# GPT Web Toolkit — Improvement Roadmap

**Created:** 2026-08-16  
**Product decisions confirmed:** OpenAI Responses API, Anthropic Messages API
and Claude Code CLI compatibility are the next protocol priorities; persisted
conversation history keeps a 24-hour TTL; `-p/--password` remains a supported
automation interface.
**Status:** planning baseline; do not treat an item as live-accepted until it is
recorded in `../reports/ACCEPTANCE_REPORT.md` with redacted evidence.

## 1. Product boundary

The V1 product is a local, single-user OpenAI Chat Completions-compatible
gateway backed by a browser-controlled ChatGPT Web session. The stable transport
is the semantic UI driver. Protocol replay remains disabled unless two separate,
redacted browser captures establish a safe replay contract.

### In scope

- OpenAI Chat Completions V1 plus prioritized OpenAI Responses API and
  Anthropic Messages API adapters: messages, supported tools, streaming,
  explicit session correlation, model aliases and reasoning-effort requests.
- A local Claude Code CLI-compatible provider path backed by the same browser
  runtime, with no dependency on an OpenAI or Anthropic hosted API key.
- Browser/session reliability, local CLI, opt-in conversation persistence, and
  artifact redaction.
- Interactive persistent-profile login and the existing optional automated
  username/password/TOTP login flow, hardened for credential safety.
- Offline contract tests and bounded opt-in live acceptance tests.

### Explicitly out of scope for this roadmap

- CAPTCHA or security-challenge solving/bypass.
- Unverified upstream protocol replay.
- Hosted/multi-user service, multi-agent scheduler, uploads, voice, image
  generation, Deep Research, and GPT management.

## 2. Non-negotiable operating policy

`AGENTS.md` is the persistent instruction for future work. Its account policy
is mandatory:

| Live-test category | Required mode | Prohibited use |
| --- | --- | --- |
| Ordinary chat, streaming, tools, reload/recovery, rate-limit and soak | `free_anonymous` — regular non-authenticated Free account | Do not use Plus |
| Model discovery/selection and reasoning-effort selection | `plus_model_matrix` — authenticated Plus account | Do not use for ordinary chat, exploratory prompts or soak |

Every live artifact and acceptance-report entry must state its account mode,
browser/profile mode, exact visible UI label, selector evidence and whether the
browser session was fresh. Live tests are opt-in, bounded and redact artifacts
before retention.

## 3. Baseline already implemented

The following work is present in the working tree. It is offline-verified unless
identified as live evidence in `../reports/ACCEPTANCE_REPORT.md`.

| Area | Implemented change | Main locations |
| --- | --- | --- |
| Test-account governance | Persistent Free/Plus test matrix and live-test instructions | `AGENTS.md`, `tests/live/README.md` |
| Request boundary | Immutable normalized Chat Completions request; validates roles, model, tools, `tool_choice`, stream, temperature and effort | `gpt/api/requests.py` |
| Model contract | Explicit local aliases; `chatgpt-web` keeps the current browser model and never infers account tier/model from a missing picker | `gpt/api/model_registry.py`, `config.example.json` |
| Gateway wiring | Applies model/effort exactly once before sending; parser errors return stable invalid-request responses | `gpt/api/server.py` |
| Tool safety | Validates forced tool selection and unknown tools before prompt rendering | `gpt/api/tool_transpiler.py` |
| Conversation cache | Opt-in JSON persistence with TTL, atomic replace, parent `0700` and file `0600` permissions | `gpt/api/conversations.py` |
| UI model behavior | Missing picker exposes only generic `chatgpt-web`; explicit unavailable model errors instead of false Free/Plus inference | `gpt/drivers/ui.py` |
| Streaming | Mutable-DOM accumulator and regression fixture for text revisions | `gpt/streaming.py`, `tests/fixtures/dom_stream_revisions.json` |
| Session/CLI | Reasoning-effort state; direct send selection errors are no longer swallowed; CLI avoids duplicate selection | `gpt/session.py`, `gpt/debug.py` |
| Credential logging | TOTP code is no longer written to logs; docs now describe automated login truthfully | `gpt/auth.py`, `../guides/DS2API_COMPAT_NOTES.md` |
| Offline gate | Pytest, Ruff, Mypy and compile script | `scripts/verify.sh` |

The latest full offline gate before this roadmap was written passed: **64 tests,
Ruff, Mypy and compileall**. The subsequent edits only changed a log message and
documentation; rerun the gate before the next implementation commit.

## 4. Known gaps and risks

1. No new live matrix has been run for the request/model/persistence changes.
   Therefore the new selector behavior is not a live acceptance result.
2. The current reasoning-effort selector maps several UI terms to broad values
   and uses a class-pattern fallback. It needs exact UI evidence and selected
   state verification before it can be trusted.
3. The conversation store writes a schema version but does not fully validate
   every loaded record, has no cross-process writer lock, and persists a
   monotonic `last_used` value that is not meaningful across process restarts.
4. The HTTP server still owns part of the conversation execution workflow; the
   runtime needs extraction to make the protocol easier to test and reuse.
5. Automated login accepts command-line credentials. This is convenient but a
   password passed on the command line can be observable in a process list.
6. CAPTCHA/security-challenge detection is not yet a complete, tested action
   path. The tool must surface the condition without attempting to solve it.
7. One browser session serializes gateway work. This is correct for V1 but is
   not multi-session or multi-worker support.

## 5. Ordered implementation phases

### Phase 0 — Freeze and reconcile the baseline

**Purpose:** make the existing changes a reliable starting point rather than a
mix of implementation and unqualified claims.

1. Audit every changed/untracked file and classify it as gateway, browser/UI,
   auth/profile, test fixture or documentation.
2. Reconcile README, both historical plans, compatibility notes and acceptance
   report. Each capability must be labeled `offline-tested`, `live-verified`,
   `blocked`, or `deferred`.
3. Retain the existing automated-login capability; remove only outdated claims
   that it does not exist. Keep the separate boundary that CAPTCHA solving is
   not supported.
4. Confirm the V1 decision: local single-user gateway, one physical browser,
   Chat Completions first, semantic UI as default transport.
5. Rerun `bash scripts/verify.sh` and `git diff --check` after reconciliation.

**Exit criteria:** no contradictory documentation; all current tests pass; no
feature is claimed live-verified without an acceptance artifact.

### Phase 1 — Formal gateway/runtime contract

**Purpose:** make HTTP input, conversation execution and browser actions
independently testable.

1. Define a protocol-neutral runtime interface that receives an already parsed
   request and returns a completed response or stream events.
2. Move conversation resolution, model/effort positioning, prompt rendering,
   tool-result correlation and response formatting out of the Starlette route
   orchestration where practical.
3. Complete request compatibility rules:
   - supported fields must be applied or documented as intentionally ignored;
   - unsupported multimodal content and OpenAI fields must fail clearly;
   - `reasoning_effort` and `reasoning.effort` need one precedence rule;
   - streaming/non-streaming must share validation and error mapping.
4. Define stable error responses: invalid request `400`, unknown explicit
   session `404`, divergent history `409`, rate limit `429`, unavailable
   model/UI and browser failures with actionable codes.
5. Define exact idempotency conditions: full canonical history, tool signature,
   selected request model and explicit session ID must all match before cached
   response reuse.
6. Add contract tests using a fake `ChatGPTWebSession`, independent of
   Starlette and Playwright.

**Exit criteria:** same request has the same observable result through HTTP,
runtime tests and fake browser boundary; no business logic is duplicated between
stream and non-stream paths.

### Phase 2 — Evidence-based model and reasoning-effort controls

**Purpose:** select only controls that the actual UI proves exist.

1. Introduce an internal capability record containing exact visible label,
   normalized ID, selected-state evidence, available efforts, UI location and
   observation time.
2. Rework `list_models` to advertise only genuine selectable entries. A picker
   button is a display control, not proof that its label is a selectable model.
3. Rework `select_model` to verify a post-click selected state and return
   `ModelUnavailable`/`UIChanged` rather than assuming click success.
4. Rework `select_reasoning_effort` to use exact visible labels or accessible
   control semantics. Do not collapse `instant`, `low`, `high` and `max` into
   another value unless the UI explicitly represents the requested value that
   way and evidence is recorded.
5. Cache capability discovery only within the browser session; invalidate it on
   reload, UI drift or model-menu failure.
6. Keep aliases as request-name to exact UI-label mappings, never as a
   hard-coded account/model catalog.
7. Add DOM-fixture tests for no picker, nested Advanced menus, missing selected
   state, duplicate labels and unavailable effort.

**Live validation:** run only `plus_model_matrix`, with no ordinary prompts.
Record picker DOM/ARIA evidence redacted for each selectable model and effort.

**Exit criteria:** every advertised selectable model/effort has exact evidence
and a confirmed selected state; unsupported UI controls fail safely.

### Phase 3 — Session lifecycle and streaming correctness

**Purpose:** recover ordinary browser failures without duplicate prompts or
invalid SSE.

1. Write explicit state-transition cases for page closed/crashed, reload,
   network loss, rate limit, auth-required, selector drift, timeout and
   interrupted generation.
2. Implement recovery only when safe: reopen the known conversation when an ID
   exists; never automatically resend a prompt that might already have been
   submitted.
3. Separate mutable browser text observations from gateway SSE output. Gateway
   emits deterministic chunks only after a stable completion, exactly one stop
   chunk and exactly one `[DONE]`.
4. Define timeout stages: composer-ready, submit acknowledged, first assistant
   response, stable completion and close/recovery.
5. Add fixtures for DOM rewrite, duplicate completed event, stop button race,
   incomplete response, reload during generation and response extraction error.
6. Preserve serialized writer behavior for V1 and report queue/concurrency
   semantics explicitly rather than implying multi-session parallelism.

**Live validation:** run bounded `free_anonymous` send, stream, rate-limit,
reload/reopen and recovery cases. Do not use the Plus profile.

**Exit criteria:** no fake/live recovery case can create a duplicate user turn;
SSE contract remains valid after mutable DOM behavior.

### Phase 4 — Conversation persistence and privacy hardening

**Purpose:** make optional local history persistence predictable and safe.

1. Specify a versioned state schema with per-record validation and migration or
discard policy.
2. Persist wall-clock timestamps for expiry/order; do not use monotonic values
across a restart.
3. Add a single-writer/process lock or explicitly reject simultaneous gateways
sharing one store path.
4. Add max record count, max file size, expiration cleanup that is persisted,
   corrupt-file recovery warning and safe atomic replacement on all paths.
5. Document the data classification: cache contains prompt history and cached
   response; browser profile contains browser-owned session state; neither
   exports cookies/tokens.
6. Decide whether a deliberate state-clear CLI is required. If approved,
   resolve the exact path first and use a recoverable delete/move behavior.
7. Add tests for corrupt JSON, schema mismatch, expired record, restart,
   permission failure, interrupted write and competing writers.

**Exit criteria:** a restart preserves only valid unexpired records; malformed
state never prevents starting the gateway or leaks its content into logs.

### Phase 5 — Tool loop and agent compatibility

**Purpose:** preserve fail-closed tool execution while supporting standard agent
loops.

1. Version the tool sentinel rendering/parsing contract and document escaping.
2. Validate tool name, strict JSON arguments, call IDs, duplicate calls, forced
tool selection and pending call/result ordering across turns.
3. Test `tool_choice=none`, `auto`, `required` and forced function behavior
through parser, runtime and OpenAI-shaped output.
4. Protect against pseudo sentinel text embedded in normal prose or code blocks.
5. Add fake-agent scenarios: multi-tool, malformed result, omitted result,
divergent history, retry and repeated tool cycles.
6. Retain standard OpenAI client compatibility test as the main V1 contract;
avoid claiming native ChatGPT Web function calling.

**Live validation:** use `free_anonymous` for one bounded tool round and one
result continuation after offline tests pass.

**Exit criteria:** only valid advertised calls can become `tool_calls`; invalid
or ambiguous output remains assistant text or safe error.

### Phase 6 — Multi-protocol API adapters and Claude Code CLI compatibility

**Purpose:** expose one normalized browser runtime through the protocols clients
actually use, without implementing three independent conversation engines.

1. Define internal protocol-neutral request and response events. They must
   represent system/developer/user/assistant/tool input, model request,
   reasoning effort, stream lifecycle, content, tool calls, usage availability
   and provider-neutral errors.
2. Keep Chat Completions as a compatibility adapter over that runtime; do not
   make it the canonical internal type.
3. Add an OpenAI Responses API adapter:
   - implement only the documented local subset first (`input`, model,
     instructions, tools, stream, previous-response correlation where safely
     mappable);
   - map runtime output to response object/item/event shapes;
   - reject unsupported built-in tools, background mode, encrypted content and
     provider-managed state explicitly rather than silently degrading them;
   - document how Responses IDs map to the local conversation store.
4. Add an Anthropic Messages API adapter:
   - accept `messages`, `system`, model, max tokens, tools, tool choice and
     stream for the supported subset;
   - map stop reasons, content blocks, `tool_use` and `tool_result` correctly;
   - reject server-side tools, prompt caching semantics, batch APIs and fields
     the browser runtime cannot honor;
   - expose an Anthropic-shaped `/v1/messages` endpoint only on localhost.
5. Build a protocol conformance fixture suite from static request/response event
   cases. Tests must prove that Chat Completions, Responses and Messages reach
   the same normalized runtime and cannot bypass tool or history validation.
6. Research Claude Code CLI's currently supported provider configuration from
   its official documentation before implementation. Select a supported local
   endpoint/auth configuration; do not spoof cloud credentials or modify the
   CLI binary.
7. Implement the smallest Claude Code adapter/configuration required by that
   documented integration. Ensure it selects the Anthropic Messages-compatible
   route if that is the officially supported transport, or a dedicated adapter
   only if needed.
8. Add a deterministic local fake-runtime smoke test that runs Claude Code's
   configured request path without a real browser. Then run one bounded
   `free_anonymous` read-only coding task only after the client contract and
   account policy are satisfied.

**Compatibility principles:**

- The gateway must never claim parity with OpenAI or Anthropic beyond the
  supported subset.
- API keys supplied by client libraries are local compatibility placeholders;
  they are not forwarded to an upstream model API or stored.
- A request may use only capabilities the normalized browser runtime can
  represent. Unsupported provider-specific behavior returns a clear protocol
  error.
- All three protocols share the same model-selection, conversation, tool,
  redaction, rate-limit and browser-recovery policy.

**Exit criteria:** official OpenAI and Anthropic client fixtures pass against
the local gateway; Claude Code can complete a bounded read-only task through a
documented local configuration; no real cloud API key is needed or emitted.

### Phase 7 — Authentication, browser profile and credential safety

**Purpose:** preserve automatic login while minimizing secret exposure and
respecting security challenges.

1. Retain the supported automation interfaces: `-p/--password`, credential
   string, stdin and environment variables. Clearly warn that command-line
   passwords can be visible in a local process list and recommend stdin or
   environment variables where that risk matters.
2. Guarantee password, TOTP, cookie, token, URL query secret and browser
storage content are redacted from logs, exceptions, artifacts and JSON output.
3. Implement and test challenge detection that raises an actionable
`CaptchaChallengeError` or equivalent; never attempt a solver or bypass.
4. Verify persistent-profile permissions, ownership, profile-in-use errors and
CDP loopback enforcement.
5. Add fake-page tests for password rejection, required MFA with/without TOTP,
challenge, timeout and already-authenticated profile.
6. Document that automated login creates browser-owned session state only in the
selected profile and does not export it.

**Exit criteria:** no test/log contains a secret; every auth failure maps to a
specific safe operator action.

### Phase 8 — CLI, diagnostics and local operations

**Purpose:** make local deployment debuggable without inspecting source.

1. Validate config file input for aliases, persistence path, TTL and browser
mode before browser startup.
2. Add a read-only diagnostic command that checks browser reachability, CDP
loopback, profile permissions, auth status, composer discovery and picker
availability without sending a prompt.
3. Standardize JSON output for `models`, `send`, `health` and errors with
request/session IDs but without prompts by default.
4. Add structured, redacted logs for state transitions, request timing and
browser mode; include an explicit debug flag for non-sensitive detail.
5. Write operator runbooks for ephemeral Free testing, dedicated Plus picker
testing, profile conflict, anonymous quota and state-store recovery.

**Exit criteria:** a local operator can install, diagnose, start, stop and
recover the gateway from documented commands alone.

### Phase 9 — Verification, acceptance and release hygiene

**Purpose:** ensure implementation claims match evidence.

1. Run `bash scripts/verify.sh` and `git diff --check` after every implementation
phase.
2. Keep browser live tests outside the normal unit suite and make their request
budget explicit.
3. Run Free acceptance in this order: health/probe, non-stream chat, stream,
rate-limit mapping, one tool round, one continuation, reload/reopen, bounded
soak.
4. Run Plus acceptance only for picker/effort discovery and selection.
5. Redact artifacts before storage and append a precise result to
`../reports/ACCEPTANCE_REPORT.md`; blocked/quota-limited results remain blocked, never
promoted to PASS.
6. Reconcile README and historical plans after each release-sized phase so they
distinguish code present, offline verification and live acceptance.

**Exit criteria:** acceptance report is reproducible, account-policy compliant
and has no unsupported success claim.

## 6. Dependencies and implementation order

```text
Phase 0 baseline
  -> Phase 1 gateway contract
     -> Phase 2 model/effort controls
     -> Phase 3 lifecycle/streaming
        -> Phase 5 tool/agent loop
        -> Phase 6 multi-protocol adapters and Claude Code
     -> Phase 4 persistence/privacy
  -> Phase 7 auth/profile hardening
  -> Phase 8 operations
  -> Phase 9 acceptance
```

Phases 2 and 3 require separate live matrices. No Plus live session is used
outside Phase 2. Phases 4, 6, 7 and 8 can be implemented and tested offline in
parallel with evidence preparation, but acceptance is only recorded in Phase 9.

## 7. Decisions requested before deep implementation

1. For Claude Code, which version/channel should be treated as the acceptance
   target? The supported provider configuration must be verified against its
   official documentation at implementation time.
2. Should the gateway expose Responses and Anthropic APIs on one port with
   protocol-specific routes, or should each adapter have an independently
   launchable local listener?
