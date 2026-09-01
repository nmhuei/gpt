"""Hybrid wiring for the T4-PERSIST TokenBundle disk cache.

``HybridWorkerFactory.start()`` must hand ``TokenManager`` a ``cache_dir``
derived from the browser profile (same convention as the rest of the
codebase); when no usable profile is known the cache stays disabled and
behaviour matches pre-T4 exactly.
"""

from __future__ import annotations

import pytest

from gpt.transport.hybrid import HybridWorkerFactory

CACHE_NAME = "webgpt-token-cache.json"


class FakeContext:
    async def cookies(self):
        return [
            {"name": "cf_clearance", "value": "clearance"},
            {"name": "oai-device-id", "value": "device-from-cookie"},
        ]


class FakePage:
    def __init__(self) -> None:
        self.context = FakeContext()
        self.goto_calls: list[str] = []

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.goto_calls.append(url)

    async def evaluate(self, script, argument=None):
        if "/api/auth/session" in script:
            return {"user": {"accessToken": "access-token"}}
        if "localStorage.getItem" in script:
            return "device-from-storage"
        raise AssertionError("unexpected page script")


class FakeBrowserManager:
    def __init__(self, profile_dir: object | None = None) -> None:
        self.profile_dir = profile_dir

    async def start(self) -> None:
        return None

    async def new_page(self) -> FakePage:
        return FakePage()

    async def stop(self) -> None:
        return None


class ProfileLessBrowserManager(FakeBrowserManager):
    """Mimics a manager that exposes no ``profile_dir`` at all."""

    def __init__(self) -> None:
        super().__init__()
        del self.profile_dir


def _factory(browser_manager: FakeBrowserManager) -> HybridWorkerFactory:
    return HybridWorkerFactory(
        browser_manager,  # type: ignore[arg-type]
        warm_workers=0,
        allow_local_mock=False,
    )


@pytest.mark.anyio
async def test_start_passes_profile_derived_cache_dir(tmp_path):
    """cache_dir handed to TokenManager == browser profile dir."""
    profile = tmp_path / "profile"
    profile.mkdir()
    factory = _factory(FakeBrowserManager(profile))
    await factory.start()
    try:
        assert factory._token_manager is not None
        assert factory._token_manager._cache_path == profile / CACHE_NAME
    finally:
        await factory.close()


@pytest.mark.anyio
async def test_extract_persists_cache_inside_derived_profile_dir(tmp_path):
    """End-to-end: a successful extract writes the cache under the profile."""
    profile = tmp_path / "profile"
    profile.mkdir()
    factory = _factory(FakeBrowserManager(profile))
    await factory.start()
    try:
        assert (profile / CACHE_NAME).is_file()
    finally:
        await factory.close()


@pytest.mark.anyio
async def test_missing_profile_attribute_disables_cache_without_crash():
    """No profile_dir attribute -> cache disabled, start still succeeds."""
    factory = _factory(ProfileLessBrowserManager())
    await factory.start()
    try:
        assert factory._token_manager is not None
        assert factory._token_manager._cache_path is None
    finally:
        await factory.close()


@pytest.mark.anyio
async def test_unusable_profile_value_falls_back_to_no_cache(tmp_path):
    """A truthy but non-path profile value must not crash the wiring."""
    factory = _factory(FakeBrowserManager(object()))
    await factory.start()
    try:
        assert factory._token_manager is not None
        assert factory._token_manager._cache_path is None
    finally:
        await factory.close()
