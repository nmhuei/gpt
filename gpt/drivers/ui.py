from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from playwright.async_api import Locator, Page

from gpt.drivers.base import EventCallback
from gpt.state import (
    AuthRequired,
    ConversationNotFound,
    GenerationTimeout,
    ModelUnavailable,
    RateLimited,
    UIChanged,
    WebChatError,
)
from gpt.streaming import MutableTextAccumulator
from gpt.types import (
    CapabilitySnapshot,
    ModelInfo,
    RequestSubmitted,
    ResponseCompleted,
    ResponseStarted,
    SendRequest,
    Turn,
    TurnResult,
)

COMPOSER_SELECTORS = (
    "#prompt-textarea",
    "[contenteditable='true'][data-virtualkeyboard='true']",
    "div[contenteditable='true'][role='textbox']",
    "textarea[aria-label*='Chat']",
    "textarea[placeholder*='Message']",
    "textarea[placeholder*='Ask']",
)
SEND_SELECTORS = (
    "button[data-testid='send-button']",
    "button[aria-label*='Send prompt']",
    "button[aria-label*='Send message']",
    "button[aria-label='Send']",
)
STOP_SELECTORS = (
    "button[data-testid='stop-button']",
    "button[aria-label*='Stop streaming']",
    "button[aria-label*='Stop generating']",
    "button[aria-label='Stop']",
)
AUTH_WALL_SELECTORS = (
    "[data-testid='login-button']",
    "a[href*='/auth/login']",
    "a[href*='/log-in-or-create-account']",
    "button:has-text('Log in')",
    "button:has-text('Sign in')",
    "[role='dialog']:has-text('Log in to continue')",
    "[role='dialog']:has-text('Sign in to continue')",
)
RATE_LIMIT_SELECTORS = (
    "#modal-no-auth-rate-limit",
    "[data-testid='modal-no-auth-rate-limit']",
    "[data-testid*='rate-limit']",
    "[data-testid*='usage-limit']",
    "[role='alert']:has-text('Too many requests')",
    "[role='dialog']:has-text('usage limit')",
    "[role='dialog']:has-text('rate limit')",
    "[role='dialog']:has-text('reached your limit')",
    "[role='dialog']:has-text('reached our limit')",
    "[role='dialog']:has-text('Too many requests')",
    "[role='dialog']:has-text('try again later')",
)
MODEL_PICKER_SELECTORS = (
    "[data-testid*='model-picker']",
    "[data-testid*='model-switcher']",
    "form button:has-text('High')",
    "form button:has-text('Medium')",
    "form button:has-text('Low')",
    "form button:has-text('Instant')",
    "form button:has-text('Standard')",
    "form button:has-text('Max')",
    "form button:has-text('GPT')",
    "form button:has-text('o3')",
    "form button:has-text('o1')",
    "button[data-testid='model-switcher-dropdown-button']",
    "button[aria-label*='Model selector']",
    "button[aria-haspopup='menu'][data-testid*='model']",
    "button:has-text('ChatGPT')",
)
ASSISTANT_TURN_SELECTORS = (
    "[data-message-author-role='assistant']",
    "article[data-testid*='conversation-turn']:has([data-message-author-role='assistant'])",
    "div[data-message-author-role='assistant']",
    "section[data-testid*='conversation-turn']:has(.agent-turn)",
    "div.agent-turn",
    "div[class*='agent-turn']",
    "div[class*='markdown']",
    "div.markdown",
)
ALL_TURN_SELECTOR = "[data-message-author-role='user'], [data-message-author-role='assistant'], div.user-turn, div.agent-turn"


def _normalise_label(value: str) -> str:
    return " ".join(value.casefold().split())


def _model_matches(opt_norm: str, target: str) -> bool:
    if opt_norm == target:
        return True
    if target in {"5.5", "gpt-5.5", "gpt 5.5", "gpt-5.5-thinking", "5.5 thinking"} and ("5.5" in opt_norm or "gpt 5.5" in opt_norm or "gpt-5.5" in opt_norm):
        return True
    if target in {"5.6", "gpt-5.6", "gpt 5.6", "sol", "gpt-5.6-sol", "5.6 sol"} and ("5.6" in opt_norm or "sol" in opt_norm):
        return True
    if target in {"o3", "o3-mini", "o3 mini"} and "o3" in opt_norm:
        return True
    if target in {"o1", "o1-mini", "o1-preview"} and "o1" in opt_norm:
        return True
    return bool(target in opt_norm or opt_norm in target)


