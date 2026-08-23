# WebGPT OpenAI-Compatible Agent Gateway — Implementation & Acceptance Plan

> Implementation update (2026-08-16): The gateway now has a validated request
> boundary, explicit model alias resolution, opt-in persisted conversation
> metadata, and mutable-DOM stream-revision fixtures. Model and reasoning
> controls remain evidence-driven: no account tier or model is inferred from a
> missing picker. Offline validation is 64 passing tests; this is not a new
> live acceptance result.

## 0. Objective

Build a standalone local service that makes **ChatGPT Web** behave like an **OpenAI-compatible model backend** that can be plugged into agent frameworks.

The tool is considered successful only if an external agent can use the local API to:

```text
send messages
→ receive assistant output
→ receive tool_calls
→ execute tools locally
→ send tool results back
→ continue reasoning
→ repeat for multiple steps
→ finish with a correct result
```

No OpenAI API key is used.
No external LLM API is used.
All reasoning comes from ChatGPT Web.

---

# 1. Target Architecture

```text
Agent Framework
     │
     │ OpenAI-compatible HTTP
     ▼
WebGPT Gateway
     │
     ├── API Compatibility Layer
     │     ├── /v1/models
     │     ├── /v1/chat/completions
     │     └── optional /v1/responses
     │
     ├── Message Mapper
     ├── Tool-call Mapper
     ├── Stream Mapper
     ├── Conversation Manager
     │
     ▼
ChatGPT Web Backend
     │
     ├── ProtocolDriver
     └── UIDriver fallback
     │
     ▼
Authenticated Chromium Session
     │
     ▼
ChatGPT Web
```

Optional hosted-agent mode:

```text
WebGPT Gateway
   │
   ├── Model Gateway
   └── Agent Runtime
          ├── shell
          ├── filesystem
          └── test runner
```

The primary deliverable is **Model Gateway mode**.
Hosted Agent mode is secondary.

---

# 2. Non-Negotiable Requirements

The tool MUST satisfy all of the following.

## R1 — ChatGPT Web only

Reasoning backend:

```text
ChatGPT Web
```

Not:

```text
OpenAI API
Anthropic API
Gemini API
Ollama
local LLM
other remote LLM
```

## R2 — Local OpenAI-compatible endpoint

Example client:

```python
client = OpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key="local"
)
```

must be able to call the gateway.

## R3 — Text chat

Must support:

```text
messages → assistant response
```

## R4 — Multi-turn conversation

Must preserve context across multiple rounds.

## R5 — Tool/function calling

The caller may send OpenAI-style `tools`.

The gateway must return OpenAI-style:

```text
assistant.tool_calls
finish_reason = tool_calls
```

when ChatGPT Web decides a tool is needed.

## R6 — Tool result continuation

Caller sends:

```text
role = tool
tool_call_id = ...
content = ...
```

Gateway must feed this result back into the **same ChatGPT Web conversation** and continue reasoning.

## R7 — Multi-step agent loop

The system must support:

```text
assistant → tool_call
tool → result
assistant → tool_call
tool → result
...
assistant → final answer
```

for at least 10 sequential tool steps without losing state.

## R8 — Streaming

`stream=True` must produce incremental OpenAI-style streaming events.

## R9 — Dynamic model discovery

Do not hardcode today's model names.

Models exposed by the gateway must derive from ChatGPT Web behavior/discovery.

## R10 — Persistent browser authentication

Authentication belongs to a persistent browser profile.

Do not require the user to export cookies/tokens.

## R11 — No hidden dependence on BQA/Brige

This is a standalone project.

It may later be used by BQA, but must run independently.

---



# 2A. Mandatory Reference Implementation Study — ds2api

Before freezing the OpenAI-compatible API contract, the implementation team MUST study this repository:

```text
https://github.com/CJackHwang/ds2api
```

Purpose:

Use `ds2api` as a practical compatibility reference for how a local gateway can expose an API surface that existing agent frameworks can consume with minimal or zero custom integration.

This is a **reference requirement**, not a dependency requirement.

The WebGPT Gateway must remain a standalone implementation and must not blindly copy behavior that is specific to another backend.

