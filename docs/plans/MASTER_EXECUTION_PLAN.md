# Master Execution Plan: WebGPT Fake API to Production-Grade Gateway for Claude Code & OpenCode

> **Nguồn sự thật duy nhất (Single Source of Truth)** cho quá trình hoàn thiện gateway.

---

# 0. Định nghĩa “hoàn thiện”

Không được coi tool hoàn thiện chỉ vì:

```text
curl /v1/chat/completions → 200
```

hay:

```text
Claude Code gọi được Read/Bash một lần
```

Tool chỉ được coi **DONE** khi toàn bộ chuỗi sau chạy từ đầu tới cuối:

```text
Claude Code CLI
    ↓
Anthropic-compatible API
    ↓
WebGPT gateway
    ↓
ChatGPT Web Free anonymous
    ↓
structured tool decision
    ↓
gateway parser/normalizer
    ↓
Claude Code executes tool locally
    ↓
tool_result
    ↓
gateway continuation
    ↓
ChatGPT Web
    ↓
...
    ↓
project hoàn chỉnh
    ↓
compile/tests/smoke/manual verification
```

và không xuất hiện:

```text
400 unexpected
409 conversation conflict
422 malformed tool call
429 do chính gateway spam request
500 crash
502 do gateway lifecycle
504 không kiểm soát
tool_call_id mismatch
invalid JSON/XML
lost indentation
false-completion
tool refusal
phantom tool execution
duplicate execution
missing tool result
conversation drift
session corruption
unbounded retry
process orphan
file viết sai workspace
```

---

# 1. Hard constraints

Các constraint này phải được enforce bằng code/harness, không chỉ ghi trong prompt.

## Account

Chỉ dùng:

```text
ChatGPT Web Free anonymous
```

Không:

```text
Plus
Pro
login account
fallback sang authenticated profile
```

Nếu session vô tình authenticated:

```text
RUN = INVALID
```

phải terminate run.

---

## Workspace

Tất cả artifact runtime phải nằm dưới:

```text
~/Downloads/webgpt/
```

Cấu trúc:

```text
~/Downloads/webgpt/
├── runs/
│   ├── claude/
│   ├── opencode/
│   └── smoke/
├── benchmarks/
│   └── pcap/
├── reverse/
├── captures/
├── failed-runs/
├── successful-runs/
└── tmp/
```

PCAP workspace:

```text
~/Downloads/webgpt/benchmarks/pcap/<run-id>/
```

Không tạo:

```text
~/webgpt-*
/tmp/webgpt-*          # ngoại trừ ephemeral OS state không tránh được
~/pcap-*
~/debug-*
```

Repo gateway vẫn ở:

```text
/home/light/GitHub/gpt
```

Nhưng runtime artifact **không được ghi vào repo** trừ regression fixtures/tests có chủ đích.

---

# 2. Golden baseline phải được tạo trước

Trước khi sửa thêm tính năng nào, phải xác định Free anonymous baseline.

Tạo script:

```text
scripts/verify-free-anonymous.sh
```

Nó phải verify:

### Browser launch

```text
browser starts
ChatGPT page loads
not authenticated
composer found
send button found
new conversation possible
```

### Direct web send

Prompt:

```text
Reply exactly: FREE_ANON_BASELINE_OK
```

Expect:

```text
FREE_ANON_BASELINE_OK
```

### Gateway non-stream

```http
POST /v1/chat/completions
```

Expect 200.

### Gateway stream

```http
POST /v1/chat/completions
stream=true
```

Expect:

```text
role chunk
text chunks
finish_reason
[DONE]
```

### Session continuation

Turn 1:

```text
Remember token ABC-719.
```

Turn 2:

```text
What token did I give you?
```

Expect:

```text
ABC-719
```

Nếu baseline fail:

```text
STOP ALL BENCHMARKS
```

Chỉ sửa baseline.

---

# 3. Reverse engineering ChatGPT Web trước khi tiếp tục patch mù

Không đoán UI behavior.

Tạo:

```text
~/Downloads/webgpt/reverse/
```

