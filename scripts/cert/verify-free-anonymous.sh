#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RUNTIME_ROOT="${WEBGPT_RUNTIME_ROOT:-${HOME}/.local/share/webgpt}"
RUNS_DIR="${RUNTIME_ROOT}/runs/smoke"
TMP_DIR="${RUNTIME_ROOT}/tmp"
mkdir -p "${RUNS_DIR}" "${TMP_DIR}"
chmod 700 "${RUNTIME_ROOT}" "${RUNS_DIR}" "${TMP_DIR}" 2>/dev/null || true
export TMPDIR="${TMP_DIR}"

LOG_FILE="${RUNS_DIR}/verify-free-anonymous-$(date +%Y%m%d-%H%M%S).log"
touch "${LOG_FILE}"
chmod 600 "${LOG_FILE}"
echo "=== Starting Free Anonymous Golden Baseline Verification ===" | tee -a "${LOG_FILE}"
echo "Repo: ${REPO_DIR}" | tee -a "${LOG_FILE}"
echo "Log: ${LOG_FILE}" | tee -a "${LOG_FILE}"

cd "${REPO_DIR}"

uv run python - <<'PYCHECK' 2>&1 | tee -a "${LOG_FILE}"
from __future__ import annotations

import asyncio
import json
import os
import sys
import httpx

from gpt.session import ChatGPTWebSession
from gpt.api.server import create_api_app
from gpt.runtime_paths import free_anonymous_gateway_lock
from gpt.state import AuthRequired, CommitUnknown, RateLimited, WebChatError

GREEN = "\033[92m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"


def pass_msg(check_id: str, desc: str) -> None:
    print(f"{GREEN}[PASS]{RESET} {BOLD}{check_id}{RESET}: {desc}")


def warn_msg(check_id: str, desc: str) -> None:
    print(f"[WARN] {check_id}: {desc}")


def fail_msg(check_id: str, desc: str, detail: str = "") -> None:
    print(f"{RED}[FAIL]{RESET} {BOLD}{check_id}{RESET}: {desc}\n  Detail: {detail}")
    sys.exit(1)


def browser_restart_budget() -> int:
    raw = os.getenv("WEBGPT_ANON_BROWSER_RESTARTS", "3")
    try:
        value = int(raw)
    except ValueError:
        fail_msg("CONFIG", "Browser restart budget", f"Invalid WEBGPT_ANON_BROWSER_RESTARTS={raw!r}")
    if value < 0:
        fail_msg("CONFIG", "Browser restart budget", "WEBGPT_ANON_BROWSER_RESTARTS must be >= 0")
    return value


async def hard_new_anonymous_session(label: str) -> ChatGPTWebSession:
    """Open a brand-new ephemeral browser process/context for one Free-anon attempt."""
    print(f"[INFO] {label}: opening a brand-new ephemeral anonymous browser session")
    session = await ChatGPTWebSession.create(
        headless=True,
        persistent=False,
        profile_dir=None,
    )
    auth_status = await session.ui_driver.auth_status()
    if auth_status == "authenticated":
        await session.close()
        raise AuthRequired(
            "Certification requires an unauthenticated Free anonymous ChatGPT Web session."
        )
    if auth_status != "anonymous":
        await session.close()
        raise RateLimited(
            f"Fresh browser anonymous state unavailable (auth_status={auth_status!r}); restart required."
        )
    return session


async def close_browser_session(session: ChatGPTWebSession | None, label: str) -> None:
    if session is None:
        return
    try:
        await session.close()
        print(f"[INFO] {label}: closed browser/session before next phase")
    finally:
        # Give Playwright/Chromium a deterministic shutdown boundary so the next
        # attempt really behaves like reopening the browser manually.
        await asyncio.sleep(1.0)