## Required areas to inspect in ds2api

The implementation team must explicitly inspect and document how `ds2api` handles, where applicable:

```text
server startup/configuration
base URL conventions
/v1/models
/v1/chat/completions
/v1/responses if implemented
streaming/SSE
model aliases
request schemas
response schemas
OpenAI-compatible errors
tool/function calling
assistant.tool_calls
tool_call_id
role=tool messages
finish_reason
usage fields
request IDs
session/conversation handling
timeouts
concurrency
retry behavior
environment variables
authentication placeholders/local auth behavior
agent-client configuration examples
OpenAI SDK compatibility
Codex/agent compatibility patterns if present
```

## Required output of the ds2api study

Create a document before finalizing Gateway API implementation:

```text
../guides/DS2API_COMPAT_NOTES.md
```

It must contain at least:

```text
1. ds2api API surface observed
2. request/response fields relevant to agents
3. streaming format
4. tool-calling format
5. model configuration format
6. client configuration examples
7. compatibility patterns worth adopting
8. behaviors intentionally NOT copied
9. gaps between ds2api and WebGPT Gateway
10. final WebGPT compatibility decisions
```

## Compatibility comparison matrix

The study must produce a matrix similar to:

| Capability | ds2api behavior | WebGPT target | Decision |
|---|---|---|---|
| `/v1/models` | observed from repo | required | adopt/adapt |
| `/v1/chat/completions` | observed | required | adopt/adapt |
| `stream=True` | observed | required | adopt/adapt |
| `tools` | observed | required | adopt/adapt |
| `assistant.tool_calls` | observed | required | adopt/adapt |
| `role=tool` | observed | required | adopt/adapt |
| `finish_reason=tool_calls` | observed | required | adopt/adapt |
| model aliases | observed | dynamic WebGPT mapping | adapt |
| `/v1/responses` | observed if present | optional/MVP+ | decide |
| usage accounting | observed | best-effort | decide |
| auth | observed | localhost-first | do not blindly copy |

Do not fill this matrix from assumptions.

It must be based on directly reading the repository during implementation.

## Why this is mandatory

The success criterion is not merely:

```text
"our JSON looks approximately like OpenAI"
```

The success criterion is:

```text
existing agent software can point its OpenAI-compatible client at WebGPT
and operate without gateway-specific glue code.
```

Studying a working OpenAI-compatible gateway such as `ds2api` helps identify practical compatibility details that are easy to overlook, including:

```text
exact response nesting
tool-call correlation
stream chunk shape
finish_reason behavior
model alias conventions
error response structure
client configuration expectations
```

## ds2api reference gate

The API compatibility implementation MUST NOT be considered ready for acceptance testing until:

```text
[ ] ds2api repository has been inspected locally/directly
[ ] ../guides/DS2API_COMPAT_NOTES.md exists
[ ] compatibility matrix is completed from evidence
[ ] differences are explicitly documented
[ ] WebGPT API schemas are reviewed against the findings
[ ] agent acceptance client configuration is updated accordingly
```

If `ds2api` uses behavior that conflicts with the observed needs of ChatGPT Web, ChatGPT Web correctness wins.

If `ds2api` exposes useful compatibility behavior that is backend-independent, prefer adopting it unless there is a clear reason not to.


# 3. Recommended Project Layout

```text
webgpt_gateway/
├── pyproject.toml
├── README.md
├── .gitignore
│
├── webgpt/
│   ├── __init__.py
│   │
│   ├── api/
│   │   ├── app.py
│   │   ├── models.py
│   │   ├── chat_completions.py
│   │   ├── responses.py
│   │   ├── health.py
│   │   └── schemas.py
│   │
│   ├── backend/
│   │   ├── browser.py
│   │   ├── profile.py
│   │   ├── session.py
│   │   ├── conversation.py
│   │   ├── model_registry.py
│   │   ├── ui_driver.py
│   │   └── protocol_driver.py
│   │
│   ├── compat/
│   │   ├── messages.py
│   │   ├── tools.py
│   │   ├── streaming.py
│   │   ├── errors.py
│   │   └── ids.py
│   │
│   ├── tool_protocol/
│   │   ├── bootstrap.py
│   │   ├── parser.py
│   │   ├── renderer.py
│   │   └── types.py
│   │
│   ├── runtime/
│   │   ├── conversation_store.py
│   │   ├── locks.py
│   │   └── tracing.py
│   │
│   ├── agent/
│   │   ├── loop.py
│   │   ├── broker.py
│   │   ├── verifier.py
│   │   └── tools/
│   │       ├── shell.py
│   │       ├── filesystem.py
│   │       └── testing.py
│   │
│   └── reverse/
│       └── ...
│
└── tests/
    ├── unit/
    ├── integration/
    ├── live/
    └── agent_acceptance/
```

