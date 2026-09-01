# ChatGPT Web Gateway Layered Clean Architecture Implementation Plan & Auth Guide

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the ChatGPT Web Gateway codebase into a clean, modular Layered Architecture with a standardized `.env` configuration parser while preserving 100% feature parity (5.5 High effort selection, Headless CloakBrowser CDP, multi-worker concurrency, fast session/Gizmo bridge, and Anthropic/OpenAI API gateway).

**Architecture:** Split the monolithic repository into dedicated packages: `gpt/config/` (environment & settings), `gpt/auth/` (credentials & TOTP), `gpt/transport/` (browser lifecycle & session workers), `gpt/gateway/` (Starlette API server & protocol adapters), while maintaining facade compatibility in `gpt/__init__.py` and `gpt/debug.py`.

**Tech Stack:** Python 3.10+, Playwright, CloakBrowser, PyOTP, Starlette, Uvicorn, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-22-repo-architecture-refactoring-design.md`

## Global Constraints
- Preserve all existing unit tests without breaking existing public API signatures.
- Support both standard `.env` (`CHATGPT_EMAIL=...`) and legacy pipe format (`EMAIL|PASSWORD|TOTP`).
- Ensure 5.5 High auto-lock (URL + 88% slider click + pre-send guard) remains active.
- Default headless mode for zero desktop GUI interference.
- Maintain Ruff linter clean status (0 errors).

---

## Account Login And Profiles

Named accounts are optional. Without `--account`, browser-backed CLI commands and the gateway use anonymous mode. Each named account owns a separate persistent CloakBrowser profile under `~/.local/share/webgpt/profiles/<name>` (XDG runtime root, `$WEBGPT_PROFILES_ROOT` to override).

### Manual login (recommended)

```bash
gpt-web account login --name personal
```

This opens CloakBrowser headfully. Complete normal ChatGPT sign-in, MFA, and any human verification yourself. The command verifies `/api/auth/session`, closes CloakBrowser cleanly, and keeps the resulting browser profile for later headless reuse. CAPTCHA, Turnstile, phone verification, and other security challenges are never automated.

### Optional saved credentials

For operators who explicitly want automated username/password/TOTP login, credentials can be stored per account in a separate mode-0600 file. Prefer stdin so secrets do not appear in shell history:

```bash
printf '%s\n' 'user@example.com|password|BASE32_TOTP_SECRET' | \
  gpt-web account credentials-set --name personal --stdin

gpt-web account login --name personal --auto --use-saved
```

Or save while performing an explicit auto-login:

```bash
printf '%s\n' 'user@example.com|password|BASE32_TOTP_SECRET' | \
  gpt-web account login --name personal --auto --stdin --save-credentials
```

The registry `~/.config/webgpt/accounts.json` contains metadata only. Passwords/TOTP secrets are not stored there, in traces, or in gateway logs.

### Multiple accounts

```bash
gpt-web account login --name personal
gpt-web account login --name work
gpt-web account list
```

A gateway may use one or more named accounts:

```bash
gpt-web api-server --transport hybrid --account personal --prewarm

gpt-web api-server --transport hybrid \
  --account personal \
  --account work \
  --prewarm
```

With multiple accounts, new logical conversations are assigned round-robin and the selected account name is persisted with that conversation so later turns and tool results remain on the same ChatGPT account. `--max-workers` applies per account.

### Anonymous mode

No account selection means anonymous mode:

```bash
gpt-web api-server --transport browser
gpt-web doctor --browser
gpt-web send --text 'hello'
```

Anonymous mode uses an ephemeral browser profile and never falls back to a saved authenticated profile.

### Account status and cleanup

```bash
gpt-web account status --name personal
gpt-web account status --name personal --live
gpt-web account credentials-delete --name personal
gpt-web account remove --name personal
gpt-web account remove --name personal --delete-profile
```

`status --live` opens the saved profile headlessly and verifies the ChatGPT session without reading or manipulating the page DOM.

### Using an account from other commands

```bash
gpt-web doctor --account personal --browser
gpt-web models --account personal
gpt-web send --account personal --text 'hello'
```

Claude Code and OpenCode do not need to know which ChatGPT account is behind the gateway. They continue to use the normal OpenAI/Anthropic-compatible local endpoints.

## 📋 Task Checklist Tái Cấu Trúc (Đã Hoàn Thành)

- [x] **Task 1: Environment & Configuration Module (`gpt/config/settings.py`)**
- [x] **Task 2: Modularize Auth & TOTP Subsystem (`gpt/auth/`)**
- [x] **Task 3: Modularize Transport Layer (`gpt/transport/`)**
- [x] **Task 4: Modularize Gateway & Adapters (`gpt/gateway/`)**
- [x] **Task 5: Root Facade Compatibility & CLI Integration**
- [x] **Task 6: Live Manual Verification & Screenshot Evidence**
