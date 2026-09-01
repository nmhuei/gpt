from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from gpt.drivers.ui import UIDriver
from gpt.types import ElementFingerprint

_CANDIDATE_RULES = {
    "composer": [
        "#prompt-textarea",
        "div[contenteditable='true']",
        "textarea[data-id='root']",
        "textarea[placeholder*='Message']",
        "div[role='textbox']",
        "textarea",
    ],
    "send_button": [
        "button[data-testid='send-button']",
        "button[aria-label*='Send prompt']",
        "button[aria-label*='Send message']",
        "button[aria-label='Send']",
    ],
    "stop_button": [
        "button[data-testid='stop-button']",
        "button[aria-label*='Stop streaming']",
        "button[aria-label*='Stop generating']",
        "button[aria-label='Stop']",
    ],
    "new_chat": [
        "a[data-testid='navigation-new-chat-button']",
        "a[href='/']",
        "button[aria-label*='New chat']",
        "button:has-text('New chat')",
    ],
    "model_picker": [
        "button[data-testid='model-switcher-dropdown-button']",
        "button[aria-label*='Model selector']",
        "button[aria-haspopup='menu']:has-text('ChatGPT')",
        "button:has-text('ChatGPT')",
    ],
    "login_button": [
        "button[data-testid='login-button']",
        "a[href*='login']",
        "button:has-text('Log in')",
        "a:has-text('Log in')",
    ],
    "turn_messages": [
        "article[data-testid*='conversation-turn']",
        "div[data-message-author-role]",
        "div.agent-turn",
        "div.user-turn",
    ],
}


class DOMProbe:
    """Performs semantic reconnaissance and invariant fingerprinting on ChatGPT DOM."""

    def __init__(self, page: Page):
        self.page = page

    async def probe_element(self, element_type: str) -> ElementFingerprint | None:
        candidates = _CANDIDATE_RULES.get(element_type, [])
        for selector in candidates:
            try:
                locator = self.page.locator(selector).first
                if await locator.is_visible(timeout=500):
                    tag = await locator.evaluate("el => el.tagName.toLowerCase()")
                    role = await locator.get_attribute("role")
                    aria_label = await locator.get_attribute("aria-label")
                    test_id = await locator.get_attribute("data-testid")

                    return ElementFingerprint(
                        name=element_type,
                        role=role,
                        tag=tag,
                        aria_label=aria_label,
                        test_id=test_id,
                        selector_candidates=[selector],
                    )
            except Exception:
                continue
        return None

    async def probe_all(self) -> dict[str, Any]:
        results: dict[str, Any] = {
            "url": self.page.url,
            "title": await self.page.title(),
            "elements": {},
            "auth_status": "unknown",
            "cloudflare_challenge": False,
        }

        # Check for Cloudflare challenge
        content = await self.page.content()
        if (
            "cf-turnstile" in content
            or "Just a moment..." in results["title"]
            or "__cf_chl_" in self.page.url
            or "/cdn-cgi/challenge-platform/" in content
        ):
            results["cloudflare_challenge"] = True

        for elem_name in _CANDIDATE_RULES:
            fp = await self.probe_element(elem_name)
            if fp:
                results["elements"][elem_name] = {
                    "tag": fp.tag,
                    "role": fp.role,
                    "aria_label": fp.aria_label,
                    "test_id": fp.test_id,
                    "matched_selector": fp.selector_candidates[0] if fp.selector_candidates else None,
                }

        # Keep auth classification aligned with the runtime UI driver.
        # A composer alone is not proof of authentication: anonymous ChatGPT
        # can expose a fully usable composer without a visible login button.
        if results["cloudflare_challenge"]:
            results["auth_status"] = "security_challenge"
        else:
            ui_status = await UIDriver(self.page).auth_status()
            results["auth_status"] = {
                "anonymous": "anonymous_free",
                "authenticated": "authenticated",
                "required": "login_required",
                "blocked": "loading_or_blocked",
            }.get(ui_status, "loading_or_blocked")

        return results

    async def get_dom_html(self) -> str:
        return await self.page.content()

    async def get_accessibility_tree(self) -> Any:
        try:
            return {"aria_snapshot": await self.page.locator("body").aria_snapshot()}
        except Exception:
            return {}
