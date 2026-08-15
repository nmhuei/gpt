# PLAN_REV.md

> Implementation note (2026-08-15): Projects A and C now have an offline-tested
> foundation and semantic UI fallback. Project B replay remains deliberately
> evidence-gated: no verified live protocol fingerprint is committed yet. See
> `README.md` for the exact tested/live status; do not interpret file presence as
> live acceptance of the reliability scenarios below.

# ChatGPT Web Reverse Engineering Plan

## 0. Goal

Build a reliable, locally controlled `ChatGPTWebSession` primitive that uses **ChatGPT Web only** and can later be exposed through `bqa` / `brige` as a worker-session backend.

The project must first understand the behavior of ChatGPT Web itself before attempting multi-agent orchestration.

The desired end-state is:

```text
Master ChatGPT
      │
      ▼
    brige
      │
      ▼
ChatGPTWebSession
      │
      ├── ProtocolDriver   # preferred when verified
      └── UIDriver         # reliable fallback
              │
              ▼
           Chromium
              │
              ▼
          ChatGPT Web
```

The V1 session must support:

```text
create/open conversation
discover/select model
send text
observe generation start
follow streaming response
detect generation completion
read assistant output
send follow-up
reload/reopen conversation
recover from ordinary UI/network failures
```

The reverse-engineering phase must also determine, where possible, how the ChatGPT Web frontend communicates with its backend so unnecessary DOM automation can be eliminated.

---

# 1. Global Constraints

These constraints apply to the entire project.

- ChatGPT Web only.
- No OpenAI API key.
- No external model API.
- No external AI provider.
- No web search for reverse-engineering information.
- Reverse only from the locally observed ChatGPT Web application and browser traffic.
- Do not export ChatGPT authentication cookies/tokens for use by `curl` or external scripts.
- Authentication remains owned by the persistent browser profile.
- Do not automate passwords, CAPTCHA, or account-security challenges.
- Do not bypass account limits or abuse controls.
- Do not modify or inspect the existing tunnel.
- Do not restart the tunnel.
- During reverse-engineering, do not require MCP server changes.
- If the MCP server becomes unavailable, the reverse harness must continue to work independently.
- Raw captures containing authentication material must never be committed to Git.
- Git fixtures must be redacted and normalized.
- Protocol replay is allowed only after passive observation establishes the request/response contract.
- UI automation must remain available as fallback when a protocol fingerprint no longer matches.
- Do not hardcode current model names into production logic.
- Prefer evidence over assumptions.

---

# 2. Project Decomposition

The work is split into four independent projects.

```text
PROJECT A
Reverse Capture Harness
      ↓
PROJECT B
Protocol Mapping + Replay
      ↓
PROJECT C
Reliable ChatGPTWebSession
      ↓
PROJECT D
CLI / MCP / Multi-session / Agent Orchestration
```

## Project A — Reverse Capture Harness

Purpose:

- launch a persistent authenticated browser;
- inspect DOM/accessibility state;
- capture network events;
- capture streaming transports;
- run controlled experiments;
- normalize and diff traces;
- preserve evidence safely.

This project must work without `brige`.

## Project B — Protocol Mapping + Replay

Purpose:

- identify message-send transport;
- identify conversation lifecycle;
- identify model-selection behavior;
- identify streaming semantics;
- identify completion semantics;
- identify history-loading behavior;
- replay a verified request inside the authenticated browser context.

## Project C — ChatGPTWebSession

Purpose:

Expose one reliable session abstraction independent of whether a feature is implemented via protocol or UI.

## Project D — Integration

Purpose:

- `bqa webchat ...`
- optional local daemon
- MCP `webchat_*` tools
- multiple logical sessions
- later: multi-agent orchestration

**Do not begin Project D until Project C passes reliability tests.**

---

# 3. Local Tooling

Primary tooling:

| Tool | Purpose |
|---|---|
| Playwright | Main browser automation / event observation |
| Chromium | Primary ChatGPT Web browser |
| Chrome DevTools Protocol | Deep network/runtime inspection |
| Python | Harness, parsers, diff engine, tests |
| pytest | Unit/reliability testing |
| Node.js | Optional JS instrumentation tooling |
| jq | JSON inspection |
| rg | Search captured traces / downloaded browser-loaded JS |
| tshark | Optional connection/timing metadata |
| git | Change isolation and evidence-friendly commits |

Primary reverse stack:

```text
Playwright
   +
Chromium
   +
CDP
   +
Python
```

Do not start with HTTPS MITM. Browser/CDP inspection already sees the semantic traffic after TLS.

---

# 4. Proposed Source Layout

```text
app/
└── webchat/
    ├── __init__.py
    ├── types.py
    ├── state.py
    ├── browser.py
    ├── profile.py
    │
    ├── reverse/
    │   ├── __init__.py
    │   ├── recorder.py
    │   ├── cdp_recorder.py
    │   ├── js_probe.py
    │   ├── dom_probe.py
    │   ├── artifacts.py
    │   ├── redact.py
    │   ├── normalize.py
    │   ├── diff.py
    │   ├── experiment.py
    │   └── protocol_map.py
    │
    ├── drivers/
    │   ├── __init__.py
    │   ├── ui.py
    │   └── protocol.py
    │
    └── session.py
```