Mỗi feature phải có observation artifact.

## 3.1 Composer lifecycle

Quan sát:

```text
initial page
composer ready
typing
send enabled
submit
generating
streaming
generation complete
composer ready again
```

Ghi:

```json
{
  "state": "...",
  "selectors": [],
  "observable_text": "",
  "timestamp": "...",
  "conversation_id": null
}
```

---

## 3.2 New conversation

Xác định chính xác:

```text
khi nào cần click New Chat
khi nào fresh page đã là conversation mới
conversation ID xuất hiện lúc nào
reload giữ state gì
```

Không hard-code assumption.

---

## 3.3 Response completion detection

Phải xác định nhiều signal:

```text
stop button disappears
send button returns
DOM response stops changing
network idle
assistant block stable
```

Sau đó chọn ít nhất 2 signal kết hợp.

Không dùng:

```text
sleep(3)
```

làm source of truth.

---

# 4. Browser state machine

Browser driver cần state machine explicit.

Ví dụ:

```text
CLOSED
  ↓
OPENING
  ↓
PAGE_READY
  ↓
COMPOSER_READY
  ↓
SUBMITTING
  ↓
COMMIT_UNKNOWN
  ↓
GENERATING
  ↓
COMPLETED
  ↓
COMPOSER_READY
```

Error states:

```text
UI_CHANGED
AUTH_REQUIRED
RATE_LIMITED
GENERATION_TIMEOUT
NAVIGATION_FAILED
CONVERSATION_LOST
```

Mỗi transition phải trace.

Ví dụ:

```json
{
  "from": "SUBMITTING",
  "to": "GENERATING",
  "evidence": "stop_button_visible",
  "duration_ms": 417
}
```

---

# 5. Exactly-once semantics

Đây là phần bắt buộc nếu muốn giống API thật.

Problem:

```text
gateway gửi prompt
↓
browser click Send
↓
network/UI timeout
↓
gateway không biết prompt đã submit hay chưa
```

Không được resend ngay.

Phải:

```text
CommitUnknown
↓
reconcile current conversation
↓
search latest user turn
```

Nếu user turn tồn tại:

```text
DO NOT RESEND
```

Nếu không tồn tại:

```text
bounded retry = 1
```

Regression test:

```text
submit succeeded
driver raises timeout
retry request arrives
gateway reconciles
model only sees prompt once
```

---

# 6. Provider compatibility matrix

Gateway phải có một matrix rõ ràng.

## OpenAI Chat Completions

Support:

```text
POST /v1/chat/completions
GET /v1/models
stream
tools
tool_choice
tool messages
developer role
system role
stream_options.include_usage
max_tokens advisory
max_completion_tokens advisory
```

---

## OpenAI Responses

Support tối thiểu:

```text
POST /v1/responses
input
previous_response_id
stream
tool calls
tool outputs
```

---

## Anthropic

Claude Code cần:

```text
POST /v1/messages
system
messages
tools
tool_choice
stream
tool_use
tool_result
stop_reason
```

---

# 7. Client fixture capture

Không viết compatibility theo docs duy nhất.

Phải capture request **thật** từ:

```text
Claude Code
OpenCode
```

Mỗi client có fixture:

```text
tests/fixtures/clients/
├── claude-code/
│   ├── simple.json
│   ├── tools.json
│   ├── tool-result.json
│   └── stream.json
└── opencode/
    ├── title-request.json
    ├── coding-request.json
    ├── tools.json
    └── stream.json
```

Sau khi capture:

```text
client version
headers
body
endpoint
stream behavior
```

được frozen thành regression fixture.

---

# 8. Request normalization layer

Không để API endpoint tự hiểu business logic.

Flow:

```text
Anthropic request ─┐
OpenAI request ────┼─→ NormalizedRequest
Responses request ─┘
```

Normalized object:

```python
NormalizedRequest(
    messages,
    tools,
    tool_choice,
    stream,
    model,
    reasoning,
    metadata,
)
```

Sau đó mọi protocol dùng chung runtime.

---

# 9. Prompt translation layer

