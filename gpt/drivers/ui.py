from __future__ import annotations

import asyncio
import time
import uuid
from typing import Literal, cast
from urllib.parse import urlsplit

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from gpt.drivers.base import EventCallback
from gpt.state import (
    AuthRequired,
    ConversationNotFound,
    GenerationTimeout,
    ModelUnavailable,
    RateLimited,
    UIChanged,
)
from gpt.types import (
    ModelInfo,
    ResponseCompleted,
    ResponseDelta,
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
MODEL_PICKER_SELECTORS = (
    "button[data-testid='model-switcher-dropdown-button']",
    "button[aria-label*='Model selector']",
    "button[aria-haspopup='menu'][data-testid*='model']",
)
ASSISTANT_TURN_SELECTORS = (
    "[data-message-author-role='assistant']",
    "article[data-testid*='conversation-turn']:has([data-message-author-role='assistant'])",
    "section[data-testid*='conversation-turn']:has(.agent-turn)",
)
ALL_TURN_SELECTOR = "[data-message-author-role='user'], [data-message-author-role='assistant']"


def _normalise_label(value: str) -> str:
    return " ".join(value.casefold().split())


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
        rate_limit = await self._first_visible(
            (
                "#modal-no-auth-rate-limit",
                "[data-testid='modal-no-auth-rate-limit']",
                "[role='dialog']:has-text('limit')",
            )
        )
        if rate_limit is not None:
            raise RateLimited("ChatGPT Web reports that the current usage limit was reached.")

    async def auth_status(self) -> str:
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
            composer = await self._first_visible(COMPOSER_SELECTORS, 500)
            if composer is not None:
                try:
                    editable = await composer.is_editable(timeout=250)
                    await asyncio.sleep(0.25)
                    if editable and await composer.is_visible() and await composer.is_editable():
                        return composer
                except Exception:
                    pass
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
        picker = await self._first_visible(MODEL_PICKER_SELECTORS, 1_500)
        if picker is None:
            return []
        selected_label = (await picker.inner_text()).strip()
        await picker.click()
        try:
            options = self.page.locator(
                "[role='menu'] [role='menuitem'], [role='listbox'] [role='option'], "
                "[data-radix-menu-content] [role='menuitemradio']"
            )
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
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    checked = await option.get_attribute("aria-checked") == "true"
                    discovered.append(
                        ModelInfo(
                            # Menu entries are real selectable model ids.  The
                            # picker button itself may only say "ChatGPT" and
                            # is not necessarily an option in the menu.
                            id=label,
                            label=label,
                            selected=checked or key == _normalise_label(selected_label),
                            source="ui",
                        )
                    )
                except Exception:
                    continue
            if not discovered and selected_label:
                # Preserve the useful display label without advertising it as
                # selectable.  ``chatgpt-web`` means "keep the current/default
                # UI model" and is always accepted by the gateway.
                discovered.append(
                    ModelInfo(
                        id="chatgpt-web",
                        label=selected_label,
                        selected=True,
                        source="ui",
                    )
                )
            return discovered
        finally:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass

    async def select_model(self, model: str) -> ModelInfo:
        picker = await self._first_visible(MODEL_PICKER_SELECTORS, 2_000)
        if picker is None:
            raise ModelUnavailable("This UI does not expose a model picker.")
        await picker.click()
        target = _normalise_label(model)
        options = self.page.locator(
            "[role='menu'] [role='menuitem'], [role='listbox'] [role='option'], "
            "[data-radix-menu-content] [role='menuitemradio']"
        )
        try:
            for index in range(await options.count()):
                option = options.nth(index)
                label = (await option.inner_text()).strip().split("\n", 1)[0].strip()
                if _normalise_label(label) == target:
                    await option.click()
                    return ModelInfo(id=None, label=label, selected=True, source="ui")
        finally:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
        raise ModelUnavailable(f"Model is not available in the current UI: {model}")

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
        composer = await self.get_composer()
        try:
            await composer.click()
            await composer.fill(req.text, timeout=5_000)
        except PlaywrightTimeoutError:
            await self._raise_known_page_error()
            composer = await self.get_composer()
            await composer.click()
            await composer.fill(req.text, timeout=5_000)

        send_button = await self.get_send_button()
        if send_button is not None and await send_button.is_enabled(timeout=1_000):
            await send_button.click()
        else:
            await composer.press("Enter")

        await emit(ResponseStarted(turn_id=turn_id, model=req.model.label if req.model else None))
        deadline = started_at + req.timeout_seconds
        last_text = ""
        last_change = time.monotonic()
        generation_seen = False
        response_seen = False

        while time.monotonic() < deadline:
            await self._raise_known_page_error()
            stop_button = await self.get_stop_button()
            generation_seen = generation_seen or stop_button is not None
            current_count = await self._assistant_count()
            response_seen = response_seen or current_count > before_count
            current_text = await self._extract_latest_response() if response_seen else ""

            if current_text and current_text != last_text:
                revision = bool(last_text and not current_text.startswith(last_text))
                delta = current_text[len(last_text) :] if not revision else ""
                last_text = current_text
                last_change = time.monotonic()
                if delta or revision:
                    await emit(
                        ResponseDelta(
                            text=delta,
                            accumulated_text=current_text,
                            revision=revision,
                        )
                    )

            composer_usable = await self._composer_usable()
            quiet = time.monotonic() - last_change >= self.stable_grace
            if response_seen and last_text and stop_button is None and composer_usable and quiet:
                break
            await asyncio.sleep(self.poll_interval)
        else:
            raise GenerationTimeout(
                f"No completed assistant turn after {req.timeout_seconds:.1f}s "
                f"(generation_seen={generation_seen}, response_seen={response_seen})."
            )

        result = TurnResult(
            turn_id=turn_id,
            conversation_id=self.conversation_id(),
            text=last_text,
            model=req.model.label if req.model else None,
            duration_ms=int((time.monotonic() - started_at) * 1_000),
        )
        await emit(
            ResponseCompleted(
                turn_id=turn_id,
                text=last_text,
                model=result.model,
                conversation_id=result.conversation_id,
            )
        )
        return result

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
                if count:
                    text = (await nodes.nth(count - 1).inner_text()).strip()
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