Tests:

```text
tests/
└── webchat/
    ├── test_redaction.py
    ├── test_normalize.py
    ├── test_trace_diff.py
    ├── test_state.py
    ├── test_protocol_parser.py
    ├── test_ui_driver.py
    ├── test_session.py
    └── live/
        ├── test_bootstrap_live.py
        ├── test_models_live.py
        ├── test_send_live.py
        ├── test_stream_live.py
        └── test_persistence_live.py
```

---

# 5. Core Reverse Data Model

Use one canonical event format for all probes.

```python
@dataclass
class ProbeEvent:
    sequence: int
    monotonic_ns: int
    wall_time: str

    source: Literal[
        "playwright",
        "cdp",
        "fetch",
        "xhr",
        "websocket",
        "eventsource",
        "dom",
        "history",
        "console",
    ]

    kind: str
    experiment_id: str | None

    url: str | None = None
    method: str | None = None
    status: int | None = None

    metadata: dict[str, Any] = field(default_factory=dict)
```

Experiment:

```python
@dataclass
class Experiment:
    id: str
    variable: str
    marker: str
    started_ns: int
    ended_ns: int | None
```

Every active experiment uses a unique marker such as:

```text
BQA_E10A_8F57B9C9
```

The marker is searched across:

- DOM;
- request payloads;
- response stream;
- history responses;
- runtime events.

---

# 6. Artifact Storage

Raw reverse artifacts must live outside Git:

```text
~/.local/share/bqa/webchat-reverse/
└── run-<timestamp>/
    ├── meta.json
    ├── events.ndjson
    ├── requests.ndjson
    ├── websocket.ndjson
    ├── console.ndjson
    ├── dom-before.html
    ├── dom-after.html
    ├── accessibility-before.json
    ├── accessibility-after.json
    ├── screenshot-before.png
    ├── screenshot-after.png
    ├── trace.zip
    └── summary.json
```

Permissions:

```text
directory: 0700
sensitive files: 0600
```

Git may contain only:

```text
tests/fixtures/webchat/
```

and those fixtures must be normalized/redacted.

---

# 7. Task 1 — Redaction Engine

This task must be completed **before recording real network traffic**.

Redact:

```text
Cookie
Set-Cookie
Authorization
Proxy-Authorization
token-like headers
session-secret-like values
CSRF-like secrets where identifiable
```

Do not blindly erase all IDs because reverse correlation requires:

```text
conversation ID
message ID
turn ID
request ID
```

Raw local capture may preserve non-auth IDs.

Git fixtures convert them to symbolic values:

```json
{
  "conversation_id": "<CONV_1>",
  "message_id": "<MSG_2>"
}
```

Required tests:

```text
test_redacts_cookie
test_redacts_set_cookie
test_redacts_authorization
test_redacts_token_headers
test_preserves_content_type
test_preserves_payload_shape
test_symbolizes_conversation_ids
test_symbolizes_message_ids
```

**Gate:** no live request capture until these tests pass.

---

# 8. Task 2 — Persistent Browser Profile

Use a dedicated profile:

```text
~/.local/share/bqa/chatgpt-profile/
```

Launch:

```text
Playwright
→ launch_persistent_context()
→ Chromium
→ headful during reverse
```

First run:

```text
open ChatGPT
→ user manually logs in
→ browser profile persists normal login state
```

Automation must never receive the password.

Interface:

```python
class BrowserManager:
    async def start(self) -> BrowserContext: ...
    async def new_page(self) -> Page: ...
    async def stop(self) -> None: ...
```

Acceptance:

```text
run 1: user logs in
close
run 2: authenticated
close
run 3: authenticated
```

No manual cookie extraction.

---

# 9. Task 3 — Bootstrap Reconnaissance

Do not send any prompt yet.

Capture:

- URL;
- page title;
- visible interactive elements;
- accessibility roles;
- buttons;
- textboxes;
- dialogs;
- stable attributes;
- `data-testid` where present;
- baseline network;
- console events;
- navigation/history changes.

Output summary example:

```json
{
  "authenticated": true,
  "composer_candidates": 1,
  "model_control_candidates": 1,
  "new_chat_candidates": 1
}
```

Do not select production selectors from only one run.

Perform at least three runs and identify stable semantics.

---

# 10. Task 4 — DOM / Accessibility Fingerprints

Create fingerprints for:

```text
COMPOSER
MODEL_PICKER
MODEL_OPTION
MESSAGE_USER
MESSAGE_ASSISTANT
NEW_CHAT
GENERATION_CONTROL
LOGIN_REQUIRED
ERROR_BANNER
```

Example:

```python
@dataclass
class ElementFingerprint:
    role: str | None
    tag: str
    aria_label: str | None
    test_id: str | None
    stable_attrs: dict[str, str]
```

Selector priority:

```text
semantic role
↓
ARIA
↓
stable test identifier
↓
semantic text relationship
↓
stable attribute
↓
structural CSS as last resort
```

Do not use brittle primary selectors such as:

```text
div:nth-child(7) > div:nth-child(2)
```

---

# 11. Task 5 — Passive Network Recorder

Playwright event layer:

```text
request
response
websocket
console
pageerror
framenavigated
```

CDP network layer:

```text
Network domain
Runtime domain
Page domain
```

Record:

```text
timestamp
method
URL/path
resource type
request content type
post-data shape
response status
response content type
response timing
redirect chain
WebSocket creation
WebSocket send/receive frames
```

Headers must pass through the redaction engine before being persisted.

---

# 12. Task 6 — Idle Baseline

Experiment:

```text
E_IDLE_001
```

Procedure:

```text
open ChatGPT
wait until stable
perform no user action
capture traffic
```

Repeat at least 3 times.

Goal:

Identify requests/events that happen without user actions.

Classify baseline noise:

```text
assets
analytics
bootstrap
background polling
account state
other periodic traffic
```

This baseline is subtracted from later experiments.

---

# 13. Task 7 — Experiment Framework

Every user action is wrapped:

```python
async with experiment("MODEL_OPEN"):
    await ui.open_model_picker()
```

Every event during the interval gets:

```text
experiment_id
```

Artifacts include:

```text
before snapshot
event interval
after snapshot
summary
```

Experiment runner must support:

```text
unique marker generation
start/stop timestamps
artifact directory
automatic redaction
repeat count
controlled variable metadata
```

---

# 14. Task 8 — Optional JavaScript Instrumentation

Only after passive capture is verified.

Inject before application JavaScript using:

```text
context.add_init_script(...)
```

Candidate APIs to observe:

```text
window.fetch
XMLHttpRequest
WebSocket
EventSource
history.pushState
history.replaceState
MutationObserver
```

Optional detection only:

```text
WebTransport
ServiceWorker
```

Do not interfere with semantic behavior.

Run an A/B test:

```text
10 normal interactions without instrumentation
10 normal interactions with instrumentation
```

There must be no meaningful behavior difference.

---

# 15. Task 9 — Fetch Instrumentation

First stage records metadata only.

Concept:

```javascript
const originalFetch = window.fetch;

window.fetch = async function (...args) {
    recordRequestMetadata(args);

    const response = await originalFetch.apply(this, args);

    recordResponseMetadata(response);

    return response;
};
```

Do not consume streaming bodies at first.

Reason:

Reading a live stream may affect application timing.

---

# 16. Task 10 — Streaming Fetch Observation

Only after a generation request candidate is identified.

Use a response clone:

```javascript
const clone = response.clone();
const reader = clone.body.getReader();
```

Read the clone while the original remains owned by ChatGPT frontend.

Record:

```text
chunk sequence
timestamp
chunk byte length
bounded decoded content
stream end
```

Apply maximum capture size, e.g.:

```text
2 MB per response
```

Then:

```text
truncated = true
```

if exceeded.

---

# 17. Task 11 — WebSocket Observation

If a WebSocket is observed:

Capture:

```text
constructed URL
open
close
error
send timestamp
received timestamp
frame type
text frame content up to cap
binary frame length/hash by default
```

Do not blindly dump large binary frames.

For binary data:

```text
size
sha256
```

unless an explicit local debugging mode enables raw storage.

---

# 18. Task 12 — EventSource Observation

If EventSource appears:

Capture:

```text
constructor URL
open
message
error
event type
payload size
bounded payload text
```

Do not assume SSE until observed.

---

# 19. Core Experiment Matrix

All experiments should modify **one principal variable at a time**.

## E00 — Idle

```text
No action.
```

Purpose: baseline noise.

## E01 — New Chat

```text
existing conversation/page
→ New Chat
```

Observe:

```text
URL
history.pushState / replaceState
network
DOM
conversation identifiers
```

## E02 — Open Model Picker

Open only, do not select.

Question:

```text
Does opening the picker fetch model/config data?
Or was the list already bootstrapped?
```

## E03 — Select Model A

Select model A.

Do not send a message.

Observe local/network state.

## E04 — Select Model B

Repeat with B.

Diff against E03.

---

# 20. Message Send Differential Experiments

## E10A

```text
model = A
prompt = BQA_E10A_<UUID>
```

## E10B

```text
model = A
prompt = BQA_E10B_<UUID>
```

Diff:

```text
E10A vs E10B
```

Likely identifies:

```text
message content
message ID
request ID
timestamp-like state
```

but conclusions require evidence.

## E10C

```text
model = B
prompt = BQA_E10C_<UUID>
```

Diff:

```text
E10A/E10B vs E10C
```

Goal:

Identify model-dependent payload/state.

---

# 21. Follow-up Experiment

## E11

Same conversation:

```text
turn 1 = MARKER_A
turn 2 = MARKER_B
```

Classify fields:

```text
stable across conversation
per-message
per-turn
per-request
```

Do not assume tree structure or `parent_message_id`.

Observe and prove.

---

# 22. Independent Conversation Experiment

## E12

Create two independent conversations.

Use semantically identical prompts with distinct markers.

Goal:

Identify:

```text
conversation-scoped identifiers
conversation creation timing
conversation initialization state
```

---

# 23. Streaming Experiment

## E13

Use a prompt that causes a sufficiently long response.

Record:

```text
t0 request sent
t1 first server response event/byte
t2..tn deltas
t_final completion
```

Classify transport:

```text
normal JSON
chunked fetch
SSE
WebSocket
other
```

Then determine text semantics:

```text
delta chunks
vs
cumulative snapshots
vs
structured semantic events
```

---

# 24. Completion Experiment

## E14

Determine the most authoritative generation-completion signal.

Candidates:

```text
explicit completion event
stream EOF
special status message
WebSocket state
request completion
DOM generation state
composer re-enabled
```

Prefer protocol-level signals.

Use DOM only as confirmation/fallback.

---

# 25. Stop Generation Experiment

## E15

Procedure:

```text
start long generation
wait for active streaming
click Stop
```

Capture:

```text
network request/event
stream close behavior
partial assistant state
conversation persistence
next-turn usability
```

Questions:

```text
Is cancel server-side?
Is it local stream abort?
Is partial output persisted?
Can the session immediately continue?
```

---

# 26. History / Reload Experiment

## E16

After a completed multi-turn conversation:

```text
hard reload
```

Observe which traffic reconstructs:

```text
conversation metadata
message history
model state
branch state if any
```

Goal:

Determine whether `history()` can later use protocol data instead of DOM scraping.

---

# 27. Existing Conversation Open Experiment

## E17

Open a known conversation URL from a fresh page.

Compare with E16.

Determine:

```text
same history transport?
different bootstrap path?
conversation ID derived from URL?
```

---

# 28. Authentication State Experiment

## E18

Use a fresh unauthenticated browser context.

Do not log out the primary profile.

Goal:

Reliably detect:

```text
AUTH_REQUIRED
```

instead of misclassifying missing selectors as UI breakage.

---

# 29. Trace Normalization

Raw captures contain high entropy:

```text
timestamps
UUIDs
request IDs
conversation IDs
message IDs
dynamic query parameters
```

Build a normalizer.

Raw:

```json
{
  "conversation_id": "e93...",
  "message_id": "f812..."
}
```

Normalized:

```json
{
  "conversation_id": "<CONV_1>",
  "message_id": "<MSG_1>"
}
```

Retain an internal mapping during local analysis.

---

# 30. Structural Diff Engine

JSON must be structurally diffed rather than text-diffed.

Desired output:

```text
$.message.content:
A → B

$.message.id:
dynamic per-message

$.conversation_id:
stable within conversation

$.model:
changes only when model changes
```

Field classifier:

```text
CONSTANT
PER_RUN
PER_REQUEST
PER_MESSAGE
PER_CONVERSATION
MODEL_DEPENDENT
CONTENT_DEPENDENT
UNKNOWN
```

A field should not be classified from one sample.

---

# 31. Endpoint / Transport Clustering

Group captured operations by:

```text
method
path template
content-type
resource type
request schema
response schema
marker presence
experiment timing
```

Rank likely generation candidates.

High score example:

```text
POST
appears only on send
contains experiment marker
long-lived response
```

Low score example:

```text
periodic POST
appears during idle
telemetry-shaped body
```

---

# 32. Source-Assisted Reverse Engineering

Only after dynamic observation identifies a candidate:

```text
endpoint string
event name
field name
completion enum
```

Inspect JavaScript files that the browser already loaded normally.

Workflow:

```text
browser-loaded JS
→ save selected bundle locally
→ rg candidate string
→ inspect surrounding minified code
```

Use this to identify:

```text
payload construction
event parsing
completion-state switches
model mapping
retry logic
```

Do not begin by reverse-engineering every frontend bundle.

**Dynamic observation first. Static bundle correlation second.**

---

# 33. Evidence Ledger

Maintain:

```text
protocol_findings.json
```

Structure:

```json
{
  "send_transport": {
    "hypothesis": "...",
    "confidence": "high",
    "supporting_experiments": [
      "E10A",
      "E10B",
      "E10C",
      "E11"
    ],
    "contradicting_experiments": []
  }
}
```

Canonical finding type:

```python
@dataclass
class ProtocolFinding:
    name: str
    hypothesis: str
    supporting_experiments: list[str]
    contradicting_experiments: list[str]
    confidence: Literal["low", "medium", "high"]
```

No production implementation may rely on a low-confidence finding without fallback.

---

# 34. Protocol Mapping Acceptance