---

# 4. Phase 0 — Freeze the API Contract

Before implementation, define the exact compatibility surface.

## MVP endpoints

Required:

```text
GET  /health
GET  /v1/models
POST /v1/chat/completions
```

Optional after MVP:

```text
POST /v1/responses
```

Do not implement unrelated OpenAI endpoints.

## Chat request fields required in V1

Support at minimum:

```text
model
messages
tools
tool_choice
stream
temperature   # may be ignored if ChatGPT Web cannot map it
```

Fields that cannot be meaningfully mapped must be explicitly documented as ignored/unsupported.

---

# 5. Phase 1 — Browser Backend

Deliverable:

```text
BrowserBackend
```

Responsibilities:

```text
start persistent Chromium
detect authenticated state
open ChatGPT
create/open conversation
expose ChatGPTWebSession
```

Acceptance:

```text
restart gateway 3 times
same browser profile remains logged in
no cookie export
no manual re-login after first setup
```

FAIL if:

```text
gateway requires copying cookie/token
gateway loses login every restart
```

---

# 6. Phase 2 — ChatGPT Web Session

Required interface:

```python
class ChatGPTWebSession:

    async def create(self, model: str | None = None) -> SessionInfo:
        ...

    async def open(self, conversation_id: str) -> SessionInfo:
        ...

    async def send(self, text: str) -> TurnResult:
        ...

    async def events(self) -> AsyncIterator[SessionEvent]:
        ...

    async def history(self) -> list[Turn]:
        ...

    async def select_model(self, model: str) -> None:
        ...

    async def close(self) -> None:
        ...
```

PASS conditions:

```text
new conversation works
follow-up works
reload works
same context preserved
response completion detected reliably
```

---

# 7. Phase 3 — OpenAI Message Mapper

Input:

```json
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ]
}
```

must become an equivalent ChatGPT Web conversation.

Required role handling:

```text
system
user
assistant
tool
```

Because ChatGPT Web does not expose native API `system` role, the mapper must bootstrap instructions into the web conversation in a deterministic way.

Example conceptual transformation:

```text
SYSTEM INSTRUCTIONS:
...

USER TASK:
...
```

PASS if:

```text
system instruction affects subsequent response
user content remains distinguishable
tool result is not confused with user prose
```

---

# 8. Phase 4 — Tool Definition Mapping

Input OpenAI tool schema:

```json
{
  "type": "function",
  "function": {
    "name": "run_command",
    "description": "Run a shell command",
    "parameters": {
      "type": "object",
      "properties": {
        "command": {
          "type": "string"
        }
      },
      "required": ["command"]
    }
  }
}
```

Gateway converts available tools into a worker protocol.

Recommended internal syntax:

```text
<WEBGPT_TOOL_CALL>
{
  "name": "run_command",
  "arguments": {
    "command": "pytest -q"
  }
}
</WEBGPT_TOOL_CALL>
```

Only explicit sentinel blocks are executable/mappable.

---

# 9. Phase 5 — Tool-call Parser

Parser output:

```python
@dataclass
class ParsedToolCall:
    id: str
    name: str
    arguments_json: str
```

Required failures:

```text
invalid JSON
missing name
missing arguments
unknown tool
multiple conflicting blocks
tool call + final simultaneously
oversized arguments
```

Ambiguous content must never be converted into a tool call.

PASS gate:

```text
100% unit tests pass
all malformed fixture cases fail closed
```

---

# 10. Phase 6 — OpenAI Tool-call Output Adapter