Đây là nơi hiện tại dễ lỗi nhất.

Không gửi raw Claude prompt + vài câu tool instruction.

Phải có:

```text
SYSTEM CONTRACT
CLIENT CONTEXT
TOOL CONTRACT
TOOL SCHEMA
CONVERSATION
CURRENT TASK
```

Thứ tự deterministic.

Hash prompt:

```text
sha256
characters
token estimate
tool count
message count
```

---

# 10. Exact pre-GPT prompt capture

Trước mỗi:

```python
session.send(prompt)
```

ghi:

```text
prompt_debug/<turn-id>.txt
prompt_debug/<turn-id>.json
```

JSON:

```json
{
  "client": "claude-code",
  "protocol": "anthropic",
  "session_id": "...",
  "model": "...",
  "tool_names": [],
  "prompt_chars": 12345,
  "sha256": "...",
  "correction": false
}
```

TXT chứa prompt thực tế sau redaction.

Correction prompt cũng phải dump.

---

# 11. Tool format

Không DSML.

Canonical model-facing protocol:

```xml
<tool_calls>
  <invoke name="Read">
    <parameter name="file_path"><![CDATA[/path/file.py]]></parameter>
  </invoke>
</tool_calls>
```

Nhưng XML chỉ là **transport giữa GPT Web và gateway**.

Client không bao giờ thấy XML.

Client thấy native:

OpenAI:

```json
{
  "tool_calls": [...]
}
```

Anthropic:

```json
{
  "type": "tool_use"
}
```

---

# 12. Tool parser pipeline

Parser phải có stages:

```text
raw model text
↓
tool block extraction
↓
XML parse
↓
schema coercion
↓
argument validation
↓
virtual-tool normalization
↓
client-tool mapping
```

Không vừa regex vừa translate trong một function.

---

# 13. Malformed tool repair

Repair chỉ cho lỗi transport phổ biến.

Ví dụ:

```text
unescaped XML chars
CDATA closure issue
collapsed whitespace
JSON scalar → expected string
```

Không repair semantics nguy hiểm.

Nếu:

```text
tool = Bash
command missing
```

không tự bịa command.

Phải correction GPT.

---

# 14. Virtual tools

ChatGPT Web không cần biết tool set kỳ quặc của từng CLI.

Gateway cung cấp canonical tools.

Ví dụ:

```text
Read
Write
Edit
Bash
Glob
Grep
```

Sau đó map.

Claude Code không native Write?

```text
GPT:
Write(...)

gateway:
Write → Bash safe writer

Claude Code:
Bash(...)
```

---

# 15. Viết file không phụ thuộc whitespace rendering

Không dùng:

```xml
<content><![CDATA[
def foo():
    return 1
]]></content>
```

làm canonical duy nhất vì ChatGPT Web có thể collapse indentation.

Canonical Write:

```text
file_path
lines
```

Ví dụ:

```text
0|def foo():
4|return 1
```

Gateway decode:

```python
def foo():
    return 1
```

Support cả:

```text
0|def foo(): 4|return 1
```

nếu web collapse newline.

---

# 16. Write validation

Trước khi tool call trả về Claude Code:

For `.py`:

```text
decode lines
ast.parse()
```

Nếu parse fail:

```text
DO NOT execute write
```

Thay vào đó correction:

```text
Your Write content is invalid Python:
IndentationError line X...
Return exactly one corrected Write call.
```

Đây là khác biệt lớn so với hiện tại.

Không để Claude Code ghi file hỏng rồi mới discover.

---

# 17. File-type validators

Theo extension:

```text
.py → ast.parse
.json → json.loads
.toml → tomllib.loads
.yaml → optional parser nếu installed
.md → no syntax validation
```

---

# 18. Atomic writes

Virtual Write phải:

```text
write temp
validate
fsync optional
rename
```

Không:

```text
truncate real file
write halfway
crash
```

Pseudo:

```text
foo.py.webgpt.tmp
↓
validate
↓
os.replace
```

---

# 19. Tool scheduler

Đây là một fix quan trọng.

GPT Web không được phát 16 coding tool calls một turn.