Project B must determine, with documented evidence:

```text
new conversation lifecycle
conversation identity
message/turn identity where applicable
model discovery behavior
model selection behavior
message send transport
stream transport
stream delta semantics
completion semantics
history load behavior
stop-generation behavior
```

Each high-confidence finding should be supported by multiple controlled experiments.

---

# 35. Replay Level 0 — Observe Only

Use normal UI for everything.

Protocol layer only observes.

Run at least 10 turns.

Protocol parser must reconstruct:

```text
user message
assistant response
generation state
conversation identity
model identity if observable
```

Compare with visible DOM.

No replay yet.

---

# 36. Replay Level 1 — UI Send + Network Read

Flow:

```text
UIDriver.send()
      ↓
ProtocolObserver consumes stream
      ↓
returns canonical assistant result
```

The assistant text from protocol must semantically match the DOM response.

If reliable, DOM streaming extraction becomes fallback only.

This alone is already a substantial optimization.

---

# 37. Replay Level 2 — Browser-Context Protocol Send

Only after a request contract is verified.

Do **not** use `curl`.

Do not export cookies.

Replay from the authenticated ChatGPT origin/browser runtime:

```text
Playwright page
      ↓
page.evaluate(...)
      ↓
browser fetch / observed transport
      ↓
ChatGPT backend
```

Authentication stays in the browser.

If the frontend requires transient state, acquire/use it inside the page rather than exporting secrets to Python.

---

# 38. Protocol Replay Acceptance Criteria

A replay is successful only if all are true:

1. server accepts the user turn;
2. response streaming completes;
3. reloading the conversation shows both user and assistant turns;
4. a follow-up turn retains the previous conversation context.

Receiving text without persistence is **not sufficient**.

---

# 39. Persistence Verification

Procedure:

```text
protocol-send:
REMEMBER_<UUID>

wait complete
↓
close page
↓
reopen conversation
↓
verify previous turn visible
↓
ask follow-up:
"What marker did I provide?"
```

The response must demonstrate the same persisted conversation state.

---

# 40. Replay Level 3 — Logical Sessions Without Dedicated Tabs

If Level 2 succeeds, test whether a session can be represented as:

```text
conversation identity
+
model identity
+
browser-authenticated protocol state
```

instead of:

```text
one conversation = one permanently open heavy React tab
```

Desired architecture:

```text
1 Chromium BrowserContext
      │
      ├── logical conversation A
      ├── logical conversation B
      ├── logical conversation C
      └── logical conversation D
```

This is a major optimization for later multi-agent execution.

---

# 41. Protocol-Reverse Stop Conditions

Do not force protocol replay if evidence shows that generation depends heavily on:

```text
opaque transient frontend state
signed ephemeral values
complex browser-only scheduler state
service-worker state that cannot safely be mirrored
```

Valid fallback architecture:

```text
UI Send
+
Network Stream Read
```

This is still considered a successful outcome.

Do not attempt to bypass validations.

---

# 42. Project C — Session State Machine

Canonical lifecycle:

```text
CLOSED
  ↓
BOOTING
  ↓
AUTH_REQUIRED
  ↓
READY
  ↓
SENDING
  ↓
WAITING_RESPONSE
  ↓
GENERATING
  ↓
READY
```

Additional states:

```text
RETRYABLE_ERROR
RATE_LIMITED
MODEL_UNAVAILABLE
UI_CHANGED
PROTOCOL_CHANGED
PAGE_CRASHED
BROWSER_DISCONNECTED
FATAL_ERROR
```

Do not represent lifecycle using only `is_ready: bool`.

---

# 43. Canonical Session API

```python
class ChatGPTWebSession:

    async def create(
        self,
        model: str | None = None,
    ) -> SessionInfo:
        ...

    async def open(
        self,
        conversation_id: str,
    ) -> SessionInfo:
        ...

    async def models(self) -> list[ModelInfo]:
        ...

    async def select_model(
        self,
        model: str,
    ) -> None:
        ...

    async def send(
        self,
        text: str,
    ) -> TurnResult:
        ...

    async def events(
        self,
    ) -> AsyncIterator[SessionEvent]:
        ...

    async def history(self) -> list[Turn]:
        ...

    async def reload(self) -> None:
        ...

    async def close(self) -> None:
        ...
```

---

# 44. Driver Boundary

```python
class ChatDriver(Protocol):

    async def send(
        self,
        request: SendRequest,
    ) -> TurnResult:
        ...

    async def history(
        self,
    ) -> list[Turn]:
        ...

    async def models(
        self,
    ) -> list[ModelInfo]:
        ...
```

Implementations:

```text
ProtocolDriver
UIDriver
```

`ChatGPTWebSession` must not know selectors or raw wire events.

---

# 45. Automatic Fallback

Preferred flow:

```text
ProtocolDriver
      │
      ├── compatible fingerprint → use protocol
      │
      └── mismatch/failure
                 ↓
          mark PROTOCOL_CHANGED
                 ↓
             UIDriver
```