class UIDriver:
    """Semantic ChatGPT UI driver and reliable protocol fallback.

    CSS classes are deliberately not used as primary selectors. A deployment that
    removes the required roles/ARIA/test ids fails explicitly with ``UIChanged``.
    """

    def __init__(self, page: Page, poll_interval: float = 0.25, stable_grace: float = 0.9):
        self.page = page
        self.poll_interval = poll_interval
        self.stable_grace = stable_grace

    async def _first_visible(
        self, selectors: tuple[str, ...], timeout_ms: int = 0
    ) -> Locator | None:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for selector in selectors:
                locator = self.page.locator(selector).first
                try:
                    if await locator.is_visible(timeout=min(300, max(timeout_ms, 1))):
                        return locator
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))

    async def dismiss_popups(self) -> None:
        selectors = (
            "button:has-text('Stay logged out')",
            "button:has-text('Dismiss')",
            "button:has-text('Accept all')",
            "button:has-text('Got it')",
            "[role='dialog'] button[aria-label='Close']",
        )
        for selector in selectors:
            try:
                button = self.page.locator(selector).first
                if await button.is_visible(timeout=250):
                    await button.click(timeout=1_000)
            except Exception:
                continue

    async def _raise_known_page_error(self) -> None:
        url = str(getattr(self.page, "url", ""))
        if "auth.openai.com" in url or "/log-in-or-create-account" in url:
            raise AuthRequired("ChatGPT redirected to a login wall; log in manually in the persistent profile.")

        rate_limit = await self._first_visible(RATE_LIMIT_SELECTORS)

        if rate_limit is not None:
            raise RateLimited("ChatGPT Web reports that the current usage limit was reached.")

        # An anonymous landing page may expose a login button beside a usable
        # composer.  It becomes an authentication wall only when composing is
        # unavailable, which lets callers fail immediately instead of waiting
        # for the generation timeout.
        login = await self._first_visible(AUTH_WALL_SELECTORS)
        if login is not None and await self._first_visible(COMPOSER_SELECTORS) is None:
            raise AuthRequired("ChatGPT login is required; log in manually in the persistent profile.")

        retry_btn = await self._first_visible(
            (
                "button:has-text('Retry')",
                "button:has-text('Thử lại')",
                "[data-testid*='retry-button']",
            )
        )
        if retry_btn is not None:
            err_node = self.page.locator(
                "div:has-text('Something went wrong'), [class*='text-red'], [class*='bg-red']"
            ).first
            if await err_node.is_visible(timeout=200):
                raise WebChatError("ChatGPT upstream error: 'Something went wrong'.")

    async def auth_status(self) -> str:
        if "auth.openai.com" in str(getattr(self.page, "url", "")):
            return "required"
        composer = await self._first_visible(COMPOSER_SELECTORS)
        login = await self._first_visible(
            ("[data-testid='login-button']", "a[href*='/auth/login']", "button:has-text('Log in')")
        )

        account_menu = await self._first_visible(
            (
                "[data-testid='accounts-profile-button']",
                "button[aria-label='Open profile menu']",
                "button[aria-label*='Account menu']",
                "button[aria-label*='Profile menu']",
            )
        )
        # Login UI wins over generic profile/menu buttons: anonymous variants
        # can render an "Open profile menu" control alongside Log in.
        if composer is not None and login is not None:
            return "anonymous"
        if composer is not None and account_menu is not None:
            return "authenticated"
        if login is not None:
            return "required"
        # ChatGPT often hydrates the composer before its auth controls.  Treat
        # that transient state as unknown instead of falsely persisting an
        # anonymous profile as authenticated.
        return "blocked"

    async def get_composer(self, timeout_ms: int = 15_000) -> Locator:
        deadline = time.monotonic() + timeout_ms / 1_000
        while time.monotonic() < deadline:
            await self._raise_known_page_error()
            composer = await self._first_visible(COMPOSER_SELECTORS, 500)
            if composer is not None:
                try:
                    editable = await composer.is_editable(timeout=250)
                    await asyncio.sleep(0.25)
                    if editable and await composer.is_visible() and await composer.is_editable():
                        return composer
                except Exception:
                    pass
        await self._raise_known_page_error()
        if await self.auth_status() == "required":
            raise AuthRequired("ChatGPT login is required; log in manually in the persistent profile.")
        raise UIChanged("No semantic ChatGPT composer was found.")

    async def get_send_button(self) -> Locator | None:
        return await self._first_visible(SEND_SELECTORS)

    async def get_stop_button(self) -> Locator | None:
        return await self._first_visible(STOP_SELECTORS)

    async def models(self) -> list[ModelInfo]:
        return await self.list_models()

    async def list_models(self) -> list[ModelInfo]:
        picker = await self._first_visible(MODEL_PICKER_SELECTORS, 3_000)
        if picker is None:
            return [
                ModelInfo(
                    id="chatgpt-web",
                    label="ChatGPT Web default",
                    selected=True,
                    available=True,
                    source="ui",
                    reasoning_efforts=[],
                )
            ]
        selected_label = (await picker.inner_text()).strip()
        await picker.click(force=True)
        await asyncio.sleep(0.2)
        try:
            # Check for Model submenu trigger: [role='menuitem']:has-text('Model')
            model_trigger = self.page.locator(
                "[role='menuitem']:has-text('Model'), [role='menuitem'] span:has-text('Model'), button:has-text('Model')"
            ).first
            if not await model_trigger.is_visible(timeout=500):
                adv_toggle = self.page.locator(
                    "[role='menuitem']:has-text('Advanced'), [aria-label*='advanced options'], button:has-text('Advanced')"
                ).first
                if await adv_toggle.is_visible(timeout=500):
                    await adv_toggle.click(force=True)
                    await asyncio.sleep(0.3)
            if await model_trigger.is_visible(timeout=1000):
                await model_trigger.click(force=True)
                await asyncio.sleep(0.3)

            options_selector = (
                "[role='menu'] [role='menuitemradio'], [role='menu'] [role='menuitem'], "
                "[role='listbox'] [role='option'], [data-radix-menu-content] [role='menuitemradio'], "
                "[data-radix-menu-content] [role='menuitem'], [data-radix-popper-content-wrapper] [role='menuitem']"
            )
            options = self.page.locator(options_selector)
            count = await options.count()
            discovered: list[ModelInfo] = []
            seen: set[str] = set()
            for index in range(count):
                option = options.nth(index)
                try:
                    if not await option.is_visible(timeout=250):
                        continue
                    label = (await option.inner_text()).strip().split("\n", 1)[0].strip()
                    key = _normalise_label(label)
                    if not key or key in seen or key in {"advanced", "model", "effort", "help"}:
                        continue
                    seen.add(key)
                    checked = (
                        await option.get_attribute("aria-checked") == "true"
                        or await option.get_attribute("data-state") == "checked"
                        or await option.locator("svg, [class*='check']").count() > 0
                    )
                    discovered.append(
                        ModelInfo(
                            id=label,
                            label=label,
                            selected=checked or key == _normalise_label(selected_label),
                            source="ui",
                            reasoning_efforts=[],
                        )
                    )
                except Exception:
                    continue
            if not discovered and selected_label:
                discovered.append(
                    ModelInfo(
                        id="chatgpt-web",
                        label=selected_label,
                        selected=True,
                        source="ui",
                        reasoning_efforts=[],
                    )
                )
            return discovered
        finally:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass

    async def _discover_reasoning_effort_state(
        self,
    ) -> tuple[list[str], str | None]:
        picker = await self._first_visible(MODEL_PICKER_SELECTORS, 2_000)
        if picker is None:
            return [], None
        await picker.click(force=True)
        await asyncio.sleep(0.2)
        discovered: list[str] = []
        selected: str | None = None
        seen: set[str] = set()
        try:
            effort_trigger = self.page.locator(
                "[role='menuitem']:has-text('Effort'), [role='menuitem'] span:has-text('Effort'), button:has-text('Effort')"
            ).first
            if not await effort_trigger.is_visible(timeout=500):
                advanced = self.page.locator(
                    "[role='menuitem']:has-text('Advanced'), [aria-label*='advanced options'], button:has-text('Advanced')"
                ).first
                if await advanced.is_visible(timeout=500):
                    await advanced.click(force=True)
                    await asyncio.sleep(0.25)
            if not await effort_trigger.is_visible(timeout=750):
                return [], None
            await effort_trigger.click(force=True)
            await asyncio.sleep(0.25)
            options = self.page.locator(
                "[role='menu'] [role='menuitemradio'], [role='listbox'] [role='option'], "
                "[data-radix-menu-content] [role='menuitemradio'], "
                "[data-radix-popper-content-wrapper] [role='menuitemradio']"
            )
            for index in range(await options.count()):
                option = options.nth(index)
                try:
                    if not await option.is_visible(timeout=200):
                        continue
                    label = (await option.inner_text()).strip().split("\n", 1)[0].strip()
                    checked = (
                        await option.get_attribute("aria-checked") == "true"
                        or await option.get_attribute("data-state") == "checked"
                    )
                except Exception:
                    continue
                key = _normalise_label(label)
                if not key or key in seen:
                    continue
                seen.add(key)
                discovered.append(label)
                if checked:
                    selected = label
            return discovered, selected
        finally:
            try:
                await self.page.keyboard.press("Escape")
                await self.page.keyboard.press("Escape")
            except Exception:
                pass

    async def list_reasoning_efforts(self) -> list[str]:
        """Discover visible reasoning-effort options without inferring account tier."""
        efforts, _selected = await self._discover_reasoning_effort_state()
        return efforts

    async def capabilities(self) -> CapabilitySnapshot:
        auth_status = await self.auth_status()
        picker = await self._first_visible(MODEL_PICKER_SELECTORS, 1_000)
        has_picker = picker is not None
        models = await self.list_models()
        if has_picker:
            efforts, selected_effort = await self._discover_reasoning_effort_state()
        else:
            efforts, selected_effort = [], None
        selected = next((model for model in models if model.selected), None)
        if selected is not None:
            selected.reasoning_efforts = list(efforts)
            selected.selected_effort = selected_effort
        return CapabilitySnapshot(
            auth_status=auth_status,
            has_model_picker=has_picker,
            models=models,
            reasoning_efforts=efforts,
            selected_model=selected.label if selected else None,
            selected_effort=selected_effort,
            protocol_send_eligible=False,
        )

    async def select_model(self, model: str) -> ModelInfo:
        target = _normalise_label(model)
        if target in {"chatgpt-web", "default"}:
            return ModelInfo(
                id="chatgpt-web",
                label="ChatGPT Web default",
                selected=True,
                available=True,
                source="ui",
                reasoning_efforts=[],
            )

        picker = await self._first_visible(MODEL_PICKER_SELECTORS, 3_000)
        if picker is None:
            raise ModelUnavailable(
                f"Model '{model}' cannot be selected because this UI exposes no model picker."
            )

        # Map common names to exact ChatGPT URL model slugs for reliable selection
        slug_map = {
            "5.5": "gpt-5-5-thinking",
            "gpt-5.5": "gpt-5-5-thinking",
            "thinking 5.5": "gpt-5-5-thinking",
            "5.5 thinking": "gpt-5-5-thinking",
            "5.5 high": "gpt-5-5-thinking",
            "gpt-5-5-thinking": "gpt-5-5-thinking",
            "5.6": "gpt-5-6-thinking",
            "gpt-5.6": "gpt-5-6-thinking",
            "thinking 5.6": "gpt-5-6-thinking",
            "5.6 thinking": "gpt-5-6-thinking",
            "5.6 sol": "gpt-5-6",
            "gpt-5-6": "gpt-5-6",
            "o3": "o3",
            "gpt-4o": "gpt-4o",
        }
        slug = slug_map.get(target)
        if slug:
            current_url = getattr(self.page, "url", "")
            if isinstance(current_url, str) and f"model={slug}" not in current_url:
                goto_fn = getattr(self.page, "goto", None)
                if callable(goto_fn):
                    ret = goto_fn(f"https://chatgpt.com/?model={slug}", wait_until="domcontentloaded")
                    if asyncio.iscoroutine(ret) or hasattr(ret, "__await__"):
                        await ret
                await asyncio.sleep(0.5)

            # If High effort is specifically requested, ensure reasoning effort is set to High
            if "high" in target:
                try:
                    await self.select_reasoning_effort("high")
                except Exception:
                    pass
            return ModelInfo(id=slug, label=model, selected=True, source="url_protocol")
        await picker.click(force=True)
        await asyncio.sleep(0.2)
        try:
            # Click 'Model' submenu trigger (as seen in screenshots: Model GPT-5.6 Sol >)
            model_trigger = self.page.locator(
                "[role='menuitem']:has-text('Model'), [role='menuitem'] span:has-text('Model'), button:has-text('Model')"
            ).first
            if not await model_trigger.is_visible(timeout=500):
                adv_toggle = self.page.locator(
                    "[role='menuitem']:has-text('Advanced'), [aria-label*='advanced options'], button:has-text('Advanced')"
                ).first
                if await adv_toggle.is_visible(timeout=500):
                    await adv_toggle.click(force=True)
                    await asyncio.sleep(0.3)
            if await model_trigger.is_visible(timeout=1000):
                await model_trigger.click(force=True)
                await asyncio.sleep(0.3)

            options_selector = (
                "[role='menu'] [role='menuitemradio'], [role='menu'] [role='menuitem'], "
                "[role='listbox'] [role='option'], [data-radix-menu-content] [role='menuitemradio'], "
                "[data-radix-menu-content] [role='menuitem'], [data-radix-popper-content-wrapper] [role='menuitem'], "
                "[data-radix-popper-content-wrapper] button"
            )
            options = self.page.locator(options_selector)
            count = await options.count()
            for index in range(count):
                option = options.nth(index)
                label = (await option.inner_text()).strip().split("\n", 1)[0].strip()
                opt_norm = _normalise_label(label)
                if _model_matches(opt_norm, target):
                    await option.click(force=True)
                    await asyncio.sleep(0.2)
                    verified = False
                    try:
                        verified = (
                            await option.get_attribute("aria-checked") == "true"
                            or await option.get_attribute("data-state") == "checked"
                        )
                    except Exception:
                        verified = False
                    if not verified:
                        try:
                            current_picker = await self._first_visible(
                                MODEL_PICKER_SELECTORS, 1_000
                            )
                            if current_picker is not None:
                                observed = _normalise_label(
                                    (await current_picker.inner_text()).strip().split("\n", 1)[0]
                                )
                                verified = _model_matches(observed, target)
                        except Exception:
                            verified = False
                    if not verified:
                        raise UIChanged(
                            f"Model selection click did not read back as active: {model}"
                        )
                    return ModelInfo(id=label, label=label, selected=True, source="ui")
        finally:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
        raise ModelUnavailable(f"Model is not available in the current UI: {model}")

    async def select_reasoning_effort(self, effort: str) -> str:
        """Select an exact visible effort option from the Effort submenu: Instant, Medium, High."""
        target = _normalise_label(effort)
        if target not in {"instant", "low", "medium", "high", "max"}:
            raise ModelUnavailable(f"Unsupported reasoning effort: {effort}")

        picker = await self._first_visible(MODEL_PICKER_SELECTORS, 3_000)
        if picker is None:
            raise ModelUnavailable("This UI exposes no reasoning effort control.")

        await picker.click(force=True)
        await asyncio.sleep(0.2)
        try:
            # Direct slider position click (Instant=0.15, Medium=0.50, High=0.88)
            slider_ctrl = self.page.locator('[aria-label="Power"], [class*="SliderControl"]').first
            if await slider_ctrl.is_visible(timeout=1000):
                box = await slider_ctrl.bounding_box()
                if box:
                    ratio = 0.88 if target in {"high", "max"} else (0.50 if target in {"medium", "standard"} else 0.15)
                    target_x = box["x"] + box["width"] * ratio
                    target_y = box["y"] + box["height"] / 2
                    mouse = getattr(self.page, "mouse", None)
                    if mouse and hasattr(mouse, "click"):
                        m_click = mouse.click(target_x, target_y)
                        if asyncio.iscoroutine(m_click) or hasattr(m_click, "__await__"):
                            await m_click
                        await asyncio.sleep(0.3)
                        esc = getattr(self.page.keyboard, "press", None)
                        if callable(esc):
                            e_res = esc("Escape")
                            if asyncio.iscoroutine(e_res) or hasattr(e_res, "__await__"):
                                await e_res
                        return target

            # Click 'Effort' submenu trigger (as seen in screenshots: Effort Medium >)
            effort_trigger = self.page.locator(
                "[role='menuitem']:has-text('Effort'), [role='menuitem'] span:has-text('Effort'), button:has-text('Effort')"
            ).first
            if not await effort_trigger.is_visible(timeout=500):
                adv_toggle = self.page.locator(
                    "[role='menuitem']:has-text('Advanced'), [aria-label*='advanced options'], button:has-text('Advanced')"
                ).first
                if await adv_toggle.is_visible(timeout=500):
                    await adv_toggle.click(force=True)
                    await asyncio.sleep(0.3)
            if await effort_trigger.is_visible(timeout=1000):
                hover_fn = getattr(effort_trigger, "hover", None)
                if callable(hover_fn):
                    h_res = hover_fn()
                    if asyncio.iscoroutine(h_res) or hasattr(h_res, "__await__"):
                        await h_res
                click_fn = getattr(effort_trigger, "click", None)
                if callable(click_fn):
                    c_res = click_fn(force=True)
                    if asyncio.iscoroutine(c_res) or hasattr(c_res, "__await__"):
                        await c_res
                await asyncio.sleep(0.2)

            # Use complete DOM dispatch sequence (mousedown + mouseup + click) for Radix UI
            eval_fn = getattr(self.page, "evaluate", None)
            if callable(eval_fn):
                try:
                    res = eval_fn(f"""
                        () => {{
                            const items = Array.from(document.querySelectorAll('[role="menuitemradio"], [role="menuitem"]'));
                            const match = items.find(el => el.innerText && el.innerText.trim().toLowerCase() === '{target}');
                            if (match) {{
                                match.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true }}));
                                match.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true }}));
                                match.dispatchEvent(new MouseEvent('click', {{ bubbles: true }}));
                                return true;
                            }}
                            return false;
                        }}
                    """)
                    if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                        await res
                    await asyncio.sleep(0.3)
                    current_picker = await self._first_visible(MODEL_PICKER_SELECTORS, 1_000)
                    if current_picker is not None:
                        observed = _normalise_label(await current_picker.inner_text())
                        if (
                            target in observed
                            or (target in {"instant", "low"} and any(item in observed for item in ("instant", "low")))
                            or (target in {"high", "max"} and any(item in observed for item in ("high", "max")))
                        ):
                            return target
                except Exception:
                    pass

            options_selector = (
                "[role='menu'] [role='menuitemradio'], [role='menu'] [role='menuitem'], "
                "[data-radix-menu-content] [role='menuitemradio'], [data-radix-menu-content] [role='menuitem'], "
                "[data-radix-popper-content-wrapper] [role='menuitem'], [data-radix-popper-content-wrapper] button"
            )
            options = self.page.locator(options_selector)
            count = await options.count()
            for index in range(count):
                option = options.nth(index)
                label = (await option.inner_text()).strip().split("\n", 1)[0].strip()
                lbl_norm = _normalise_label(label)
                if (lbl_norm == target) or (target in {"instant", "low"} and lbl_norm in {"instant", "low"}) or (target in {"high", "max"} and lbl_norm in {"high", "max"}):
                    await option.click(force=True)
                    await asyncio.sleep(0.2)
                    verified = False
                    try:
                        verified = (
                            await option.get_attribute("aria-checked") == "true"
                            or await option.get_attribute("data-state") == "checked"
                        )
                    except Exception:
                        verified = False
                    if not verified:
                        current_picker = await self._first_visible(MODEL_PICKER_SELECTORS, 1_000)
                        if current_picker is not None:
                            observed = _normalise_label(await current_picker.inner_text())
                            verified = (
                                target in observed
                                or (target in {"instant", "low"} and any(item in observed for item in ("instant", "low")))
                                or (target in {"high", "max"} and any(item in observed for item in ("high", "max")))
                            )
                    if not verified:
                        raise UIChanged(f"Reasoning-effort click did not read back as active: {effort}")
                    return target
        finally:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
        raise ModelUnavailable(f"Reasoning effort is not available in the current UI: {effort}")

    async def setup_default_tier(self) -> tuple[ModelInfo | None, str | None]:
        """Observe the current UI configuration without inferring an account tier.

        Historical versions guessed subscription tier from the presence of a
        model picker and then mutated the UI to a hard-coded model/effort. That
        is unsafe because ChatGPT experiments and account capabilities change.
        The default is now whatever ChatGPT Web currently selected. Explicit
        model/effort overrides are applied only when the caller requests them.
        """
        try:
            models = await self.list_models()
        except Exception:
            return None, None
        selected_model = next((model for model in models if model.selected), None)
        if selected_model is None and len(models) == 1:
            selected_model = models[0]
        return selected_model, selected_model.selected_effort if selected_model else None

    async def new_conversation(self) -> None:
        origin = f"{urlsplit(self.page.url).scheme}://{urlsplit(self.page.url).netloc}"
        if not origin.startswith("http"):
            origin = "https://chatgpt.com"
        await self.page.goto(origin, wait_until="domcontentloaded", timeout=45_000)
        await self.dismiss_popups()
        await self.get_composer()

    async def open_conversation(self, conversation_id: str) -> None:
        if not conversation_id or "/" in conversation_id or "?" in conversation_id:
            raise ConversationNotFound("Invalid conversation id.")
        parts = urlsplit(self.page.url)
        origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else "https://chatgpt.com"
        response = await self.page.goto(
            f"{origin}/c/{conversation_id}", wait_until="domcontentloaded", timeout=45_000
        )
        if response is not None and response.status == 404:
            raise ConversationNotFound(conversation_id)
        await self.dismiss_popups()
        await self.get_composer()
        if self.conversation_id() != conversation_id:
            raise ConversationNotFound(conversation_id)

    async def history(self) -> list[Turn]:
        turns: list[Turn] = []
        locator = self.page.locator(ALL_TURN_SELECTOR)
        for index in range(await locator.count()):
            node = locator.nth(index)
            raw_role = await node.get_attribute("data-message-author-role")
            if raw_role not in {"user", "assistant"}:
                continue
            text = (await node.inner_text()).strip()
            if text:
                role = cast(Literal["user", "assistant"], raw_role)
                turns.append(Turn(turn_id=f"dom_{index}", role=role, text=text))
        return turns

    async def send(
        self,
        request: SendRequest | str | None = None,
        event_callback: EventCallback | None = None,
        *,
        text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> TurnResult:
        if isinstance(request, SendRequest):
            req = request
        else:
            prompt = text if text is not None else request
            if not isinstance(prompt, str):
                raise TypeError("send requires text or SendRequest")
            req = SendRequest(text=prompt, timeout_seconds=timeout_seconds or 120.0)
        if not req.text.strip():
            raise ValueError("Prompt text cannot be empty.")

        async def emit(event) -> None:
            if event_callback is not None:
                result = event_callback(event)
                if asyncio.iscoroutine(result):
                    await result

        turn_id = f"turn_{uuid.uuid4().hex[:10]}"
        started_at = time.monotonic()
        before_count = await self._assistant_count()
        await self.dismiss_popups()
        await self._raise_known_page_error()

        # Handle file attachments if provided
        file_attachment_text = ""
        if req.files:
            file_input = self.page.locator('input[type="file"]').first
            resolved_files: list[str] = []
            for f_path in req.files:
                p = Path(f_path).expanduser().resolve()
                if not p.exists():
                    raise FileNotFoundError(f"Attachment file not found: {f_path}")
                resolved_files.append(str(p))
                try:
                    raw_bytes = p.read_bytes()
                    if len(raw_bytes) <= 100_000 and b"\x00" not in raw_bytes[:1024]:
                        text_content = raw_bytes.decode("utf-8", errors="replace")
                        file_attachment_text += f"\n\n--- ATTACHED FILE: {p.name} ---\n```\n{text_content}\n```"
                except Exception:
                    pass
            if resolved_files and await file_input.count() > 0:
                try:
                    await file_input.set_input_files(resolved_files)
                    await asyncio.sleep(1.5)
                except Exception:
                    pass

        full_prompt = (req.text + file_attachment_text) if file_attachment_text else req.text

        # Ensure model/effort is strictly 5.5 High before sending if 5.5 is selected
        try:
            pill = await self._first_visible(MODEL_PICKER_SELECTORS, 1_000)
            if pill is not None:
                txt = _normalise_label(await pill.inner_text())
                if "5.5" in txt and "high" not in txt:
                    await self.select_reasoning_effort("high")
        except Exception:
            pass

        composer = await self.get_composer()
        try:
            await composer.click(force=True)
            await composer.fill(full_prompt, timeout=3_000)
        except Exception:
            await self._raise_known_page_error()
            try:
                await composer.click(force=True)
                await self.page.keyboard.insert_text(full_prompt)
            except Exception:
                composer = await self.get_composer()
                await composer.click(force=True)
                await self.page.keyboard.insert_text(full_prompt)

        # Attach network response listener for direct SSE stream capture
        network_stream_text = ""
        network_conv_id = None
        network_error: RateLimited | AuthRequired | None = None

        async def _on_network_response(response) -> None:
            nonlocal network_error, network_stream_text, network_conv_id
            url = response.url
            status = getattr(response, "status", None)
            if status == 429:
                network_error = RateLimited("ChatGPT Web request was rate limited.")
                return
            if status in {401, 403}:
                network_error = AuthRequired("ChatGPT authentication is required.")
                return
            if ("backend-api" in url or "backend-anon" in url) and ("conversation" in url):
                try:
                    body = await response.text()
                    for line in body.splitlines():
                        if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                            try:
                                payload = json.loads(line[6:])
                                if payload.get("conversation_id"):
                                    network_conv_id = payload["conversation_id"]
                                parts = payload.get("message", {}).get("content", {}).get("parts")
                                if parts and isinstance(parts, list):
                                    network_stream_text = "".join(parts)
                            except Exception:
                                pass
                except Exception:
                    pass

        self.page.on("response", _on_network_response)

        try:
            send_button = await self.get_send_button()
            if send_button is not None and await send_button.is_enabled(timeout=1_000):
                try:
                    await send_button.click(timeout=2_000)
                except Exception:
                    await composer.press("Enter")
            else:
                await composer.press("Enter")

            await asyncio.sleep(0)
            await emit(
                RequestSubmitted(
                    turn_id=turn_id,
                    conversation_id=self.conversation_id(),
                )
            )
            await emit(ResponseStarted(turn_id=turn_id, model=req.model.label if req.model else None))
            deadline = started_at + req.timeout_seconds
            accumulator = MutableTextAccumulator()
            last_change = time.monotonic()
            generation_seen = False
            response_seen = False

            while time.monotonic() < deadline:
                try:
                    if network_error is not None:
                        raise network_error
                    await self._raise_known_page_error()
                    stop_button = await self.get_stop_button()
                    generation_seen = generation_seen or stop_button is not None
                    current_count = await self._assistant_count()
                    current_text = await self._extract_latest_response()
                    if current_text or network_stream_text or (current_count > before_count):
                        response_seen = True

                    latest_text = network_stream_text or current_text
                    delta = accumulator.update(latest_text) if latest_text else None
                    if delta is not None:
                        last_change = time.monotonic()
                        await emit(delta)

                    composer_usable = await self._composer_usable()
                    quiet = time.monotonic() - last_change >= self.stable_grace
                    if response_seen and accumulator.text and stop_button is None and composer_usable and quiet:
                        break
                except (RateLimited, AuthRequired, UIChanged, WebChatError):
                    raise
                except Exception:
                    await asyncio.sleep(self.poll_interval)
                    continue
                await asyncio.sleep(self.poll_interval)

            else:
                raise GenerationTimeout(
                    f"No completed assistant turn after {req.timeout_seconds:.1f}s "
                    f"(generation_seen={generation_seen}, response_seen={response_seen})."
                )

            final_conv_id = network_conv_id or self.conversation_id()
            result = TurnResult(
                turn_id=turn_id,
                conversation_id=final_conv_id,
                text=accumulator.text,
                model=req.model.label if req.model else None,
                duration_ms=int((time.monotonic() - started_at) * 1_000),
            )
            await emit(
                ResponseCompleted(
                    turn_id=turn_id,
                    text=accumulator.text,
                    model=result.model,
                    conversation_id=result.conversation_id,
                )
            )
            return result
        finally:
            self.page.remove_listener("response", _on_network_response)

    async def _composer_usable(self) -> bool:
        composer = await self._first_visible(COMPOSER_SELECTORS)
        if composer is None:
            return False
        try:
            return await composer.is_enabled(timeout=250)
        except Exception:
            return True

    async def _assistant_count(self) -> int:
        for selector in ASSISTANT_TURN_SELECTORS:
            try:
                count = await self.page.locator(selector).count()
                if count:
                    return count
            except Exception:
                continue
        return 0

    async def _extract_latest_response(self) -> str:
        for selector in ASSISTANT_TURN_SELECTORS:
            try:
                nodes = self.page.locator(selector)
                count = await nodes.count()
                for idx in range(count - 1, -1, -1):
                    node = nodes.nth(idx)
                    if await node.is_visible(timeout=50):
                        text = (await node.inner_text()).strip()
                        if text:
                            return text
            except Exception:
                continue
        return ""


    def conversation_id(self) -> str | None:
        path = urlsplit(self.page.url).path
        if "/c/" not in path:
            return None
        value = path.split("/c/", 1)[1].split("/", 1)[0]
        return value or None

    def _extract_conversation_id_from_url(self) -> str | None:
        return self.conversation_id()