Policy coding:

```text
max_model_tool_calls_per_turn = 1
```

Nếu model output:

```text
Write a.py
Write b.py
Write c.py
```

Gateway không gửi cả 3.

Correction:

```text
Return exactly ONE tool invocation.
Complete tasks sequentially.
```

---

# 20. Tool-result correlation

Mỗi call:

```text
toolu_<stable-id>
```

State:

```python
pending_tool_calls = {
    id: {
        name,
        arguments_hash,
        created_turn,
        consumed=False
    }
}
```

Tool result phải:

```text
exist
not consumed
same conversation
```

Sau consumption:

```text
consumed=True
```

Duplicate result:

```text
idempotent handling
```

hoặc deterministic 409.

---

# 21. Never fabricate tool success

Nếu GPT trả:

```text
Done. File created.
```

nhưng không tool call:

```text
false completion
```

Gateway phải detect khi task clearly requires tools.

Không được gửi final đó về client.

---

# 22. Tool refusal detector

Không whack-a-mole từng phrase.

Semantic detector dựa trên:

```text
tools available
task requires tool
assistant has no tool call
response contains refusal/capability denial patterns
```

Patterns normalized:

```text
not exposed
cannot access filesystem
don't have shell
unable to call tools
cannot truthfully
won't fabricate
controller unavailable
```

Correction một lần hoặc bounded N.

Không infinite retry.

---

# 23. Correction budget

Per model turn:

```text
max_corrections = 2
```

Types:

```text
TOOL_REFUSAL
MALFORMED_TOOL
MULTI_TOOL
INVALID_WRITE
FALSE_COMPLETION
MISSING_REQUIRED_TOOL
```

Nếu vượt:

```text
provider-style structured error
```

Không treo.

---

# 24. Error contract

Map internal error → API error.

Ví dụ:

```text
UIChanged              → 503 web_ui_changed
AuthRequired           → 503 anonymous_session_unavailable
GenerationTimeout      → 504 generation_timeout
ConversationConflict   → 409 conversation_conflict
MalformedToolCall      → 502 malformed_model_tool_call
RateLimit              → 429 rate_limit
```

Body:

```json
{
  "error": {
    "type": "...",
    "code": "...",
    "message": "...",
    "retryable": true
  }
}
```

---

# 25. Retry policy

Không retry chung chung.

### Retryable

```text
navigation transient
generation timeout before submit
temporary UI stale
HTTP network transient
```

### Reconcile first

```text
commit_unknown
```

### Do not retry

```text
auth required
schema invalid
tool_call mismatch
rate-limit
```

`rate-limit` ở đây nghĩa là **không resend mù cùng logical request trên browser/session đã bị rate-limit**. Theo amendment hiện hành, khi gặp 429, live harness phải đóng hoàn toàn ephemeral Free-anonymous browser/session hiện tại và khởi tạo browser/session mới trước lần thử tiếp theo. Số lần restart phải hữu hạn theo budget của harness; không đổi IP/proxy và không bypass security challenge. Nếu 429 xảy ra giữa multi-turn/coding workflow, không tiếp tục conversation cũ: archive/reset attempt đó và chạy lại logical workflow từ đầu trên browser/session mới.

---

# 26. Backoff

Provider-like:

```text
250 ms
500 ms
1000 ms
```

bounded.

Không:

```text
while True
```

---

# 27. Process lifecycle

Mỗi benchmark run:

```text
gateway PID
browser PID
Claude PID
```

được ghi vào:

```text
run.json
```

Exit phải cleanup child process.

Thêm test:

```text
timeout Claude
gateway stop
→ no orphan Claude
→ no orphan browser
```

---

# 28. Prewarm

`--prewarm` chỉ optimization.

Không được là requirement.

Flow:

```text
server starts
↓
prewarm attempted
↓
fail?
log warning
continue serving
```

Request đầu phải self-initialize.

---

# 29. Generation timeout

Config explicit:

```text
--generation-timeout 45
```

Áp dụng cho:

```text
normal prompt
correction prompt
continuation prompt
```