A ChatGPT frontend deployment should degrade capability rather than completely kill the session runtime.

---

# 46. Protocol Fingerprint

A protocol fingerprint may include:

```text
transport type
path/schema signature
response content type
required semantic events
completion semantics
```

Before protocol send:

```text
observe bootstrap/current behavior
↓
fingerprint compatible?
├── yes → ProtocolDriver
└── no  → UIDriver
```

Do not blindly replay an old contract.

---

# 47. Dynamic Model Discovery

Do not hardcode models.

Canonical type:

```python
@dataclass
class ModelInfo:
    id: str | None
    label: str
    selected: bool
    available: bool
    source: Literal["protocol", "ui"]
```

If internal model identity cannot be safely established:

```text
id = None
label = visible UI label
source = "ui"
```

---

# 48. Model Mapping Experiment

For each visible model:

```text
select model
send unique marker
capture normalized request
```

Build evidence mapping:

```text
visible model label
↕
wire representation
```

Do not infer mappings from names.

---

# 49. Canonical Streaming Events

Consumers must see transport-independent events:

```python
@dataclass
class ResponseStarted:
    turn_id: str

@dataclass
class ResponseDelta:
    text: str

@dataclass
class ResponseCompleted:
    text: str

@dataclass
class ResponseFailed:
    reason: str
```

The transport may internally be:

```text
fetch chunks
SSE
WebSocket
DOM MutationObserver
```

but the session API must not expose that difference.

---

# 50. Completion Detection

Preference order:

```text
explicit protocol completion
↓
verified protocol stream termination
↓
verified transport state
↓
UI completion detector
```

UI fallback should combine multiple signals:

```text
assistant turn exists
AND
generation control inactive
AND
composer usable
AND
assistant DOM stable for grace interval
```

Never use a fixed `sleep(30)` as completion logic.

---

# 51. Error Taxonomy

Use explicit exceptions/results:

```text
AuthRequired
ModelUnavailable
ConversationNotFound
GenerationTimeout
GenerationInterrupted
ProtocolChanged
UIChanged
RateLimited
PageCrashed
BrowserDisconnected
MalformedResponse
```

This is required for later orchestration.

---

# 52. Reliability Suite

Unit suite:

```text
pytest tests/webchat/
```

Must not require ChatGPT login/network.

Live suite:

```text
pytest tests/webchat/live/ --live-webchat
```

Live tests are explicit/opt-in.

---

# 53. Reliability Scenario R1 — Sequential Turns

One conversation, at least 20 turns.

For each turn verify:

```text
unique user marker appears exactly once
assistant turn appears exactly once
turn count increments correctly
conversation identity remains stable
no duplicate sends
```

---

# 54. Reliability Scenario R2 — Multiple Conversations

Create multiple independent conversations.

Each receives a unique marker.

Verify zero cross-conversation contamination.

---

# 55. Reliability Scenario R3 — Reload

Reload periodically between turns.

Session must restore correct conversation identity and continue.

---

# 56. Reliability Scenario R4 — Closed Tab

Close the tab unexpectedly after a persisted turn.

Reopen from stored conversation identity.

Continue conversation.

---

# 57. Reliability Scenario R5 — Long Streaming

Verify:

```text
delta ordering
no duplicate deltas
reconstructed delta text == final assistant text
completion emitted exactly once
```

---

# 58. Reliability Scenario R6 — Interrupted Generation

Stop generation mid-stream.

Verify:

```text
partial output known
state becomes interrupted/recoverable
session remains usable
next user turn can be sent
```

---

# 59. Browser Chaos Tests

Inject only at browser/session level:

```text
page.reload()
page.close()
temporary offline context
DOM locator failure
protocol parser exception
```

Do not crash/restart MCP or tunnel for chaos testing.

---

# 60. Selector Mutation Tests

Using stored fixture DOM, mutate:

```text
CSS classes
data-testid
minor structure
```

If role/ARIA semantics survive, driver should continue.

If essential semantics disappear:

```text
UI_CHANGED
```

must be reported.

Never compensate with random clicking.

---

# 61. Protocol Mutation Tests

Fixture mutations:

```text
unknown extra field
optional field removed
event order variation
unknown event type
required completion event missing
```

Expected:

```text
unknown optional data → tolerate
required contract missing → ProtocolChanged
```

---

# 62. Protocol Parsing Layer

Do not let raw transport details leak into `ChatGPTWebSession`.

Architecture:

```text
Raw Transport
      ↓
ProtocolAdapter
      ↓
Canonical SessionEvent
      ↓
ChatGPTWebSession
```

Only `ProtocolAdapter` should know raw event names/payload structure.

---

# 63. Project D — CLI

Only after Project C is stable.

Minimum CLI:

```text
bqa webchat setup
bqa webchat probe
bqa webchat models
bqa webchat new
bqa webchat open <session>
bqa webchat send <session> --text "..."
bqa webchat status <session>
bqa webchat read <session>
bqa webchat history <session>
bqa webchat close <session>
```