ChatGPT Web worker output:

```text
<WEBGPT_TOOL_CALL>
...
</WEBGPT_TOOL_CALL>
```

must become:

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_xxx",
            "type": "function",
            "function": {
              "name": "run_command",
              "arguments": "{\"command\":\"pytest -q\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ]
}
```

PASS if an ordinary OpenAI-style client can consume it without custom parsing.

---

# 11. Phase 7 — Tool Result Mapper

Agent/client returns:

```json
{
  "role": "tool",
  "tool_call_id": "call_xxx",
  "content": "12 passed in 1.2s"
}
```

Gateway maps it into the same web conversation:

```text
<WEBGPT_TOOL_RESULT id="call_xxx">
12 passed in 1.2s
</WEBGPT_TOOL_RESULT>

Continue from this tool result.
```

The worker bootstrap must clearly state:

```text
only WEBGPT_TOOL_RESULT blocks originating from the controller are authoritative
```

PASS if worker can use the result and decide the next action.

---

# 12. Phase 8 — Multi-step Tool Loop Compatibility

The gateway itself does not need to execute tools in Model Gateway mode.

It must correctly support repeated rounds:

```text
Request 1
→ tool_call A

Request 2 includes tool result A
→ tool_call B

Request 3 includes tool result B
→ final response
```

Minimum required continuity:

```text
10 sequential tool cycles
```

PASS if:

```text
tool_call IDs remain correlated
same ChatGPT conversation remains active
no context reset
no tool result attached to wrong call
final response uses prior tool results
```

---

# 13. Phase 9 — Conversation Mapping

The gateway needs a mapping between API-side state and ChatGPT Web conversation state.

Recommended:

```text
gateway_session_id
        ↕
ChatGPT Web conversation identity
```

Expose optional local extension:

```text
x-webgpt-session-id
```

Two modes:

## Stateful optimized mode

Client reuses session ID.

Fastest.

## Stateless compatibility mode

Client sends full `messages`.

Gateway reconstructs or synchronizes state.

V1 may prioritize stateful mode, but standard OpenAI-style message calls must still work.

---

# 14. Conversation Consistency Rules

For a session:

```text
one active writer at a time
```

Use per-conversation lock.

Prevent:

```text
two requests simultaneously mutating same conversation
```

PASS if concurrency test cannot interleave tool results/messages incorrectly.

---

# 15. Phase 10 — Dynamic Model Registry

Required API:

```text
GET /v1/models
```

Model registry obtains currently available models from ChatGPT Web behavior.

Do not hardcode model list.

Canonical model object:

```python
@dataclass
class GatewayModel:
    id: str
    display_name: str
    backend_identity: str | None
    available: bool
```

PASS if:

```text
gateway can select at least the default current model
invalid/unavailable model returns deterministic error
model list survives ChatGPT UI label changes when discovery still works
```

---

# 16. Phase 11 — Streaming Adapter

Input:

```json
{
  "stream": true
}
```

Output must be OpenAI-style incremental stream.

Internal:

```text
ChatGPT Web delta
       ↓
canonical ResponseDelta
       ↓
OpenAI SSE adapter
```

Required properties:

```text
ordered deltas
no duplicate chunks
final finish_reason
single terminal completion
```

PASS if:

```text
concatenated streamed assistant content
==
non-streaming final content semantically
```

---

# 17. Phase 12 — Error Mapping

Backend errors must map into predictable HTTP/API errors.

At minimum:

```text
AuthRequired
ModelUnavailable
ConversationNotFound
GenerationTimeout
GenerationInterrupted
ProtocolChanged
UIChanged
RateLimited
BrowserDisconnected
MalformedToolCall
```

No raw internal stack traces to normal clients.

---

# 18. Phase 13 — Health Endpoint

`GET /health` should report:

```json
{
  "ok": true,
  "browser": "ready",
  "authenticated": true,
  "backend": "ready"
}
```

Do not expose credentials.

PASS if health distinguishes:

```text
service alive
browser down
login required
backend temporarily unavailable
```

---

# 19. Phase 14 — OpenAI Client Compatibility Test

Use an OpenAI-compatible client configured only with:

```python
base_url="http://127.0.0.1:8765/v1"
api_key="local"
```

Required tests:

```text
list models
one-turn chat
multi-turn chat
streaming chat
tool definition input
tool_call output
tool result continuation
final assistant output
```

No gateway-specific parser should be required by the test client.

---

# 20. Phase 15 — External Agent Harness

Build a very small reference agent loop.

Pseudo-flow:

```python
messages = [...]

