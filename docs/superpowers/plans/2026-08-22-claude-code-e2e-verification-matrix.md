# Claude Code CLI Full Conformance & Verification Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to execute this verification plan step-by-step. All verification steps use checkbox (`- [ ]`) syntax for status tracking.

**Goal:** End-to-end certification of the ChatGPT Web Gateway as an Anthropic-compatible LLM backend for Claude Code CLI, proving correctness across all 10 verification phases and tool capabilities (Agent fan-out, Bash, Cron, LSP, Worktrees, SSE Streaming, and Error recovery).

**Architecture:** Claude Code CLI (`ANTHROPIC_BASE_URL=http://127.0.0.1:8000`) communicates via standard Anthropic `/v1/messages` protocol with Starlette Gateway. Gateway handles token translation, tool call transpilation, and dispatches via either `HybridWorkerFactory` (curl_cffi + browser TokenManager) or Browser UI Driver.

**Tech Stack:** Claude Code CLI, Python 3.13, Starlette, curl_cffi, Playwright / CloakBrowser, Pytest, PyOTP.

**Spec / Ground Truth:** `/home/light/Documents/WEBGPT SESSION BOOTSTRAP.txt` and [Gateway Protocol Reference](https://code.claude.com/docs/en/llm-gateway-protocol).

---

## Global Constraints

- **No Premature Buffer**: Streaming responses must emit `message_start` immediately; no full turn buffering before first byte.
- **Pass-through Headers**: Never drop `anthropic-beta`, `anthropic-version`, `x-claude-code-session-id`, `x-claude-code-agent-id`.
- **Zero-GUI Headless Mode**: All browser workers must run in background (`--headless=new`, CDP `9222`).
- **Standard Error Format**: Errors returned as RFC 9457 / Anthropic schema `{"type": "error", "error": {"type": "...", "message": "..."}}`.
- **Real Evidence Gate**: Tests are certified only with real CLI execution logs and exit codes.

---

## Phase Breakdown & Verification Tasks

### Task 1: Protocol & Route Contract Conformance (Gate 1 & Gate 3)

**Files:**
- Test: `tests/test_claude_code_conformance.py`
- Gateway: `gpt/gateway/server.py`

- [ ] **Step 1.1**: Verify `POST /v1/messages` and `POST /v1/messages?beta=true` match router without 404.
- [ ] **Step 1.2**: Verify `POST /v1/messages/count_tokens` returns `{"input_tokens": N}` with correct character-to-token ratio.
- [ ] **Step 1.3**: Verify `GET /v1/models` lists models and aliases (`claude-3-5-sonnet`, `claude-3-opus`, `gpt-5-5-thinking`).
- [ ] **Step 1.4**: Verify header pass-through (`anthropic-version`, `anthropic-beta`, `x-claude-code-session-id`) does not strip open beta flags.

---

### Task 2: Real-time SSE Streaming & Heartbeat Keep-Alive (Gate 2)

**Files:**
- Gateway: `gpt/gateway/server.py`
- Test: `tests/test_claude_code_conformance.py`

- [ ] **Step 2.1**: Test event sequence order:
  `message_start` -> `content_block_start` -> `content_block_delta` -> `content_block_stop` -> `message_delta` -> `message_stop`.
- [ ] **Step 2.2**: Test keep-alive ping emission (`: ping\n\n` or `event: ping`) during 30s+ thinking phases to satisfy the 300s watchdog.
- [ ] **Step 2.3**: Measure TTFT (Time-To-First-Token) to ensure immediate socket response.

---

### Task 3: Tool Transpilation & Execution Cycle (Gate 4)

**Files:**
- Transpiler: `gpt/utils/toolcall.py`
- Sieve: `gpt/utils/toolstream.py`
- Test: `tests/test_tool_transpiler.py`

- [ ] **Step 3.1: File System Tools**:
  - Test `Read`: Reads files with 1-based indexing, handles line offsets and limits.
  - Test `Edit`: Exact string replacement with unique matching.
  - Test `Write`: File creation and overwriting.
- [ ] **Step 3.2: Execution & Bash Tool**:
  - Test `Bash`: Runs shell commands, respects timeouts, supports `run_in_background`.
- [ ] **Step 3.3: Cron Tools**:
  - Test `CronCreate`: One-shot (`recurring: false`) and recurring (`recurring: true`) 5-field cron parsing.
  - Test `CronList` & `CronDelete`: Lists active session jobs and deletes by ID.
- [ ] **Step 3.4: Code Intelligence & Notebook Tools**:
  - Test `LSP`: `goToDefinition`, `findReferences`, `hover`, `documentSymbol`.
  - Test `NotebookEdit`: Replace, insert, and delete cells in Jupyter notebooks.
- [ ] **Step 3.5: Git Worktree Tools**:
  - Test `EnterWorktree` & `ExitWorktree`: Switch session to isolated worktree and clean up.

---

### Task 4: Subagent Spawning & Fan-Out Load Testing

**Files:**
- Worker Factory: `gpt/transport/hybrid.py` & `gpt/transport/factory.py`
- Test: `tests/live/test_subagent_fanout.py`

- [ ] **Step 4.1**: Test `Agent` tool spawning 2 parallel subagents (`subagent_type`, `run_in_background: true`).
- [ ] **Step 4.2**: Test `Agent` fan-out with maximum concurrency (5-10 workers) via `HybridWorkerFactory`.
- [ ] **Step 4.3**: Verify inter-agent messaging with `ListAgents` and `SendMessage({to: "<agent_name>", message: "..."})`.
- [ ] **Step 4.4**: Verify background completion notifications land in session queue without dropping.

---

### Task 5: Error Propagation, Cancellation & Soak Testing (Gate 5 & Gate 6)

**Files:**
- Server: `gpt/gateway/server.py`
- Test: `tests/test_claude_code_conformance.py`

- [ ] **Step 5.1**: Test client disconnect (`stream.aclose()`) cancels upstream generation without leaking worker slots.
- [ ] **Step 5.2**: Test typed error propagation for RateLimited (429), ModelUnavailable (404), InvalidRequest (400), and AuthRequired (401).
- [ ] **Step 5.3**: Run 30-minute soak test measuring RSS memory stability (no OOM / memory leaks).

---

### Task 6: Live Claude Code CLI End-to-End OSINT / Coding Benchmark

**Environment:**
```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8000"
export ANTHROPIC_API_KEY="sk-webgpt-local"
claude "Research how to solve an OSINT challenge and fan out 3 subagents to investigate different techniques"
```

- [ ] **Step 6.1**: Start Gateway server (`gpt api-server --transport hybrid --port 8000 --max-workers 4 --headless`).
- [ ] **Step 6.2**: Execute Claude Code CLI command with subagent fan-out.
- [ ] **Step 6.3**: Verify subagents complete in parallel and final response is rendered cleanly in terminal.
- [ ] **Step 6.4**: Export `FINAL_API_CERTIFICATION.md` report with 100-point score breakdown.