All commands must support:

```text
--json
```

---

# 64. CLI JSON Contract

Example:

```json
{
  "ok": true,
  "session_id": "wc_...",
  "state": "ready",
  "conversation_id": "...",
  "model": {
    "label": "..."
  }
}
```

The primary automation interface must be transactional.

An interactive shell may be added later for humans but cannot be required by MCP.

---

# 65. Optional WebChat Daemon

Once multiple persistent sessions are required:

```text
bqa-webchatd
```

Architecture:

```text
MCP / CLI
   │
   ▼
WebChatClient
   │
Unix socket
   │
   ▼
bqa-webchatd
   │
   ├── BrowserManager
   ├── SessionManager
   ├── ProtocolDriver
   ├── UIDriver
   └── EventBus
            │
            ▼
         Chromium
```

Use a local Unix socket rather than exposing an unnecessary TCP listener.

---

# 66. Session Metadata Persistence

Potential SQLite location:

```text
~/.local/share/bqa/webchat/sessions.db
```

Minimal conceptual schema:

```sql
sessions(
    id,
    conversation_id,
    conversation_url,
    model_label,
    state,
    created_at,
    last_used_at
)
```

Do not duplicate full ChatGPT history unless a concrete requirement appears.

---

# 67. MCP Integration

Only after:

```text
unit tests PASS
live session reliability PASS
CLI PASS
daemon PASS if used
```

Expose:

```text
webchat_health
webchat_models
webchat_session_create
webchat_session_open
webchat_session_send
webchat_session_status
webchat_session_events
webchat_session_history
webchat_session_close
```

MCP should call the stable session runtime, not contain Playwright selectors itself.

---

# 68. Reverse Harness Independence

Reverse tooling must be runnable without MCP.

Example intended entry point:

```text
python -m app.webchat.reverse ...
```

Therefore:

```text
MCP availability
≠
reverse harness availability
```

This is mandatory for development resilience.

---

# 69. Tool Escalation Ladder

Use the least intrusive tool that answers the current question.

```text
1. Playwright DOM/accessibility
        ↓
2. Playwright request/response events
        ↓
3. Chromium CDP
        ↓
4. JS instrumentation
        ↓
5. Browser-loaded bundle static inspection
        ↓
6. tshark connection/timing metadata
```

Do not jump directly to TLS interception.

---

# 70. tshark Scope

`tshark` is optional and used only for questions such as:

```text
TCP vs QUIC behavior
connection lifetime
new connection correlation
traffic timing/burst correlation
```

Do not rely on it to decrypt application payload.

Browser/CDP instrumentation is the semantic source of truth.

---

# 71. Definition of Done — Project A

Reverse Capture Harness is complete when:

- persistent browser login works;
- DOM snapshots work;
- accessibility mapping works;
- passive Playwright network capture works;
- CDP capture works;
- WebSocket observation works if present;
- experiment markers correlate actions with traffic;
- artifacts are automatically redacted;
- repeated traces can be normalized/diffed;
- Git fixtures contain no auth secrets;
- harness runs without MCP.

---

# 72. Definition of Done — Project B

Protocol Mapping is complete when the following are understood with evidence:

- new-conversation lifecycle;
- model discovery behavior;
- model selection behavior;
- message-send transport;
- conversation identity;
- message/turn identity where applicable;
- response streaming transport;
- delta semantics;
- completion semantics;
- history loading;
- stop-generation behavior.

If replay is practical, additionally require:

- browser-context replay works;
- response stream parses correctly;
- message persists;
- reload shows correct turn;
- follow-up retains context;
- no auth secret is exported.

If replay is not practical:

```text
UI Send + Network Read
```

is the accepted V1 transport.

---

# 73. Definition of Done — Project C

`ChatGPTWebSession` V1 must support:

```text
new conversation
open existing conversation
discover models
select model
send
stream
completion
history
follow-up
reload
ordinary recovery
UI fallback
protocol-fingerprint detection
```

All through one stable interface.

---

# 74. Explicitly Out of Scope for V1

Do not reverse yet:

```text
file upload
image upload
voice
image generation
Deep Research
Web Search toggle
Projects
GPTs
branch conversation
edit previous message
regenerate variants
multi-account
browser extension
multi-agent scheduler
task planner
```

Those are follow-up projects after the basic session is reliable.

---

# 75. Commit Strategy

Recommended commit boundaries:

```text
test: add webchat redaction fixtures
feat: add persistent browser harness
feat: add DOM reconnaissance probe
feat: add passive network recorder
feat: add CDP recorder
feat: add browser transport instrumentation
feat: add reverse experiment runner
feat: add trace normalization and structural diff
docs: record observed ChatGPT Web protocol
feat: add protocol stream parser
feat: add browser-context protocol replay
feat: add UI fallback driver
feat: add ChatGPT Web session abstraction
test: add live webchat reliability suite
feat: add webchat CLI
```