Không prompt nào được escape timeout.

---

# 30. Rate-limit protection

Gateway phải tránh tự gây 429.

Per session:

```text
one active generation
```

Global Free anonymous:

```text
max browser generations = 1
```

Không parallel GPT Web generation.

Client có thể parallel request nhưng gateway queue.

---

# 31. Prompt-size budget

Trước gửi GPT Web:

```text
prompt_chars
estimated_tokens
tool_schema_chars
conversation_chars
```

Threshold.

Nếu quá lớn:

```text
compact old tool protocol
retain relevant transcript
```

Không silently send 100k chars rồi timeout.

---

# 32. Conversation compaction

Không được bỏ tool state.

Retain:

```text
system contract
current user objective
last assistant tool call
matching tool result
recent context
```

Old context:

```text
summarized deterministically where safe
```

Không dùng model summarization nếu có thể gây drift trong coding state.

---

# 33. Streaming OpenAI

Phải test byte-level order:

```text
data: role
data: content/tool delta
data: finish_reason
data: optional usage
data: [DONE]
```

Nếu opencode gửi:

```json
"stream_options": {"include_usage": true}
```

gateway nên emit usage-compatible final chunk.

---

# 34. Streaming Anthropic

Order:

```text
message_start
content_block_start
content_block_delta
content_block_stop
message_delta
message_stop
```

Tool:

```text
content_block_start input={}
input_json_delta
content_block_stop
```

Claude Code regression bắt buộc.

---

# 35. Client test matrix

Phải pass:

| Client      | Text | Stream | Tool | Tool result | Multi-turn |
| ----------- | ---: | -----: | ---: | ----------: | ---------: |
| curl OpenAI |    ✓ |      ✓ |    ✓ |           ✓ |          ✓ |
| OpenCode    |    ✓ |      ✓ |    ✓ |           ✓ |          ✓ |
| Claude Code |    ✓ |      ✓ |    ✓ |           ✓ |          ✓ |

---

# 36. OpenCode live benchmark

Không chỉ fake server.

Sau fake compatibility:

```text
opencode
→ actual WebGPT gateway
→ Free anonymous
```

Task 1:

```text
Reply exactly OC_FREE_OK
```

Task 2:

```text
Use bash to run pwd and return output.
```

Task 3:

```text
Create a tiny valid Python file and run it.
```

Pass cả 3 mới đánh dấu OpenCode live-compatible.

---

# 37. Claude Code micro-gates

Trước PCAP, chạy lần lượt.

### Gate C1

```text
text only
```

### C2

```text
Read SPEC.md
```

### C3

```text
Bash pwd
```

### C4

```text
Write one Python file
```

### C5

```text
Edit existing Python file
```

### C6

```text
Write → Bash compile → final
```

### C7

```text
3 sequential files
```

### C8

```text
failing test → Claude fixes it
```

PCAP chỉ chạy khi C1–C8 pass.

---

# 38. PCAP clean-room policy

Mỗi attempt:

```text
~/Downloads/webgpt/benchmarks/pcap/run-XXXX/
```

Contains only:

```text
SPEC.md
```

Claude Code phải tạo mọi thứ còn lại.

Nếu attempt fail vì:

```text
gateway bug
tool corruption
conversation corruption
malformed project generation caused by gateway
```

archive:

```text
failed-runs/run-XXXX/
```

Sau fix:

```text
NEW workspace
```

Không reuse project cũ.

---

# 39. Khi nào không cần reset toàn bộ PCAP?

Nếu lỗi hoàn toàn là lỗi project logic bình thường:

```text
assert expected 5 got 4
```

và gateway vận hành đúng:

```text
Claude Code phải tự fix ngay trong run đó
```

Đây chính là behavior của coding agent thật.

Nhưng nếu gateway gây:

```text
lost indentation
missing file
duplicated write
bad tool result
conversation reset
```

run đó invalid.

Reset từ đầu.

---

# 40. PCAP SPEC phải deterministic

SPEC cần chứa:

```text
architecture
CLI
input/output contract
fixtures
expected JSON schema
expected markdown fields
failure cases
test requirements
exit codes
MITRE mapping behavior
Zeek parsing behavior
Suricata parsing behavior
RITA behavior
fallback detector behavior
```

Không để Claude tự invent acceptance criteria.

---

# 41. PCAP implementation gates

Claude phải tự pass từng stage.

### P1 Skeleton

```text
package exists
pyproject exists
tests exist
```

### P2 Syntax

```bash
python -m compileall -q .
```

### P3 Unit tests

```bash
pytest -q
```

### P4 CLI help

```bash
python -m pcap_analysis_automation --help
```

### P5 Fixture analysis

```bash
python -m pcap_analysis_automation \
  --input tests/fixtures/... \
  --out out \
  --format both
```

### P6 JSON

```text
parseable
expected keys
findings list
MITRE fields
metadata
```

### P7 Markdown

```text
exists
nonempty
contains summary/findings
```

### P8 Failure behavior

Bad path:

```text
non-zero exit
clear stderr
```

---

# 42. PCAP scoring

Rubric 100.

Ví dụ:

```text
Architecture                 10
Metadata extraction          10
Zeek normalization           15
Suricata integration         10
RITA integration             10
Fallback detection           15
MITRE mapping                10
JSON report                   5
Markdown report               5
CLI/error handling            5
Tests/documentation           5
```

Target không phải 75 nữa.

Với yêu cầu mới:

```text
required score = 100/100 hoặc tất cả mandatory rubric item pass
```

---

# 43. Fault injection tests

Để gọi là API thật, phải test lỗi chủ động.

Inject:

```text
browser send timeout
browser reload
malformed XML
duplicate tool result
tool result wrong ID
slow GPT response
empty assistant response
refusal
16 tool calls
collapsed Write.lines
conversation ID disappears
prewarm failure
```

Gateway phải phản ứng deterministic.

---

# 44. Soak test

Sau PCAP pass một lần chưa đủ.

Chạy ít nhất:

```text
10 simple text requests
10 sequential tool requests
5 multi-turn tool workflows
3 tiny coding projects
```

Không:

```text
process leak
conversation collision
tool ID collision
browser crash
```

---

# 45. Restart test

API thật phải survive restart behavior hợp lý.

Test:

```text
server stop
server start
new conversation works
```

Không bắt restore impossible browser state, nhưng không được corrupt store.

---

# 46. Security / isolation

Tool args phải không cho virtual writer escape workspace.

Normalize:

```python
resolved_path
```

Check:

```text
resolved_path inside client cwd/workspace
```

Nếu:

```text
../../etc/passwd
```

reject.

---

# 47. Observability schema

Mỗi request có:

```text
request_id
client
protocol
gateway_session_id
browser_conversation_id
turn_id
duration
queue_ms
browser_ms
parse_ms
tool_count
correction_count
status
error
```

Mỗi PCAP run có summary:

```json
{
  "api_requests": 0,
  "successful_requests": 0,
  "tool_calls": 0,
  "corrections": 0,
  "timeouts": 0,
  "errors": [],
  "average_latency_ms": 0
}
```

---

# 48. Error budget cho final successful run

Final PCAP certification run:

```text
unexpected gateway errors = 0
malformed tools = 0
manual gateway intervention = 0
conversation conflicts = 0
tool mismatches = 0
process crashes = 0
```

Model có thể viết bug project rồi tự sửa, đó là coding behavior bình thường.

Nhưng **gateway-layer failure phải bằng 0**.

---

# 49. Automated regression gate sau mỗi gateway patch

Mỗi patch:

```bash
pytest focused tests
bash scripts/verify.sh
```

Chỉ khi xanh mới live-test.

Không:

```text
patch
→ immediately PCAP
```

---

# 50. Mandatory manual verification

Sau automated tests pass:

1. Start Free anonymous gateway bằng command thực.
2. Dùng Claude Code thực.
3. Chạy một micro workflow thực.
4. Mở file được tạo.
5. Chạy file.
6. Kiểm tra stdout.
7. Inspect trace.
8. Xác nhận không fallback account.
9. Ghi:

