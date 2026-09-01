"""T4-lite: disk-persisted TokenBundle cache (webgpt-token-cache.json).

The cache lets a gateway restart serve requests without touching the browser
while the persisted bundle is still within ``refresh_interval``.  The file
holds the live access token, so permission checks are part of the contract.
"""

from __future__ import annotations

import json
import stat
import time

import pytest

from gpt.transport.token_manager import TokenManager

CACHE_NAME = "webgpt-token-cache.json"


class FakeContext:
    def __init__(self) -> None:
        self._cookies = [
            {"name": "cf_clearance", "value": "clearance"},
            {"name": "oai-device-id", "value": "device-from-cookie"},
            {"name": "session", "value": "session-cookie"},
        ]

    async def cookies(self):
        return list(self._cookies)


class FakePage:
    def __init__(self) -> None:
        self.context = FakeContext()
        self.calls: list[tuple[str, object | None]] = []

    async def evaluate(self, script, argument=None):
        self.calls.append((script, argument))
        if "/api/auth/session" in script:
            return {"user": {"accessToken": "access-token"}}
        if "localStorage.getItem" in script:
            return "device-from-storage"
        raise AssertionError("unexpected page script")


def _write_cache(
    cache_dir,
    *,
    stored_at: float | None = None,
    payload: dict | None = None,
    raw: str | None = None,
) -> None:
    if raw is None:
        body = {
            "version": 1,
            "stored_at": time.time() if stored_at is None else stored_at,
            "access_token": "cached-access-token",
            "cookies": {
                "cf_clearance": "cached-clearance",
                "session": "cached-session",
            },
            "cf_clearance": "cached-clearance",
            "oai_device_id": "cached-device-id",
        }
        if payload is not None:
            body.update(payload)
        raw = json.dumps(body)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / CACHE_NAME).write_text(raw, encoding="utf-8")


@pytest.mark.anyio
async def test_successful_extract_writes_cache_with_0600_and_atomic(tmp_path):
    cache_dir = tmp_path / "profile"
    page = FakePage()
    manager = TokenManager(page, refresh_interval=1_800, cache_dir=cache_dir)

    bundle = await manager.extract_all()

    cache_file = cache_dir / CACHE_NAME
    assert cache_file.is_file()
    mode = stat.S_IMODE(cache_file.stat().st_mode)
    assert mode == 0o600
    # Atomic swap: no leftover temporary files next to the cache.
    assert [p.name for p in cache_dir.iterdir()] == [CACHE_NAME]

    stored = json.loads(cache_file.read_text(encoding="utf-8"))
    assert stored["version"] == 1
    assert stored["access_token"] == bundle.access_token == "access-token"
    assert stored["cookies"]["cf_clearance"] == "clearance"
    assert stored["cf_clearance"] == "clearance"
    assert stored["oai_device_id"] == "device-from-cookie"
    # Freshness anchor must be a plausible wall-clock timestamp.
    assert abs(stored["stored_at"] - time.time()) < 30


@pytest.mark.anyio
async def test_fresh_cache_serves_first_refresh_without_browser(tmp_path):
    cache_dir = tmp_path / "profile"
    _write_cache(cache_dir)

    page = FakePage()
    manager = TokenManager(page, refresh_interval=1_800, cache_dir=cache_dir)

    bundle = await manager.refresh_if_needed()

    assert bundle.access_token == "cached-access-token"
    assert bundle.oai_device_id == "cached-device-id"
    assert bundle.cf_clearance == "cached-clearance"
    assert bundle.cookie_header == "cf_clearance=cached-clearance; session=cached-session"
    # Cold start must not have touched the browser at all.
    assert page.calls == []


@pytest.mark.anyio
async def test_stale_cache_is_ignored_and_extract_runs_normally(tmp_path):
    cache_dir = tmp_path / "profile"
    interval = 600
    _write_cache(cache_dir, stored_at=time.time() - (interval + 5))

    page = FakePage()
    manager = TokenManager(page, refresh_interval=interval, cache_dir=cache_dir)

    bundle = await manager.refresh_if_needed()

    assert bundle.access_token == "access-token"
    session_calls = sum("/api/auth/session" in s for s, _ in page.calls)
    assert session_calls >= 1
    # The successful extract overwrites the stale cache with fresh data.
    stored = json.loads((cache_dir / CACHE_NAME).read_text(encoding="utf-8"))
    assert stored["access_token"] == "access-token"


@pytest.mark.anyio
async def test_corrupt_cache_is_skipped_silently(tmp_path):
    cache_dir = tmp_path / "profile"
    _write_cache(cache_dir, raw="{not valid json at all")

    page = FakePage()
    manager = TokenManager(page, refresh_interval=1_800, cache_dir=cache_dir)

    bundle = await manager.refresh_if_needed()

    assert bundle.access_token == "access-token"
    assert sum("/api/auth/session" in s for s, _ in page.calls) >= 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": ""},  # empty token is not a usable credential
        {"access_token": 123},  # wrong type
        {"cookies": "not-a-dict"},  # malformed cookie map
        {"cookies": {"a": 1}},  # non-string cookie value
        {"version": 99},  # unknown schema version
        {"stored_at": "yesterday"},  # unusable timestamp
    ],
)
async def test_malformed_cache_payloads_are_rejected(tmp_path, payload):
    cache_dir = tmp_path / "profile"
    _write_cache(cache_dir, payload=payload)

    page = FakePage()
    manager = TokenManager(page, refresh_interval=1_800, cache_dir=cache_dir)

    bundle = await manager.refresh_if_needed()
    assert bundle.access_token == "access-token"
    assert sum("/api/auth/session" in s for s, _ in page.calls) >= 1


@pytest.mark.anyio
async def test_missing_cache_dir_behaviour_unchanged(tmp_path):
    page = FakePage()
    manager = TokenManager(page, refresh_interval=1_800)

    assert manager._cache_path is None

    bundle = await manager.extract_all()

    assert bundle.access_token == "access-token"
    assert not list(tmp_path.rglob(CACHE_NAME))


@pytest.mark.anyio
async def test_future_timestamp_cache_is_not_trusted(tmp_path):
    """A stored_at ahead of wall clock is treated as invalid, not fresh."""
    cache_dir = tmp_path / "profile"
    _write_cache(cache_dir, stored_at=time.time() + 3_600)

    page = FakePage()
    manager = TokenManager(page, refresh_interval=1_800, cache_dir=cache_dir)

    bundle = await manager.refresh_if_needed()
    assert bundle.access_token == "access-token"
