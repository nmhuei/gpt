"""CODEX-AUTH-TOKEN-SOURCE tests (docs/reports/codex-oauth-research-2026-08-25.md).

Fake HTTP only — the network is never touched.  Covers the task-required
assertions: atomic 0600 load/save, single-use rotation persisted under the
cross-process flock, HTTP 400 invalid_grant → terminal DEAD state with no
retry loop, and the 60s expiry-skew refresh window.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import threading
import time

import pytest

from gpt.transport.codex_auth import (
    DEFAULT_CLIENT_ID,
    DEFAULT_TOKEN_URL,
    ENV_AUTH_JSON,
    ENV_CLIENT_ID,
    CodexAuthBundle,
    CodexAuthDead,
    CodexAuthDisabled,
    CodexAuthInvalid,
    CodexAuthManager,
    CodexAuthTransient,
    codex_auth_enabled,
    format_last_refresh,
    merge_bundle_into_raw,
    parse_jwt_exp,
    parse_last_refresh,
    save_auth_json,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scrub_codex_env(monkeypatch):
    """Isolate from shell-exported flags; every test opts in explicitly."""
    monkeypatch.delenv(ENV_AUTH_JSON, raising=False)
    monkeypatch.delenv(ENV_CLIENT_ID, raising=False)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_jwt(expires_in: float | None = 3600.0) -> str:
    """Build an unsigned-but-well-shaped JWT whose payload carries ``exp``."""
    payload: dict[str, object] = {"sub": "auth|user"}
    if expires_in is not None:
        payload["exp"] = int(time.time() + expires_in)
    return ".".join(
        [
            _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()),
            _b64url(json.dumps(payload).encode()),
            _b64url(b"signature"),
        ]
    )


def write_auth(
    path,
    *,
    access: str,
    refresh: str,
    id_token: str | None = None,
    account_id: str | None = "acct_123",
    last_refresh: float | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    """Write one auth.json document matching the codex-rs schema."""
    doc: dict[str, object] = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token,
            "access_token": access,
            "refresh_token": refresh,
            "account_id": account_id,
        },
    }
    if last_refresh is not None:
        doc["last_refresh"] = format_last_refresh(last_refresh)
    if extra:
        doc.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)


class FakeHTTP:
    """Injectable sync poster: records calls, replays canned responses."""

    def __init__(
        self,
        status: int = 200,
        payload: object | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.status = status
        self.payload = payload
        self.error = error
        self.delay = delay
        self.calls: list[dict[str, object]] = []

    def __call__(self, url: str, body: dict[str, str]) -> tuple[int, object]:
        self.calls.append({"url": url, "body": dict(body)})
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.status, self.payload


def make_manager(path, **kwargs) -> CodexAuthManager:
    kwargs.setdefault("http_post", FakeHTTP())
    return CodexAuthManager(auth_path=path, **kwargs)


# ---------------------------------------------------------------------------
# Flag gate — default OFF
# ---------------------------------------------------------------------------


def test_default_off_without_env():
    assert codex_auth_enabled() is False


def test_disabled_manager_raises_on_use(tmp_path):
    manager = CodexAuthManager()  # no auth_path, env scrubbed
    with pytest.raises(CodexAuthDisabled):
        asyncio.run(manager.get_access_token())


def test_enabled_flag_via_env(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(), refresh="R1")
    monkeypatch.setenv(ENV_AUTH_JSON, str(auth))
    assert codex_auth_enabled() is True


# ---------------------------------------------------------------------------
# Load validation
# ---------------------------------------------------------------------------


def test_load_missing_file_raises_invalid(tmp_path):
    manager = make_manager(tmp_path / "missing.json")
    with pytest.raises(CodexAuthInvalid):
        asyncio.run(manager.get_access_token())


def test_load_corrupt_json_raises_invalid(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("{not json", encoding="utf-8")
    manager = make_manager(auth)
    with pytest.raises(CodexAuthInvalid):
        asyncio.run(manager.get_access_token())


def test_load_non_object_root_raises_invalid(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("[1, 2]", encoding="utf-8")
    manager = make_manager(auth)
    with pytest.raises(CodexAuthInvalid):
        asyncio.run(manager.get_access_token())


def test_load_rejects_missing_refresh_token(tmp_path):
    """Grant-less documents are unusable for the refresh flow."""
    auth = tmp_path / "auth.json"
    # apikey mode: no tokens object at all.
    auth.write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x", "tokens": None}),
        encoding="utf-8",
    )
    manager = make_manager(auth)
    with pytest.raises(CodexAuthInvalid, match="tokens"):
        asyncio.run(manager.get_access_token())

    # chatgpt-shaped file whose grant was stripped: point at re-login.
    write_auth(auth, access=make_jwt(), refresh="")
    manager2 = make_manager(auth)
    with pytest.raises(CodexAuthInvalid, match="codex login"):
        asyncio.run(manager2.get_access_token())


# ---------------------------------------------------------------------------
# Freshness / skew behaviour
# ---------------------------------------------------------------------------


def test_valid_token_served_without_http(tmp_path):
    auth = tmp_path / "auth.json"
    token = make_jwt(expires_in=3600)
    write_auth(auth, access=token, refresh="R1")
    http = FakeHTTP(status=200, payload={})
    manager = make_manager(auth, http_post=http)
    assert asyncio.run(manager.get_access_token()) == token
    assert http.calls == []


def test_expiry_skew_triggers_refresh(tmp_path):
    """Token with <60s of life left must refresh, not be served."""
    auth = tmp_path / "auth.json"
    dying = make_jwt(expires_in=30.0)
    write_auth(auth, access=dying, refresh="R1")
    http = FakeHTTP(
        status=200,
        payload={
            "access_token": make_jwt(expires_in=3600),
            "refresh_token": "R2",
            "expires_in": 3600,
        },
    )
    manager = make_manager(auth, http_post=http)
    token = asyncio.run(manager.get_access_token())
    assert token != dying
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["url"] == DEFAULT_TOKEN_URL
    body = call["body"]
    assert body["grant_type"] == "refresh_token"
    assert body["client_id"] == DEFAULT_CLIENT_ID
    assert body["refresh_token"] == "R1"


def test_unknown_expiry_forces_refresh(tmp_path):
    """A non-JWT access token has unknown expiry → conservative refresh."""
    auth = tmp_path / "auth.json"
    write_auth(auth, access="opaque-not-a-jwt", refresh="R1")
    http = FakeHTTP(
        status=200,
        payload={"access_token": make_jwt(), "refresh_token": "R2"},
    )
    manager = make_manager(auth, http_post=http)
    assert asyncio.run(manager.get_access_token()).startswith("ey")
    assert len(http.calls) == 1


def test_client_id_from_env_override(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(expires_in=30.0), refresh="R1")
    monkeypatch.setenv(ENV_CLIENT_ID, "app_custom")
    http = FakeHTTP(status=200, payload={"access_token": make_jwt()})
    manager = make_manager(auth, http_post=http)
    asyncio.run(manager.get_access_token())
    assert http.calls[0]["body"]["client_id"] == "app_custom"


def test_expires_in_wins_over_jwt_exp(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(expires_in=30.0), refresh="R1")
    http = FakeHTTP(
        status=200,
        payload={"access_token": make_jwt(expires_in=7200), "expires_in": 120},
    )
    manager = make_manager(auth, http_post=http)
    asyncio.run(manager.get_access_token())
    state = manager.state
    assert state is not None and state.expires_at is not None
    delta = state.expires_at - time.time()
    assert 110 <= delta <= 130  # expires_in honoured, not the JWT's 7200


# ---------------------------------------------------------------------------
# Rotation persistence (atomic, key-preserving, 0600)
# ---------------------------------------------------------------------------


def test_refresh_rotates_and_preserves_unknown_keys(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth(
        auth,
        access=make_jwt(expires_in=10.0),
        refresh="R1",
        id_token="id-old",
        extra={"agent_identity": "keep-me", "personal_access_token": None},
    )
    http = FakeHTTP(
        status=200,
        payload={
            "access_token": make_jwt(expires_in=3600),
            "refresh_token": "R2",
            "id_token": "id-new",
            "account_id": "acct_999",
        },
    )
    manager = make_manager(auth, http_post=http)
    asyncio.run(manager.get_access_token())

    raw = json.loads(auth.read_text(encoding="utf-8"))
    tokens = raw["tokens"]
    assert tokens["refresh_token"] == "R2"  # rotation persisted
    assert tokens["id_token"] == "id-new"
    assert tokens["account_id"] == "acct_999"
    assert raw["OPENAI_API_KEY"] is None  # untouched sibling keys survive
    assert raw["agent_identity"] == "keep-me"
    assert raw["personal_access_token"] is None
    assert raw["auth_mode"] == "chatgpt"
    assert parse_last_refresh(raw["last_refresh"]) > time.time() - 60

    mode = stat.S_IMODE(os.stat(auth).st_mode)
    assert mode == 0o600
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []  # atomic swap left no temp files


def test_lock_file_written_0600_after_refresh(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(expires_in=10.0), refresh="R1")
    http = FakeHTTP(status=200, payload={"access_token": make_jwt(), "expires_in": 600})
    manager = make_manager(auth, http_post=http)
    asyncio.run(manager.get_access_token())
    lock_path = tmp_path / "auth.json.lock"
    assert lock_path.exists()
    assert stat.S_IMODE(os.stat(lock_path).st_mode) == 0o600


def test_save_auth_json_direct_roundtrip(tmp_path):
    auth = tmp_path / "nested" / "auth.json"
    original = {
        "custom_key": {"deep": [1, 2]},
        "tokens": {"legacy": "field"},
    }
    bundle = CodexAuthBundle(access_token="a", refresh_token="r")
    written = save_auth_json(auth, original, bundle)
    assert json.loads(auth.read_text(encoding="utf-8")) == written
    assert written["custom_key"] == {"deep": [1, 2]}
    assert written["tokens"]["access_token"] == "a"
    assert written["last_refresh"].endswith("Z")


def test_merge_keeps_bundle_fields_out_of_unknown_keys():
    raw = {"unexpected": 1}
    merged = merge_bundle_into_raw(raw, CodexAuthBundle("a", "r"), time.time())
    assert merged["unexpected"] == 1
    assert raw is not merged  # input document never mutated


# ---------------------------------------------------------------------------
# DEAD handling — single-use rotation fallout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403])
def test_rejected_refresh_marks_dead_and_stops_retrying(tmp_path, status):
    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(expires_in=10.0), refresh="R1")
    http = FakeHTTP(status=status, payload={"error": "invalid_grant"})
    manager = make_manager(auth, http_post=http)

    with pytest.raises(CodexAuthDead, match="invalid_grant"):
        asyncio.run(manager.get_access_token())

    # Fast-fail forever: the retry loop must not exist.
    with pytest.raises(CodexAuthDead):
        asyncio.run(manager.get_access_token())
    with pytest.raises(CodexAuthDead):
        asyncio.run(manager.get_access_token())
    assert len(http.calls) == 1

    # A dead grant must never overwrite the file with anything.
    raw = json.loads(auth.read_text(encoding="utf-8"))
    assert raw["tokens"]["refresh_token"] == "R1"

    assert manager.dead_reason is not None and "invalid_grant" in manager.dead_reason


def test_chain_older_than_8_days_dead_before_http(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth(
        auth,
        access=make_jwt(expires_in=10.0),  # forces the refresh path
        refresh="R1",
        last_refresh=time.time() - 9 * 86400,
    )
    http = FakeHTTP(status=200, payload={"access_token": make_jwt()})
    manager = make_manager(auth, http_post=http)
    with pytest.raises(CodexAuthDead, match="re-login"):
        asyncio.run(manager.get_access_token())
    assert http.calls == []


def test_chain_age_gate_skipped_when_token_still_fresh(tmp_path):
    """The 8-day gate fires only on the refresh path, never kills live tokens."""
    auth = tmp_path / "auth.json"
    write_auth(
        auth,
        access=make_jwt(expires_in=3600),
        refresh="R1",
        last_refresh=time.time() - 9 * 86400,
    )
    http = FakeHTTP(status=200, payload={})
    manager = make_manager(auth, http_post=http)
    assert asyncio.run(manager.get_access_token())
    assert http.calls == []


def test_transient_500_does_not_kill_credential(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(expires_in=10.0), refresh="R1")

    responses = iter(
        [
            (500, None),
            (200, {"access_token": make_jwt(expires_in=3600), "refresh_token": "R2"}),
        ]
    )

    def flaky_post(url: str, body: dict[str, str]) -> tuple[int, object]:
        return next(responses)

    manager = make_manager(auth, http_post=flaky_post)
    with pytest.raises(CodexAuthTransient):
        asyncio.run(manager.get_access_token())
    assert manager.dead_reason is None  # still alive
    assert asyncio.run(manager.get_access_token()).startswith("ey")


def test_transport_failure_is_transient(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(expires_in=10.0), refresh="R1")
    http = FakeHTTP(error=OSError("connection reset"))
    manager = make_manager(auth, http_post=http)
    with pytest.raises(CodexAuthTransient, match="connection reset"):
        asyncio.run(manager.get_access_token())
    assert manager.dead_reason is None


def test_success_payload_missing_access_token_is_transient(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(expires_in=10.0), refresh="R1")
    http = FakeHTTP(status=200, payload={"nope": True})
    manager = make_manager(auth, http_post=http)
    with pytest.raises(CodexAuthTransient):
        asyncio.run(manager.get_access_token())
    assert manager.dead_reason is None


# ---------------------------------------------------------------------------
# Concurrency — in-process dedup + cross-process flock re-read
# ---------------------------------------------------------------------------


def test_concurrent_callers_share_one_refresh(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(expires_in=10.0), refresh="R1")
    http = FakeHTTP(
        status=200,
        payload={"access_token": make_jwt(expires_in=3600), "refresh_token": "R2"},
        delay=0.05,
    )
    manager = make_manager(auth, http_post=http)

    async def gather_two() -> list[str]:
        return await asyncio.gather(
            manager.get_access_token(),
            manager.get_access_token(),
        )

    tokens = asyncio.run(gather_two())
    assert tokens[0] == tokens[1]
    assert len(http.calls) == 1


def test_other_process_rotation_adopted_via_in_lock_reread(tmp_path):
    """Blocked process must adopt the winner's snapshot, not spend its grant.

    Simulates research §5.1: this process's memory holds a dying token and a
    pre-rotation refresh grant; while its worker sits inside the critical
    section, "another process" rotates the grant on disk under the flock.
    The mandatory in-lock re-read must adopt the rotated fresh token and
    skip the HTTP refresh entirely — posting the stale grant would trip the
    server's reuse detection and revoke the chain.
    """
    import fcntl

    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(expires_in=10.0), refresh="R1")
    http = FakeHTTP(status=200, payload={"access_token": make_jwt()})
    manager = make_manager(auth, http_post=http)

    # Force a genuinely stale memory snapshot: a previous turn cached the
    # near-expiry bundle, then the world moved on underneath us.
    manager._state = CodexAuthBundle(
        access_token=make_jwt(expires_in=10.0),
        refresh_token="R1",
        expires_at=time.time() + 10.0,
    )
    rotated = make_jwt(expires_in=3600)

    entered = threading.Event()
    original_refresh = manager._locked_refresh

    def signal_then_refresh(*args: object, **kwargs: object) -> str:
        entered.set()
        time.sleep(0.05)
        return original_refresh(*args, **kwargs)  # type: ignore[arg-type]

    manager._locked_refresh = signal_then_refresh  # type: ignore[method-assign]

    async def wait_ready(timeout: float = 2.0) -> None:
        deadline = time.time() + timeout
        while not entered.is_set():
            assert time.time() < deadline, "worker never reached the refresh path"
            await asyncio.sleep(0.01)

    async def scenario() -> str:
        task = asyncio.create_task(manager.get_access_token())
        await wait_ready()
        # The worker is now mid-refresh; perform the competing rotation.
        with open(tmp_path / "auth.json.lock", "a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            write_auth(auth, access=rotated, refresh="R2")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return await task

    assert asyncio.run(scenario()) == rotated
    assert http.calls == []


# ---------------------------------------------------------------------------
# Codec units
# ---------------------------------------------------------------------------


def test_parse_jwt_exp_variants():
    good = make_jwt(expires_in=120.0)
    assert parse_jwt_exp(good) is not None
    assert abs((parse_jwt_exp(good) or 0) - (time.time() + 120)) < 5
    assert parse_jwt_exp("not-a-jwt") is None
    assert parse_jwt_exp("a.b") is None  # wrong segment count
    header_only = ".".join([_b64url(b"{}"), _b64url(b'{"n":1}'), "sig"])
    assert parse_jwt_exp(header_only) is None  # no exp claim
    string_exp = ".".join(["x", _b64url(b'{"exp":"soon"}'), "sig"])
    assert parse_jwt_exp(string_exp) is None
    bool_exp = ".".join(["x", _b64url(b'{"exp":true}'), "sig"])
    assert parse_jwt_exp(bool_exp) is None


def test_last_refresh_codec_roundtrip():
    epoch = 1_750_000_000.0
    text = format_last_refresh(epoch)
    assert text.endswith("Z")
    assert abs(parse_last_refresh(text) - epoch) < 1
    assert parse_last_refresh(None) == 0.0
    assert parse_last_refresh("") == 0.0
    assert parse_last_refresh("garbage") == 0.0
    # Naive timestamps are read as UTC.
    assert parse_last_refresh("2026-08-25T00:00:00") == parse_last_refresh(
        "2026-08-25T00:00:00Z"
    )


def test_invalidate_forces_reload_and_refresh(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(expires_in=3600), refresh="R1")
    http = FakeHTTP(
        status=200,
        payload={"access_token": make_jwt(expires_in=3600), "refresh_token": "R2"},
    )
    manager = make_manager(auth, http_post=http)
    assert asyncio.run(manager.get_access_token())  # primes a fresh snapshot
    assert http.calls == []

    # External world moves on: file now holds a dying token again.
    write_auth(auth, access=make_jwt(expires_in=10.0), refresh="R3")
    manager.invalidate()
    asyncio.run(manager.get_access_token())
    assert len(http.calls) == 1
    assert http.calls[0]["body"]["refresh_token"] == "R3"


def test_bundle_snapshot_property(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(expires_in=3600), refresh="R1", account_id="acct")
    manager = make_manager(auth)
    assert manager.state is None  # nothing loaded before first use
    asyncio.run(manager.get_access_token())
    state = manager.state
    assert isinstance(state, CodexAuthBundle)
    assert state.refresh_token == "R1"
    assert state.account_id == "acct"


# ---------------------------------------------------------------------------
# Force refresh + distrust latch (codex review round 13, finding 1)
# ---------------------------------------------------------------------------


def test_force_refresh_bypasses_fresh_disk_bundle(tmp_path):
    """``force_refresh=True`` must post a REAL refresh grant.

    Round 13: ``invalidate()`` alone re-reads the SAME still-fresh token from
    disk, so a resource-server 401 "rotate once" retry replayed the exact
    rejected bearer just because its JWT ``exp`` was still in the future.
    """
    auth = tmp_path / "auth.json"
    rejected = make_jwt(expires_in=3600)
    write_auth(auth, access=rejected, refresh="R1")
    # Distinct exp so the rotated JWT string differs from the rejected one
    # even when both are minted within the same second.
    http = FakeHTTP(
        status=200,
        payload={"access_token": make_jwt(expires_in=7200), "refresh_token": "R2"},
    )
    manager = make_manager(auth, http_post=http)

    assert asyncio.run(manager.get_access_token()) == rejected
    assert http.calls == []  # plain call serves the fresh disk bundle

    rotated = asyncio.run(manager.get_access_token(force_refresh=True))

    assert rotated != rejected
    assert len(http.calls) == 1
    assert http.calls[0]["body"]["refresh_token"] == "R1"
    raw = json.loads(auth.read_text(encoding="utf-8"))
    assert raw["tokens"]["refresh_token"] == "R2"  # rotation persisted


def test_force_refresh_still_honours_dead_latch(tmp_path):
    """Forcing can never revive a DEAD credential (no retry loop, contract)."""
    auth = tmp_path / "auth.json"
    write_auth(auth, access=make_jwt(expires_in=10.0), refresh="R1")
    http = FakeHTTP(status=400, payload={"error": "invalid_grant"})
    manager = make_manager(auth, http_post=http)

    with pytest.raises(CodexAuthDead, match="invalid_grant"):
        asyncio.run(manager.get_access_token())
    with pytest.raises(CodexAuthDead):
        asyncio.run(manager.get_access_token(force_refresh=True))

    assert len(http.calls) == 1  # exactly one POST ever; latch fast-fails
    assert manager.dead_reason is not None
    assert manager.untrusted_reason is None  # distrust never overrides DEAD


def test_mark_untrusted_forces_real_refresh_until_trusted(tmp_path):
    """Distrust latch: every call really rotates until mark_trusted clears it."""
    auth = tmp_path / "auth.json"
    rejected = make_jwt(expires_in=3600)
    write_auth(auth, access=rejected, refresh="R1")
    responses = iter(
        [
            (200, {"access_token": make_jwt(expires_in=7200), "refresh_token": "R2"}),
            (200, {"access_token": make_jwt(expires_in=10800), "refresh_token": "R3"}),
        ]
    )

    def rotating_post(url: str, body: dict[str, str]) -> tuple[int, object]:
        return next(responses)

    manager = make_manager(auth, http_post=rotating_post)
    assert asyncio.run(manager.get_access_token()) == rejected

    manager.mark_untrusted("resource server rejected the bearer twice")
    assert manager.untrusted_reason is not None

    # While distrusted, even fresh snapshots are never served from cache:
    # each call performs one real rotation.
    first = asyncio.run(manager.get_access_token())
    assert first != rejected
    second = asyncio.run(manager.get_access_token())
    assert second != first

    manager.mark_trusted()
    assert manager.untrusted_reason is None
    assert asyncio.run(manager.get_access_token()) == second  # cache again
    assert manager.state is not None and manager.state.refresh_token == "R3"
