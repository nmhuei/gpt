# Design Specification: ChatGPT Web Gateway Layered Clean Architecture Refactoring

- **Date**: 2026-08-22
- **Author**: Antigravity & User Pair
- **Status**: Approved

---

## 1. Context & Objectives

The ChatGPT Web Gateway project enables AI coding clients (such as Claude Code CLI, OpenCode, and OpenAI SDK tools) to interact with ChatGPT Web Plus (`gpt-5-5-thinking` on `High Effort`) through a local API server.

### Core Requirements
1. **Model Auto-Lock (5.5 High)**:
   - Always navigate with `?model=gpt-5-5-thinking`.
   - Automatically click the Radix SliderControl at 88% width to select `High Effort`.
   - Pre-send guard in the composer flow to prevent sending while in Medium effort.
2. **Headless Background Execution**:
   - Zero visible Chrome GUI windows on user desktop.
   - Run CloakBrowser on `--headless=new` with remote debugging port `9222`.
   - Dedicated authenticated profile at `/home/light/Downloads/webgpt/cloak-profile`.
3. **Multi-Worker Concurrency for Claude Code Fan-Out**:
   - API Gateway on port `8000` supporting Anthropic (`/v1/messages`) and OpenAI (`/v1/chat/completions`).
   - Concurrently lease isolated worker tabs to process subagent requests without race conditions.
4. **Standardized & Flexible `.env` Configuration**:
   - Parse key-value configuration (`CHATGPT_EMAIL`, `CHATGPT_PASSWORD`, `CHATGPT_TOTP_KEY`, `CDP_PORT`, `API_PORT`, `PROFILE_DIR`, `DEFAULT_MODEL`, `DEFAULT_EFFORT`, `MAX_WORKERS`).
   - Backward compatibility for raw pipe format (`EMAIL|PASSWORD|TOTP_KEY`).
5. **Fast Protocol & MCP Bridge**:
   - Direct session token extraction via `/api/auth/session` in <100ms.
   - Gizmo Action registration and MCP tool bridge compatibility.

---

## 2. Target Directory Structure (Layered Clean Architecture)

```
gpt/
├── __init__.py
├── config/                  # Configuration & Environment loading
│   ├── __init__.py
│   └── settings.py          # AppConfig dataclass, .env loader, fallback parser
├── auth/                    # Authentication & 2FA
│   ├── __init__.py
│   ├── authenticator.py     # Email/Password + TOTP 2FA login automator
│   └── totp.py              # PyOTP 6-digit token generator
├── drivers/                 # Browser & DOM automation drivers
│   ├── __init__.py
│   ├── base.py              # Base driver interface & EventCallback
│   ├── ui.py                # Playwright DOM UI driver (5.5 High slider + composer)
│   └── fast_protocol.py     # Fast non-DOM protocol client & Gizmo manager
├── transport/               # Browser management & Worker pools
│   ├── __init__.py
│   ├── browser.py           # BrowserManager (CDP connection, process spawning)
│   ├── factory.py           # ChatGPTWorkerFactory (bounded semaphore pool)
│   └── session.py           # ChatGPTWebSession (logical turn manager & state machine)
├── gateway/                 # API Server & Protocols
│   ├── __init__.py
│   ├── server.py            # Starlette API Gateway (/v1/messages, /v1/chat/completions)
│   ├── adapters.py          # Anthropic & OpenAI request/response transformers
│   ├── runtime.py           # CompletionRuntime (conversation store & tool streaming)
│   └── model_registry.py    # Model mapping & alias resolution
├── cli/                     # CLI commands entrypoints
│   ├── __init__.py
│   └── main.py              # CLI parser (start, gateway, doctor, cloak-launch)
└── utils/                   # Shared utilities & helpers
    ├── __init__.py
    ├── state.py             # Exceptions and state machine definitions
    ├── types.py             # Data types, dataclasses, TurnResult
    ├── tracing.py           # Event bus and diagnostic logging
    └── toolcall.py          # Tool call transpiler and JSON sieving
```

---

## 3. Detailed Component Specifications

### 3.1 `gpt/config/settings.py`
- Parses standard `.env` containing `CHATGPT_EMAIL`, `CHATGPT_PASSWORD`, `CHATGPT_TOTP_KEY`.
- If line 1 contains `|`, splits into `(email, password, totp_key)` automatically.
- Provides singleton `get_config()` with defaults:
  - `CDP_PORT = 9222`
  - `API_PORT = 8000`
  - `BROWSER_HEADLESS = True`
  - `PROFILE_DIR = /home/light/Downloads/webgpt/cloak-profile`
  - `DEFAULT_MODEL = gpt-5-5-thinking`
  - `DEFAULT_EFFORT = high`
  - `MAX_WORKERS = 3`

### 3.2 `gpt/drivers/ui.py`
- Preserves 3-layer `5.5 High` auto-lock:
  1. `select_model()` URL parameter navigation (`?model=gpt-5-5-thinking`).
  2. `select_reasoning_effort("high")` with coordinate click at `box.x + box.width * 0.88`.
  3. Pre-send guard in `send()` checking composer pill before prompt execution.

### 3.3 `gpt/gateway/server.py`
- Fully backwards compatible with existing API server arguments.
- Exports Starlette app for uvicorn serving.
- Handles `/health`, `/models`, `/v1/messages` (Claude Code CLI), `/v1/chat/completions` (OpenAI clients).

### 3.4 Entry Point `gpt/debug.py` & `gpt/__init__.py`
- Re-exports all core classes (`ChatGPTWebSession`, `BrowserManager`, `ChatGPTWorkerFactory`, `CompletionRuntime`, `get_config`) to maintain 100% backward compatibility for all existing tests and scripts.

---

## 4. Verification Plan

1. **Unit Tests**: Run `pytest tests/` - all 196+ tests must pass.
2. **Linting**: Run `ruff check .` - 0 errors.
3. **Environment Test**: Test `get_config()` against `.env`.
4. **End-to-End Test**:
   - Launch headless CloakBrowser on port 9222.
   - Start API Gateway on port 8000 with `--allow-authenticated`.
   - Send Claude Code CLI prompt and verify response.