async def run_with_hard_browser_restarts(check_id: str, description: str, workflow):
    """Retry a complete logical workflow on a fully fresh browser after 429.

    The rate-limited browser context is never reused. A multi-turn workflow is
    replayed from its first turn after restart, preserving conversation semantics.
    """
    max_restarts = browser_restart_budget()
    session: ChatGPTWebSession | None = None
    last_rate_limit: RateLimited | None = None
    for attempt in range(max_restarts + 1):
        try:
            session = await hard_new_anonymous_session(f"{check_id}-attempt-{attempt + 1}")
            result = await workflow(session)
            if attempt:
                pass_msg(
                    f"{check_id}R",
                    f"Recovered after {attempt} complete anonymous browser/session restart(s)",
                )
            return session, result
        except RateLimited as exc:
            last_rate_limit = exc
            await close_browser_session(session, f"{check_id}-rate-limited-{attempt + 1}")
            session = None
            if attempt >= max_restarts:
                fail_msg(
                    f"{check_id}_RATE_LIMITED",
                    description,
                    f"Rate limit persisted after {attempt} complete browser/session restart(s): {exc}",
                )
            warn_msg(
                check_id,
                f"rate-limited browser/session discarded; reopening a completely fresh browser "
                f"and restarting the whole workflow ({attempt + 1}/{max_restarts})",
            )
        except AuthRequired as exc:
            await close_browser_session(session, f"{check_id}-auth-required")
            fail_msg(f"{check_id}_AUTH_REQUIRED", description, str(exc))
        except CommitUnknown as exc:
            await close_browser_session(session, f"{check_id}-commit-unknown")
            fail_msg(f"{check_id}_COMMIT_UNKNOWN", description, str(exc))
        except WebChatError as exc:
            await close_browser_session(session, f"{check_id}-webchat-error")
            fail_msg(f"{check_id}_WEBCHAT_ERROR", description, f"{type(exc).__name__}: {exc}")
        except BaseException:
            await close_browser_session(session, f"{check_id}-exception-cleanup")
            raise
    raise AssertionError(f"unreachable rate-limit loop exit: {last_rate_limit}")


async def run_gateway_with_hard_browser_restarts(check_id: str, description: str, workflow):
    """Run one gateway workflow, rebuilding the whole gateway/browser on 429."""
    max_restarts = browser_restart_budget()
    for attempt in range(max_restarts + 1):
        app = create_api_app(
            headless=True,
            persistent=False,
            profile_dir=None,
            max_workers=1,
            require_anonymous=True,
        )
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway.local") as client:
                result = await workflow(client)
            if attempt:
                pass_msg(
                    f"{check_id}R",
                    f"Recovered after {attempt} complete gateway/browser restart(s)",
                )
            return result
        except RateLimited as exc:
            if attempt >= max_restarts:
                fail_msg(
                    f"{check_id}_RATE_LIMITED",
                    description,
                    f"Gateway rate limit persisted after {attempt} complete browser restart(s): {exc}",
                )
            warn_msg(
                check_id,
                f"gateway received 429; closing gateway/browser and recreating it "
                f"before restarting the workflow ({attempt + 1}/{max_restarts})",
            )
        finally:
            await app.state.server.close()
            await asyncio.sleep(1.0)
    raise AssertionError("unreachable gateway restart loop exit")