while True:
    response = gateway.chat(..., tools=tools)

    if response.tool_calls:
        for call in response.tool_calls:
            result = execute_tool(call)
            messages += assistant_tool_call
            messages += tool_result
        continue

    return response.content
```

This proves that the gateway can power a normal tool-using agent.

---

# 21. Optional Hosted Agent Mode

After Model Gateway passes, add:

```text
POST /agent/run
```

or CLI:

```text
webgpt-agent run ...
```

Hosted runtime owns:

```text
tool broker
agent loop
verification
```

This is not required for the core gateway PASS.

---

# 22. Local Tool Broker for Hosted Agent

Minimal tools:

```text
fs.list
fs.read
fs.search
fs.write
fs.replace
shell.run
test.run
```

Each call has:

```text
call_id
workspace
timeout
bounded output
```

The worker must never claim tool success without actual broker output.

---

# 23. Agent Acceptance Test A — Read-only Task

Fixture:

```text
a.txt
b.txt
c.txt
```

One contains:

```text
WEBGPT_NEEDLE_91A7
```

Task:

```text
Find the file and exact line containing WEBGPT_NEEDLE_91A7.
```

Expected agent loop:

```text
model
→ tool_call search
→ tool result
→ optional read
→ final answer
```

PASS if answer matches oracle.

---

# 24. Agent Acceptance Test B — Command Task

Fixture has a tiny program.

Task:

```text
Determine exact program output and verify it by running the program.
```

PASS requires:

```text
actual shell execution occurred
final output matches command output
```

FAIL if agent guesses without executing.

---

# 25. Agent Acceptance Test C — File Mutation

Task:

```text
Create result.txt containing exactly WEBGPT_AGENT_OK
and verify it.
```

PASS requires independent filesystem oracle.

---

# 26. Agent Acceptance Test D — Coding Bug Fix

Fixture:

```python
def add(a, b):
    return a - b
