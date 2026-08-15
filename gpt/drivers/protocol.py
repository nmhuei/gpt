from __future__ import annotations

import inspect
from collections.abc import Callable
from urllib.parse import urlsplit

from playwright.async_api import Page

from gpt.drivers.base import EventCallback
from gpt.state import ProtocolChanged
from gpt.types import ModelInfo, ProtocolFingerprint, SendRequest, Turn, TurnResult


class ProtocolDriver:
    """Evidence-gated browser-context protocol driver.

    The project has not captured a verified request contract yet. Consequently
    this driver intentionally reports itself unavailable instead of replaying a
    guessed endpoint. A verified replay implementation can be injected after the
    experiment ledger establishes persistence and completion semantics.
    """

    def __init__(
        self,
        page: Page,
        fingerprint: ProtocolFingerprint | None = None,
        replay: Callable[..., object] | None = None,
    ):
        self.page = page
        self.fingerprint = fingerprint
        self._replay = replay

    @property
    def available(self) -> bool:
        return bool(
            self.fingerprint
            and self.fingerprint.verified
            and len(self.fingerprint.supporting_experiments) >= 2
            and self._replay
        )

    async def probe_protocol_compatibility(self) -> bool:
        if not self.available:
            return False
        try:
            host = urlsplit(self.page.url).hostname or ""
            runtime_ok = await self.page.evaluate(
                "() => typeof window.fetch === 'function' && window.isSecureContext === true"
            )
            return runtime_ok and (host == "chatgpt.com" or host.endswith(".chatgpt.com"))
        except Exception:
            return False

    async def send(
        self,
        request: SendRequest | str,
        event_callback: EventCallback | None = None,
        **legacy: object,
    ) -> TurnResult:
        if not await self.probe_protocol_compatibility():
            raise ProtocolChanged(
                "No verified protocol fingerprint is active; use the UI driver."
            )
        assert self._replay is not None
        result = self._replay(self.page, request, event_callback)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, TurnResult):
            raise ProtocolChanged("Verified replay adapter returned an invalid result.")
        return result

    async def history(self) -> list[Turn]:
        raise ProtocolChanged("Protocol history contract is not verified.")

    async def models(self) -> list[ModelInfo]:
        raise ProtocolChanged("Protocol model discovery contract is not verified.")

    async def select_model(self, model: str) -> ModelInfo:
        raise ProtocolChanged("Protocol model selection contract is not verified.")