async def run_baseline_checks() -> None:
    print("\n--- F1: Browser Launch & Unauthenticated State ---")

    async def f1_workflow(session: ChatGPTWebSession):
        composer = await session.ui_driver.get_composer(timeout_ms=10_000)
        if composer is None:
            fail_msg("F1", "Composer detection", "No semantic composer found")
        await composer.fill("F1_SEND_BUTTON_PROBE")
        send_button = await session.ui_driver.get_send_button()
        if send_button is None or not await send_button.is_enabled(timeout=1_000):
            fail_msg("F1", "Send button detection", "Send control was not enabled after filling composer")
        await composer.fill("")
        return True

    diagnostic, _ = await run_with_hard_browser_restarts(
        "F1", "Browser launch / anonymous composer check", f1_workflow
    )
    pass_msg("F1", "Browser starts, ChatGPT page loads, not authenticated, composer and send control found")
    await close_browser_session(diagnostic, "F1")

    print("\n--- F2: Direct Web Send From Fresh Browser ---")

    async def f2_workflow(session: ChatGPTWebSession):
        result = await session.send("Reply exactly: FREE_ANON_BASELINE_OK", timeout_seconds=60)
        if "FREE_ANON_BASELINE_OK" not in result.text:
            fail_msg("F2", "Direct web send", f"Expected 'FREE_ANON_BASELINE_OK', got: {result.text!r}")
        return result

    f2_session, res2 = await run_with_hard_browser_restarts(
        "F2", "Direct web send", f2_workflow
    )
    pass_msg("F2", f"Direct web send returned {res2.text!r} in {res2.duration_ms}ms")
    try:
        await f2_session.new_conversation()
        pass_msg("F1N", "New conversation is available after a completed turn")
    finally:
        await close_browser_session(f2_session, "F2")

    print("\n--- F5: Session Continuation In Its Own Fresh Browser ---")

    async def f5_workflow(session: ChatGPTWebSession):
        await session.send("Remember token ABC-719. Reply with OK.", timeout_seconds=60)
        result = await session.send(
            "What token did I give you? Reply with only the token.", timeout_seconds=60
        )
        if "ABC-719" not in result.text:
            fail_msg(
                "F5",
                "Session continuation",
                f"Expected token 'ABC-719' in Turn 2, got: {result.text!r}",
            )
        return result

    f5_session, r_t2 = await run_with_hard_browser_restarts(
        "F5", "Session continuation", f5_workflow
    )
    pass_msg(
        "F5",
        f"Session continuation successfully retrieved token ABC-719 (Turn 2: {r_t2.text!r})",
    )
    await close_browser_session(f5_session, "F5")

    print("\n--- F3: Gateway Non-Stream (/v1/chat/completions) ---")

    async def f3_workflow(client: httpx.AsyncClient):
        payload = {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": "Reply exactly: FREE_ANON_BASELINE_OK"}],
            "stream": False,
        }
        response = await client.post("/v1/chat/completions", json=payload, timeout=60.0)
        if response.status_code == 429:
            raise RateLimited(response.text)
        if response.status_code != 200:
            fail_msg("F3", "Gateway non-stream", f"Expected 200, got {response.status_code}: {response.text}")
        content = response.json()["choices"][0]["message"]["content"]
        if "FREE_ANON_BASELINE_OK" not in content:
            fail_msg("F3", "Gateway non-stream content", f"Expected 'FREE_ANON_BASELINE_OK', got: {content!r}")
        return content

    content = await run_gateway_with_hard_browser_restarts(
        "F3", "Gateway non-stream", f3_workflow
    )
    pass_msg("F3", f"Gateway non-stream returned 200 and expected content: {content!r}")

    print("\n--- F4: Gateway Stream (/v1/chat/completions stream=true) ---")

    async def f4_workflow(client: httpx.AsyncClient):
        payload = {
            "model": "chatgpt-web",
            "messages": [{"role": "user", "content": "Reply exactly: FREE_ANON_STREAM_OK"}],
            "stream": True,
        }
        async with client.stream(
            "POST", "/v1/chat/completions", json=payload, timeout=60.0
        ) as response:
            if response.status_code == 429:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise RateLimited(body)
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", errors="replace")
                fail_msg("F4", "Gateway stream status", f"Expected 200, got {response.status_code}: {body}")
            has_role_chunk = False
            has_text_chunk = False
            has_finish_reason = False
            has_done = False
            raw_lines: list[str] = []
            async for line in response.aiter_lines():
                if not line:
                    continue
                raw_lines.append(line)
                if line.strip() == "data: [DONE]":
                    has_done = True
                    continue
                if not line.startswith("data: "):
                    continue
                try:
                    parsed = json.loads(line[len("data: "):])
                except Exception:
                    continue
                delta = parsed.get("choices", [{}])[0].get("delta", {})
                finish = parsed.get("choices", [{}])[0].get("finish_reason")
                if delta.get("role") == "assistant":
                    has_role_chunk = True
                if delta.get("content"):
                    has_text_chunk = True
                if finish == "stop":
                    has_finish_reason = True
            if not (has_role_chunk and has_text_chunk and has_finish_reason and has_done):
                fail_msg(
                    "F4",
                    "Gateway stream chunk protocol",
                    f"role={has_role_chunk}, text={has_text_chunk}, finish={has_finish_reason}, done={has_done}. Raw lines: {raw_lines}",
                )
        return True

    await run_gateway_with_hard_browser_restarts("F4", "Gateway stream", f4_workflow)
    pass_msg("F4", "Gateway stream validated: role chunk, text chunks, finish_reason, [DONE]")

    print(f"\n{GREEN}{BOLD}=== ALL GOLDEN BASELINE CHECKS PASSED (F1 - F5) ==={RESET}\n")


if __name__ == "__main__":
    with free_anonymous_gateway_lock():
        asyncio.run(run_baseline_checks())
PYCHECK

echo "Verification complete. Logs saved to: ${LOG_FILE}"