```

Test expects addition.

Task:

```text
Fix the project so all tests pass.
```

Expected loop:

```text
inspect
→ run tests
→ inspect failure
→ patch
→ rerun tests
→ final
```

PASS only if independent verifier reruns tests successfully.

---

# 27. Agent Acceptance Test E — Failure Recovery

Fixture causes first attempted command/path to fail.

Expected:

```text
tool error
→ worker observes error
→ changes approach
→ succeeds
```

FAIL if worker repeats the same failed request until limit or falsely reports success.

---

# 28. Agent Acceptance Test F — First Fix Is Insufficient

Design fixture so obvious first patch still leaves one failing test.

Expected:

```text
first patch
→ test fails
→ worker observes
→ second diagnosis
→ second patch
→ test passes
```

This proves the environment feedback loop actually works.

---

# 29. Agent Acceptance Test G — Long Horizon

Task must require at least:

```text
10 tool cycles
```

Example:

```text
inspect project
run tests
trace failure
read multiple files
patch
run targeted test
discover second issue
patch
run full suite
inspect diff
finalize
```

PASS if:

```text
conversation context is preserved
tool results remain aligned
no duplicate execution
correct final result
```

---

# 30. Idempotency

Every tool call should have:

```text
tool_call_id
```

For hosted-agent mode, broker caches:

```text
(run_id, tool_call_id) → result
```

If identical call ID is repeated:

```text
return cached result
do not execute twice
```

Required especially for:

```text
append
write
commands with side effects
```

---

# 31. Idempotency Acceptance Test

Tool call:

```text
append "X\n" to file
call_id = ABC
```

Submit same call twice.

PASS:

```text
file contains X exactly once
```

---

# 32. False Success Protection

Never trust:

```text
assistant:
"All tests pass."
```

Independent verifier reruns required checks.

For coding tasks:

```text
pytest
build
hidden tests
file checks
git diff
```

Agent output is not the oracle.

---

# 33. Hidden Verification

Fixtures should separate:

```text
workspace/
oracle/
```

Agent sees workspace.

Verifier owns hidden tests/oracle.

This prevents accidental tailoring only to visible test output.

---

# 34. Session Recovery Tests

Required:

## SR1 — page reload

During a run:

```text
tool result delivered
→ reload ChatGPT page
→ resume same conversation
```

PASS if run continues correctly.

## SR2 — page close/reopen

Close tab after persisted turn.

Reopen by conversation identity.

PASS if context remains.

## SR3 — generation interruption

Interrupt one generation.

PASS if partial malformed tool call is never executed.

---

# 35. Concurrency Tests

Do not start with concurrency.

First single-session PASS.

Then:

```text
2 independent sessions
4 independent sessions
```

Each gets unique task marker.

PASS if:

```text
no message cross-routing
no tool-result cross-routing
no conversation contamination
no session ID collision
```

---

# 36. Same-Conversation Concurrency

Two simultaneous writes to the same conversation must not execute concurrently.

Expected:

```text
request A acquires lock
request B waits
A completes
B executes
```

PASS if order is deterministic and no conversation corruption occurs.

---

# 37. Protocol Change Fallback

If ProtocolDriver fingerprint mismatches:

```text
do not send blind protocol request
```

Fallback:

```text
UIDriver
```

PASS if simulated protocol fixture mutation causes:

```text
ProtocolChanged
→ UI fallback
→ chat still succeeds
```

---

# 38. UI Change Failure Behavior

If UI selectors/fingerprints break:

```text
UI_CHANGED
```

Do not random-click.

PASS if test fixture mutation causes deterministic failure rather than unintended action.

---

# 39. Observability

Every request should have:

```text
request_id
gateway_session_id
backend_conversation_id (internal only)
duration
model
stream flag
tool_count
error_code
```

Hosted agent run additionally:

```text
run_id
step_count
tool_call_count
verification result
```

Do not log auth secrets.

---

# 40. Performance Baseline

Performance is secondary to correctness.

Still record:

```text
time to first token/event
total response time
tool round-trip time
session creation time
browser memory
```

No hard performance PASS threshold initially except:

```text
no indefinite hang
timeouts work
resource use does not grow unbounded across repeated runs
```

---

# 41. Soak Test

Run at least:

```text
50 sequential simple requests
```

across one or more sessions.

Measure:

```text
success
timeouts
browser crash
stale state
wrong conversation
duplicate responses
```

PASS target:

```text
>= 98% successful simple requests
0 wrong-conversation responses
0 duplicate side-effect tool execution
```

---

# 42. Repeated Agent Benchmark

For representative agent tasks:

```text
5 independent runs per task
```

Track:

```text
success rate
tool-call format errors
average steps
recovery rate
false-success rate
```

The path need not be identical.

Outcome must be correct.

---

# 43. Security / Local Boundary

Although this is personal/local use, hosted-agent execution still needs operational bounds.

Default:

```text
localhost only
workspace-scoped tools
command timeout
bounded stdout/stderr
max tool steps
max run time
```

This mainly protects against accidental loops and machine lockups.

---

# 44. Required Quality Metrics

Track:

```text
chat_success_rate
tool_call_parse_rate
tool_result_correlation_rate
multi_step_completion_rate
false_success_rate
session_recovery_rate
wrong_conversation_rate
duplicate_tool_execution_rate
```

---

# 45. PASS Gate — Level 1: Chat Backend

PASS only if:

```text
[ ] /health works
[ ] /v1/models works
[ ] one-turn chat works
[ ] 10-turn chat preserves context
[ ] reload preserves context
[ ] model selection works dynamically
[ ] non-stream response works
[ ] stream response works
[ ] 50-request soak >= 98% success
[ ] wrong-conversation rate = 0
```

If any mandatory item fails, the project is not ready for agent use.

---

# 46. PASS Gate — Level 2: Tool Calling

PASS only if:

```text
[ ] OpenAI-style tools accepted
[ ] ChatGPT Web can choose a tool
[ ] gateway emits valid assistant.tool_calls
[ ] finish_reason=tool_calls
[ ] arguments parse correctly
[ ] caller can return role=tool
[ ] tool_call_id correlates correctly
[ ] worker continues after tool result
[ ] 10 sequential tool cycles work
[ ] malformed tool output fails closed
[ ] no ordinary prose is executed as tool call
```

---

# 47. PASS Gate — Level 3: External Agent Compatibility

PASS only if a standard OpenAI-style agent loop can be configured using only:

```text
base_url
api_key placeholder
model
tools
```

and then successfully perform:

```text
read-only search task
command-execution task
file-write task
bug-fix task
failure-recovery task
```

without gateway-specific intervention in the loop.

This is the central acceptance requirement.

---

# 48. PASS Gate — Level 4: Real Agent Behavior

Required benchmark targets:

```text
Read-only tasks:
>= 95% success

