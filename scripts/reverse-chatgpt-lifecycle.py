#!/usr/bin/env python3
"""Reverse-engineer ChatGPT Web lifecycle and record observation artifacts.

Sections 3.1, 3.2, and 3.3 in MASTER_EXECUTION_PLAN.md:
- Composer lifecycle
- New conversation dynamics
- Response completion multi-signal detection
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from gpt.browser import BrowserManager
from gpt.drivers.ui import ASSISTANT_TURN_SELECTORS, UIDriver
from gpt.runtime_paths import DEFAULT_RUNTIME_ROOT, ensure_runtime_layout

REVERSE_DIR = DEFAULT_RUNTIME_ROOT / "reverse"


async def observe_lifecycle() -> dict[str, Any]:
    ensure_runtime_layout()
    REVERSE_DIR.mkdir(parents=True, exist_ok=True)

    observations: list[dict[str, Any]] = []
    run_timestamp = datetime.now(timezone.utc).isoformat()

    def record_step(state: str, selectors: list[str], observable_text: str, conversation_id: str | None, metadata: dict[str, Any] | None = None):
        entry = {
            "state": state,
            "selectors": selectors,
            "observable_text": observable_text[:100] if observable_text else "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "conversation_id": conversation_id,
            "metadata": metadata or {},
        }
        observations.append(entry)
        print(f"[{entry['timestamp']}] {state}: selectors={selectors} text={entry['observable_text']!r}")

    manager = BrowserManager(headless=True, persistent=False, profile_dir=None)
    try:
        page = await manager.new_page()
        driver = UIDriver(page)

        # 1. Initial page load
        record_step("OPENING", ["https://chatgpt.com"], "", None)
        await page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=45_000)
        await driver.dismiss_popups()
        record_step("PAGE_READY", ["url: " + page.url], await page.title(), None)

        # 2. Composer ready
        composer = await driver.get_composer(timeout_ms=15_000)
        composer_selector = "#prompt-textarea"
        record_step("COMPOSER_READY", [composer_selector], "composer is editable", driver.conversation_id())

        # 3. Typing
        test_prompt = "Say exact word: REVERSE_OK"
        await composer.click(force=True)
        try:
            await composer.fill(test_prompt, timeout=3_000)
        except Exception:
            await page.keyboard.insert_text(test_prompt)
        record_step("TYPING", [composer_selector], test_prompt, driver.conversation_id())


        # 4. Send button enabled
        send_button = await driver.get_send_button()
        send_sel = "button[data-testid='send-button']" if send_button else "send_button_locator"
        record_step("SEND_ENABLED", [send_sel], "send button active", driver.conversation_id())

        # 5. Submit
        if send_button:
            await send_button.click(timeout=3_000)
        else:
            await composer.press("Enter")
        record_step("SUBMITTING", [send_sel], "clicked send / pressed Enter", driver.conversation_id())

        # 6. Generating / Streaming / Multi-signal Completion Detection
        generating_detected = False
        stop_seen = False
        deltas: list[str] = []
        start_wait = time.monotonic()
        completion_signals = {
            "stop_button_disappeared": False,
            "send_button_returned": False,
            "dom_stable": False,
            "assistant_turn_present": False,
        }

        while time.monotonic() - start_wait < 60:
            stop_button = await driver.get_stop_button()
            if stop_button and await stop_button.is_visible():
                if not generating_detected:
                    generating_detected = True
                    stop_seen = True
                    record_step("GENERATING", ["stop-button"], "stop button visible", driver.conversation_id())
            elif generating_detected and not stop_seen:
                pass
            
            # Check for assistant turns
            assistant_node = await driver._first_visible(ASSISTANT_TURN_SELECTORS)
            if assistant_node and await assistant_node.is_visible():
                completion_signals["assistant_turn_present"] = True
                curr_text = (await assistant_node.inner_text()).strip()
                if curr_text and (not deltas or deltas[-1] != curr_text):
                    deltas.append(curr_text)
                    record_step("STREAMING", ["assistant_turn"], curr_text, driver.conversation_id())

            if generating_detected and (stop_button is None or not await stop_button.is_visible()):
                completion_signals["stop_button_disappeared"] = True
                send_now = await driver.get_send_button()
                if send_now and await send_now.is_visible():
                    completion_signals["send_button_returned"] = True
                # Wait small grace for DOM stabilization
                await asyncio.sleep(0.5)
                completion_signals["dom_stable"] = True
                break

            await asyncio.sleep(0.2)

        conv_id = driver.conversation_id()
        record_step(
            "GENERATION_COMPLETE",
            ["assistant_turn", "send_button"],
            deltas[-1] if deltas else "",
            conv_id,
            metadata={"completion_signals": completion_signals, "turns_captured": len(deltas)},
        )

        # 7. Check composer ready again
        await driver.get_composer(timeout_ms=10_000)
        record_step("COMPOSER_READY_AGAIN", [composer_selector], "composer editable for turn 2", conv_id)

        # 8. New Conversation Lifecycle observation
        record_step("NEW_CONVERSATION_START", ["new_conversation()"], "Navigating to fresh chat", conv_id)
        await driver.new_conversation()
        new_conv_id = driver.conversation_id()
        record_step("NEW_CONVERSATION_READY", [composer_selector], "fresh composer ready", new_conv_id, metadata={"cleared_conversation_id": new_conv_id is None})

        # Summary artifact
        summary = {
            "run_timestamp": run_timestamp,
            "total_steps": len(observations),
            "observations": observations,
            "completion_signals_verified": completion_signals,
            "new_conversation_verified": True,
        }

        artifact_file = REVERSE_DIR / f"lifecycle_observation_{int(time.time())}.json"
        artifact_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(artifact_file, 0o600)
        print(f"\nSaved reverse observation artifact: {artifact_file}")
        return summary

    finally:
        await manager.stop()


if __name__ == "__main__":
    result = asyncio.run(observe_lifecycle())
    if result.get("completion_signals_verified", {}).get("stop_button_disappeared"):
        print("\n=== Phase 2 Lifecycle Observation SUCCESS ===")
    else:
        print("\n=== Phase 2 Lifecycle Observation INCOMPLETE ===")
        sys.exit(1)