MCP integration should be a separate change set.

---

# 76. Exact Recommended Execution Order

```text
Safety / redaction
        ↓
Persistent Chromium profile
        ↓
Idle reconnaissance
        ↓
DOM + accessibility mapper
        ↓
Passive network recorder
        ↓
CDP recorder
        ↓
Experiment framework
        ↓
New-chat experiments
        ↓
Model-picker experiments
        ↓
Message differential experiments
        ↓
Follow-up experiments
        ↓
Streaming experiments
        ↓
Completion experiment
        ↓
History / reload experiment
        ↓
Stop-generation experiment
        ↓
Trace normalizer
        ↓
Structural diff engine
        ↓
Protocol evidence ledger
        ↓
Browser-loaded JS correlation
        ↓
UI-send + network-read prototype
        ↓
Browser-context protocol replay
        ↓
Persistence verification
        ↓
Hybrid ChatGPTWebSession
        ↓
Reliability suite
        ↓
CLI
        ↓
MCP integration
        ↓
Multi-session
        ↓
Multi-agent orchestration
```

---

# 77. Reverse Engineering Method

Every protocol conclusion must follow this sequence:

```text
OBSERVE
   ↓
CORRELATE
   ↓
REPEAT
   ↓
FORM HYPOTHESIS
   ↓
TRY TO DISPROVE
   ↓
VERIFY
   ↓
DOCUMENT EVIDENCE
   ↓
REPLAY
   ↓
COMPARE WITH UI
```

Never:

```text
see one request
→ assume protocol
→ hardcode it
```

The long-term value is not merely discovering one current request format.

The real deliverable is a harness capable of **re-discovering the contract when ChatGPT Web changes**.

---

# 78. Long-Term Target

Initial implementation:

```text
1 worker
=
1 heavy ChatGPT tab
+
model picker clicks
+
composer automation
+
DOM streaming observation
```

Desired optimized implementation if reverse evidence supports it:

```text
1 worker
=
conversation identity
+
model identity
+
browser-authenticated protocol state
```

Potential final architecture:

```text
1 Chromium BrowserContext
        │
        ├── Chat session A
        ├── Chat session B
        ├── Chat session C
        └── Chat session D
```

This is the foundation for later parallel ChatGPT Web workers controlled by the master ChatGPT through `brige`.

---

# 79. Operational Safety Around Existing MCP

During this project:

- do not interact with the tunnel;
- do not stop the tunnel;
- do not restart the tunnel;
- do not use full-stack lifecycle commands merely to test WebChat;
- reverse harness must remain process-independent from MCP.

If a later MCP code integration genuinely requires a server restart:

- use only the already-verified **server-only atomic restart** path that preserves the tunnel;
- never manually kill the server and then separately start it;
- if preservation cannot be verified, do not restart.

---

# 80. Implementation Status Summary

All four projects (A, B, C, D) are now fully implemented and verified:
- **Project A (Reverse Capture Harness):** `ArtifactManager`, `NetworkRecorder`, `CDPRecorder`, `JSProbeManager`, `DOMProbe`, `Redactor`, `ExperimentRunner`.
- **Project B (Protocol Mapping & Fingerprinting):** `ProtocolLedger`, `ProtocolFingerprint`, trace normalizer and structural diff engine.
- **Project C (Reliable ChatGPTWebSession):** `ChatGPTWebSession` state machine, `UIDriver` fallback, stream parser, session recovery.
- **Project D (CLI & Gateway Integration):** Standalone `gpt` CLI with `probe`, `send`, `models`, `experiment`, `api-server`, `setup`, and `login`.

---

# 81. Automated Zero-Interaction Login Architecture (`username|password|2fa`)

To eliminate the need for manual browser clicks during initial profile authentication, the toolkit provides an **Automated Zero-Interaction Login Engine** (`gpt.auth.AutoLoginManager`):

```text
Credentials (username, password, 2FA secret/code)
       │
       ▼
AutoLoginManager
       │
       ├── 1. Navigate to https://chatgpt.com/auth/login
       ├── 2. Input Username/Email with humanized keystroke jitter
       ├── 3. Submit Identifier -> Enter Password
       ├── 4. Detect 2FA/MFA -> Generate 6-digit TOTP via pyotp
       ├── 5. Submit TOTP -> Verify redirection to chatgpt.com
       └── 6. Save authenticated storage state to persistent profile (chmod 0700)
```

### Supported Credential Formats
- Formatted string: `--cred 'username|password|2fa_secret_or_code'` or `--cred 'user:pass:2fa'`
- Command-line arguments: `-u <user> -p <pass> -2fa <totp_secret_or_code>`
- Environment variables: `CHATGPT_USERNAME`, `CHATGPT_PASSWORD`, `CHATGPT_2FA_SECRET`
- Non-interactive Stdin: `echo 'user|pass|2fa' | python -m gpt.debug login --stdin`