Basic file mutation tasks:
>= 95% success

Deterministic coding tasks:
>= 85% success

Failure recovery tasks:
>= 80% success

False-success rate:
<= 5%

Wrong-conversation responses:
0

Cross-session tool result routing:
0 errors

Duplicate side-effect execution:
0
```

These are initial engineering targets and may be tightened later.

---

# 49. PASS Gate — Level 5: Hosted Agent Mode

Only applies if hosted agent runtime is included.

PASS only if the tool autonomously completes:

```text
receive task
→ inspect workspace
→ run command/tests
→ modify files
→ rerun verification
→ recover from at least one failed attempt
→ produce final answer
→ independent verifier confirms success
```

At least one acceptance fixture must require 10+ tool steps.

---

# 50. Absolute FAIL Conditions

The tool does NOT satisfy the project requirement if any of the following is true:

```text
It can only chat but cannot emit usable tool_calls.

Tool results cannot be fed back into the same reasoning session.

It loses context after tool execution.

It requires manual copy/paste between ChatGPT Web and terminal.

It requires an OpenAI API key.

It requires another LLM backend.

It hardcodes one current ChatGPT model and breaks model discovery.

It cannot survive a normal page reload.

It executes arbitrary prose as commands.

It can execute the same mutation twice due to retry.

It claims task success while independent verification consistently fails.

Multiple sessions mix messages/tool results.

A 10-step agent loop cannot complete reliably.
```

---


# 50A. PASS Gate — Reference Compatibility Review

Before the project can receive final PASS:

```text
[ ] ds2api has been used as a concrete API compatibility reference
[ ] the OpenAI-compatible client configuration has been compared with ds2api patterns
[ ] tool-calling request/response shapes have been compared
[ ] streaming behavior has been compared
[ ] model-list/model-alias behavior has been compared
[ ] error response behavior has been compared
[ ] differences are documented rather than silently assumed
[ ] at least one external agent client configuration follows a standard pattern demonstrated by the reference study
```

This gate does not require WebGPT to behave identically to ds2api.

It requires the team to prove that the API surface was designed with an existing practical agent-compatible gateway as a reference rather than from memory alone.


# 51. Definition of "Meets User Requirement"

The project is considered to meet the intended requirement when this scenario works end-to-end:

```text
External Agent Framework
        │
        ▼
base_url=http://127.0.0.1:8765/v1
model=chatgpt-web/...
tools=[shell, filesystem, tests]
        │
        ▼
WebGPT Gateway
        │
        ▼
ChatGPT Web
        │
        ▼
assistant requests shell/test/read/write tool
        │
        ▼
agent framework executes tool
        │
        ▼
tool result sent back through gateway
        │
        ▼
same ChatGPT Web conversation continues
        │
       ...
        │
        ▼
assistant final answer
        │
        ▼