```text
MANUAL_PASS
```

Đây là bước bắt buộc, không được thay bằng pytest.

---

# 51. Final certification run

Khi mọi micro-gate xanh:

```text
fresh browser
fresh gateway
fresh anonymous session
fresh Claude config
fresh PCAP workspace
```

Claude Code nhận duy nhất:

```text
SPEC.md
```

Sau đó nó tự:

```text
inspect
implement
test
debug
fix
verify
```

Mình chỉ quan sát gateway.

Không can thiệp project.

---

# 52. Điều kiện phải restart certification từ đầu

Nếu trong certification xuất hiện bất kỳ lỗi gateway nào:

```text
parser fix required
prompt fix required
tool normalization fix
session fix
stream fix
client compatibility fix
browser fix
```

thì:

```text
certification INVALID
```

Flow:

```text
archive failed run
↓
fix gateway
↓
focused tests
↓
full gate
↓
manual micro-test
↓
NEW PCAP WORKSPACE
↓
restart certification from SPEC.md
```

Không tiếp tục workspace cũ.

---

# 53. “Không một chút sai sót” được định nghĩa thế nào

Không thể yêu cầu GPT không bao giờ viết bug trung gian, vì coding agent thật cũng có thể:

```text
write implementation
run test
find bug
fix bug
```

Cái mình có thể đảm bảo ở acceptance level là:

### Tool/API layer

```text
0 unexpected failures
```

### Agent workflow

```text
mọi bug Claude tạo được Claude tự phát hiện/sửa
```

### Final project

```text
0 known failing tests
0 syntax errors
0 incomplete mandatory feature
0 manual verification failure
```

Đây mới tương đương “Claude Code dùng fake API như API thật”.

---

# 54. Thứ tự execution thực tế

Thứ tự chính xác:

```text
Phase 1
Restore + freeze Free anonymous baseline

Phase 2
Reverse current ChatGPT Web lifecycle

Phase 3
Harden browser state machine

Phase 4
Harden request/session/reconciliation

Phase 5
Complete OpenAI/Anthropic protocol fidelity

Phase 6
Complete structured XML tool bridge

Phase 7
Complete virtual tools + safe Write.lines

Phase 8
Complete correction/scheduler/validation

Phase 9
Claude Code micro-gates C1-C8

Phase 10
OpenCode live gates

Phase 11
Fault injection suite

Phase 12
PCAP clean implementation run

Phase 13
PCAP scoring

Phase 14
Soak/restart test

Phase 15
Manual verification

Phase 16
Fresh final PCAP certification run

Phase 17
MANUAL_PASS + final acceptance report
```

---

# 55. Progress ledger

File theo dõi tiến độ chính thức: [`GATEWAY_CERTIFICATION.md`](../reports/GATEWAY_CERTIFICATION.md).

Mỗi checkpoint có format:

```text
[PASS] F1 Free anonymous browser
[PASS] F2 Free anonymous text
[FAIL] F3 stream
[BLOCKED] C4 Claude Write
[NOT RUN] P1 PCAP
```

Mỗi PASS phải có:

```text
command
timestamp
artifact path
expected
actual
```

---

# 56. Artifact của run cuối

Final successful run phải được lưu nguyên vẹn:

```text
~/Downloads/webgpt/successful-runs/pcap-final/
├── workspace/
├── gateway.log
├── claude.log
├── prompt_debug/
├── trace.jsonl
├── request-summary.json
├── compile.log
├── pytest.log
├── cli-smoke.log
├── report.json
├── report.md
├── score.json
└── MANUAL_PASS.txt
```

---

## Nguyên tắc quan trọng nhất

Không còn flow:

```text
PCAP fail
→ vá một regex
→ PCAP tiếp
→ fail
→ vá tiếp
```

Mà là:

```text
observe exact behavior
→ identify violated invariant
→ fix architecture/invariant
→ add regression
→ full gate
→ manual micro verification
→ clean client run
```

Và **PCAP là certification benchmark cuối**, không phải nơi dùng để mò từng bug cơ bản của gateway.