independent verifier confirms the task is actually complete
```

No manual terminal choreography.
No external model API.
No context reset between tool calls.

---

# 52. Final Demonstration Test

Use a disposable Python project containing:

```text
multiple source files
multiple tests
one seeded primary bug
one secondary regression exposed after first fix
```

User command to the reference agent:

```text
"Fix this project. Do not modify tests. Verify everything before finishing."
```

Expected autonomous sequence:

```text
1. inspect repository
2. run tests
3. read failing code
4. patch first bug
5. rerun tests
6. observe remaining failure
7. inspect second cause
8. patch second bug
9. run targeted tests
10. run full suite
11. inspect final diff
12. final response
13. independent verifier reruns hidden + visible tests
```

The project receives final PASS only when this demo succeeds repeatedly.

Recommended requirement:

```text
4/5 successful fresh-session runs
```

with:

```text
0 test-file tampering
0 wrong-workspace writes
0 duplicate side effects
<= 1 false-success across the 5 runs
```

---

# 53. Recommended Implementation Order

```text
1. inspect ds2api reference repository
2. write ../guides/DS2API_COMPAT_NOTES.md
3. freeze API compatibility contract
4. API schemas
5. persistent browser backend
6. ChatGPTWebSession
4. one-turn text completion
5. multi-turn mapping
6. dynamic models
7. non-stream /v1/chat/completions
8. streaming adapter
9. tool schema renderer
10. tool-call parser
11. OpenAI tool_calls mapper
12. role=tool result mapper
13. repeated tool-cycle support
14. session/conversation locking
15. error mapping
16. OpenAI client compatibility tests
17. reference external agent loop
18. read-only acceptance task
19. command task
20. mutation task
21. coding task
22. recovery task
23. long-horizon task
24. session recovery
25. 50-request soak
26. 2-session isolation
27. 4-session isolation
28. final seeded-project demonstration
29. optional hosted agent runtime
```

---


> Implementation-order numbering after step 6 is sequential in execution even if earlier draft numbering remains in referenced notes. The mandatory ds2api study occurs before API schema lock-in.

# 54. Final Deliverables

A successful project should contain:

```text
webgpt-gateway server
persistent ChatGPT Web browser backend
OpenAI-compatible /v1/models
OpenAI-compatible /v1/chat/completions
stream=True support
tools/function calling
role=tool continuation
conversation/session persistence
reference OpenAI-style agent client
acceptance benchmark suite
independent verifier
test report
```

Optional:

```text
/v1/responses
hosted agent mode
parallel sessions
multi-agent orchestration
```

---

# 55. Final Principle

The gateway is not complete because:

```text
"ChatGPT Web replied through localhost."
```

It is complete when:

```text
a normal agent can treat the gateway as its model,
ask it to use tools,
execute those tools,
feed the results back,
continue reasoning across many steps,
and finish real tasks correctly.
```

That is the final PASS condition.

---

# 56. Gateway Verification & Deliverables Summary

The entire WebGPT Gateway has been built, hardened, and verified with **37 automated unit and acceptance tests**:

1. **OpenAI Endpoint Compatibility:**
   - `GET /v1/models`
   - `POST /v1/chat/completions` (Non-streaming JSON response)
   - `POST /v1/chat/completions` (`stream=True` with SSE chunks `data: {"choices": [{"delta": ...}]}`)
2. **Tool-Calling Transpiler & Multi-Turn Loop:**
   - Explicit `<WEBGPT_TOOL_CALL>` sentinel format avoiding hallucination.
   - 10-step multi-turn agent tool execution loop verified with standard `openai.OpenAI` SDK (`test_gateway_agent_loop.py`).
   - Rejection of mismatched or forged `tool_call_id`s.
3. **Conversation State Management:**
   - In-memory & disk-backed conversation store with message history validation.
   - Prevention of divergent history forks.
4. **Resilience & Fallback:**
   - UI Driver fallback with humanized keystrokes and DOM state stabilization.
   - Stream parsing with UTF-8 byte boundary preservation.

---

# 57. Zero-Interaction Automated Login (`username|password|2fa`)

The gateway supports fully automated, zero-interaction authentication via `AutoLoginManager`:
- Automatically fills username/email on Auth0.
- Automatically enters password.
- Computes TOTP 2FA code dynamically using RFC 6238 (`pyotp`) if secret key is provided, or enters static code.
- Verifies session establishment and stores browser context to persistent profile (`0700` permissions).
- Can be invoked via CLI: `python -m gpt.debug login -u <user> -p <pass> -2fa <secret>` or `--cred 'user|pass|2fa'` or stdin.
