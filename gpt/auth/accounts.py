from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

from gpt.auth.authenticator import LoginCredentials

DEFAULT_CONFIG_ROOT = Path.home() / ".config" / "webgpt"
DEFAULT_DATA_ROOT = Path.home() / ".local" / "share" / "webgpt"
DEFAULT_ACCOUNTS_ROOT = DEFAULT_CONFIG_ROOT
DEFAULT_PROFILES_ROOT = DEFAULT_DATA_ROOT / "profiles"
DEFAULT_ACCOUNT_REGISTRY = DEFAULT_ACCOUNTS_ROOT / "accounts.json"
DEFAULT_ACCOUNT_KEY = "default_account"
DEFAULT_ACCOUNT_ENV = "WEBGPT_DEFAULT_ACCOUNT"

_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UNSET = object()
_REGISTRY_BACKUP_KEEP = 3

logger = logging.getLogger(__name__)


def validate_account_name(name: str) -> str:
    value = name.strip()
    if not _ACCOUNT_NAME_RE.fullmatch(value):
        raise ValueError(
            "Account name must be 1-64 characters using letters, numbers, '.', '_' or '-'."
        )
    return value


@dataclass
class AccountRecord:
    name: str
    profile_dir: str
    credentials_file: str | None = None
    auth_status: str = "unknown"
    created_at: float = 0.0
    updated_at: float = 0.0


class AccountStore:
    """Local registry for named ChatGPT Web profiles and optional credentials."""

    def __init__(
        self,
        registry_path: str | Path | None = None,
        profiles_root: str | Path | None = None,
    ) -> None:
        resolved_registry = (
            registry_path
            or os.environ.get("WEBGPT_ACCOUNTS_FILE")
            or DEFAULT_ACCOUNT_REGISTRY
        )
        resolved_profiles = (
            profiles_root
            or os.environ.get("WEBGPT_PROFILES_ROOT")
            or DEFAULT_PROFILES_ROOT
        )
        self.registry_path = Path(resolved_registry).expanduser()
        self.profiles_root = Path(resolved_profiles).expanduser()
        self.accounts_root = self.registry_path.parent
        self._ensure_layout()
        self._warn_if_registry_missing()

    def _warn_if_registry_missing(self) -> None:
        """Surface a likely registry deletion instead of failing silently later.

        Crash-loop signature this guards against: profiles on disk, registry
        gone → every session lease raises "Unknown account profile" with no
        hint about the missing file. Fires once per store construction.
        """
        if self.registry_path.exists():
            return
        try:
            entries = sorted(p.name for p in self.profiles_root.iterdir())
        except OSError:
            return
        if not entries:
            return
        preview = ", ".join(entries[:8]) + (", …" if len(entries) > 8 else "")
        logger.warning(
            "accounts registry missing but profiles exist — possible deletion, "
            "check .bak backups next to the registry "
            "(registry=%s profiles_root=%s entries=[%s])",
            self.registry_path,
            self.profiles_root,
            preview,
        )

    def _ensure_layout(self) -> None:
        for path in (self.accounts_root, self.profiles_root):
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass

    def _read_raw(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {}
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _read(self) -> dict[str, AccountRecord]:
        records = self._read_raw().get("accounts", [])
        result: dict[str, AccountRecord] = {}
        if not isinstance(records, list):
            return result
        known_fields = {f.name for f in dataclass_fields(AccountRecord)}
        for item in records:
            if not isinstance(item, dict):
                continue
            # Tolerate unknown keys: keep the known fields, drop the rest.
            filtered = {k: v for k, v in item.items() if k in known_fields}
            try:
                record = AccountRecord(**filtered)
                validate_account_name(record.name)
            except (TypeError, ValueError):
                continue
            result[record.name] = record
        return result

    def _stored_default(self) -> str | None:
        value = self._read_raw().get(DEFAULT_ACCOUNT_KEY)
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            validate_account_name(value)
        except ValueError:
            return None
        return value

    def _backup_registry(self) -> None:
        """Snapshot the current registry before it is overwritten.

        Keeps at most ``_REGISTRY_BACKUP_KEEP`` rotated copies next to the
        registry (``accounts.json.bak.1`` newest … ``.bak.N`` oldest) so an
        accidental deletion or corruption is always recoverable from disk.
        Backup failures never block the write itself — they only log.
        """
        if not self.registry_path.exists():
            return
        base_name = self.registry_path.name
        for i in range(_REGISTRY_BACKUP_KEEP - 1, 0, -1):
            src = self.registry_path.with_name(f"{base_name}.bak.{i}")
            if not src.exists():
                continue
            try:
                src.replace(self.registry_path.with_name(f"{base_name}.bak.{i + 1}"))
            except OSError:
                logger.warning(
                    "accounts registry backup rotate failed slot=%s", i, exc_info=True
                )
        target = self.registry_path.with_name(f"{base_name}.bak.1")
        try:
            shutil.copy2(self.registry_path, target)
            os.chmod(target, 0o600)
        except OSError:
            logger.warning(
                "accounts registry backup failed path=%s", target, exc_info=True
            )

    def _write(
        self,
        records: dict[str, AccountRecord],
        *,
        default_account: str | object | None = _UNSET,
    ) -> None:
        self._ensure_layout()
        if default_account is _UNSET:
            stored_default = self._stored_default()
        else:
            stored_default = default_account  # type: ignore[assignment]
        payload = {
            "version": 1,
            DEFAULT_ACCOUNT_KEY: stored_default,
            "accounts": [asdict(records[name]) for name in sorted(records)],
        }
        # Snapshot the previous on-disk state before it is clobbered.
        self._backup_registry()
        temporary = self.registry_path.with_suffix(
            f"{self.registry_path.suffix}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.registry_path)
        os.chmod(self.registry_path, 0o600)

    def list(self) -> list[AccountRecord]:
        records = self._read()
        return [records[name] for name in sorted(records)]

    def get(self, name: str) -> AccountRecord:
        name = validate_account_name(name)
        record = self._read().get(name)
        if record is None:
            raise KeyError(f"Unknown account profile: {name}")
        return record

    def ensure(self, name: str) -> AccountRecord:
        name = validate_account_name(name)
        records = self._read()
        existing = records.get(name)
        if existing is not None:
            profile = Path(existing.profile_dir).expanduser()
            profile.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(profile, 0o700)
            except OSError:
                pass
            return existing
        now = time.time()
        profile_dir = (self.profiles_root / name).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(profile_dir, 0o700)
        record = AccountRecord(
            name=name,
            profile_dir=str(profile_dir),
            created_at=now,
            updated_at=now,
        )
        records[name] = record
        self._write(records)
        return record

    def update_status(self, name: str, status: str) -> AccountRecord:
        name = validate_account_name(name)
        records = self._read()
        if name not in records:
            self.ensure(name)
            records = self._read()
        record = records[name]
        record.auth_status = status
        record.updated_at = time.time()
        records[name] = record
        self._write(records)
        return record

    def save_credentials(self, name: str, credentials: LoginCredentials) -> Path:
        record = self.ensure(name)
        values = [
            credentials.username,
            credentials.password,
            credentials.totp_secret_or_code or "",
        ]
        if any("|" in value or "\n" in value or "\r" in value for value in values):
            raise ValueError("Saved credentials cannot contain '|', CR or LF characters.")
        path = self.accounts_root / f"{record.name}.cred"
        path.write_text("|".join(values) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        records = self._read()
        record = records[record.name]
        record.credentials_file = str(path)
        record.updated_at = time.time()
        records[record.name] = record
        self._write(records)
        return path

    def load_credentials(self, name: str) -> LoginCredentials:
        record = self.get(name)
        if not record.credentials_file:
            raise FileNotFoundError(f"Account {record.name!r} has no saved credentials.")
        path = Path(record.credentials_file).expanduser()
        raw = path.read_text(encoding="utf-8").strip()
        username, sep, remainder = raw.partition("|")
        if not sep:
            raise ValueError("Saved credential file is malformed.")
        password, sep, totp = remainder.partition("|")
        if not sep or not username or not password:
            raise ValueError("Saved credential file is malformed.")
        return LoginCredentials(username, password, totp or None)

    def delete_credentials(self, name: str) -> None:
        records = self._read()
        name = validate_account_name(name)
        record = records.get(name)
        if record is None:
            raise KeyError(f"Unknown account profile: {name}")
        if record.credentials_file:
            try:
                Path(record.credentials_file).expanduser().unlink()
            except FileNotFoundError:
                pass
        record.credentials_file = None
        record.updated_at = time.time()
        records[name] = record
        self._write(records)

    def remove(self, name: str, *, delete_profile: bool = False) -> None:
        name = validate_account_name(name)
        records = self._read()
        record = records.pop(name, None)
        if record is None:
            raise KeyError(f"Unknown account profile: {name}")
        if record.credentials_file:
            try:
                Path(record.credentials_file).expanduser().unlink()
            except FileNotFoundError:
                pass
        if delete_profile:
            shutil.rmtree(Path(record.profile_dir).expanduser(), ignore_errors=True)
        default_account = self._stored_default()
        if default_account == name:
            self._write(records, default_account=None)
        else:
            self._write(records)

    def get_default(self) -> str | None:
        """Return the registered default account name, or None when unset/invalid."""
        name = self._stored_default()
        if name is None:
            return None
        return name if name in self._read() else None

    def set_default(self, name: str) -> AccountRecord:
        """Register an existing account as the sticky default for new sessions."""
        name = validate_account_name(name)
        record = self.get(name)
        self._write(self._read(), default_account=record.name)
        return record

    def clear_default(self) -> None:
        """Remove the default account marker without touching any profile."""
        self._write(self._read(), default_account=None)


def _find_string(value: Any, *keys: str) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for candidate in value.values():
            found = _find_string(candidate, *keys)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_string(candidate, *keys)
            if found:
                return found
    return None


async def browser_session_authenticated(page: Any) -> bool:
    """Verify a ChatGPT web session without reading or manipulating the DOM."""
    if "chatgpt.com" not in str(getattr(page, "url", "")):
        return False
    try:
        session = await page.evaluate(
            """async () => {
                const response = await fetch('/api/auth/session', {credentials: 'include'});
                if (!response.ok) return {};
                return response.json();
            }"""
        )
    except Exception:
        return False
    return bool(_find_string(session, "accessToken", "access_token"))


def resolve_default_account(store: AccountStore) -> str | None:
    """Resolve the sticky default account for new sessions.

    Precedence: ``WEBGPT_DEFAULT_ACCOUNT`` env override, then the registry's
    ``default_account`` key. Values naming unknown accounts are ignored so a
    stale override can never pin traffic to nothing. Gateway/server wiring
    (``_lease_session``) should call this in a later wave.
    """
    known = {record.name for record in store.list()}
    env_value = os.environ.get(DEFAULT_ACCOUNT_ENV, "").strip()
    if env_value and env_value in known:
        return env_value
    registered = store.get_default()
    if registered is not None and registered in known:
        return registered
    return None


def find_cloak_executable() -> Path:
    root = Path.home() / ".cloakbrowser"
    candidates = sorted(root.glob("**/chrome"), reverse=True) if root.is_dir() else []
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise FileNotFoundError("CloakBrowser executable was not found under ~/.cloakbrowser.")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_loopback_port(port: int, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            del reader
            return
        except OSError:
            await asyncio.sleep(0.2)
    raise RuntimeError(f"CloakBrowser CDP port {port} did not become ready.")


async def _manual_cloak_login_binary(
    profile_dir: Path, *, url: str, wait_seconds: int
) -> bool:
    from playwright.async_api import async_playwright

    executable = find_cloak_executable()
    port = _free_loopback_port()
    process = subprocess.Popen(
        [
            str(executable),
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    playwright = None
    browser = None
    try:
        await _wait_for_loopback_port(port)
        playwright = await async_playwright().start()
        browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        if not browser.contexts:
            raise RuntimeError("CloakBrowser did not expose its persistent context over CDP.")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        deadline = time.monotonic() + max(1, wait_seconds)
        while time.monotonic() < deadline:
            if await browser_session_authenticated(page):
                return True
            await asyncio.sleep(1.0)
        return False
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception as exc:
                logger.debug("Could not close login CDP browser cleanly: %s", exc)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception as exc:
                logger.debug("Could not stop Playwright cleanly: %s", exc)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


async def manual_cloak_login(
    profile_dir: str | Path,
    *,
    url: str = "https://chatgpt.com/",
    wait_seconds: int = 300,
) -> bool:
    """Open a headful CloakBrowser profile and wait for normal operator login."""
    target = Path(profile_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    try:
        from cloakbrowser import launch_persistent_context_async
    except ImportError:
        return await _manual_cloak_login_binary(
            target, url=url, wait_seconds=wait_seconds
        )

    context = await launch_persistent_context_async(
        user_data_dir=str(target),
        headless=False,
    )
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        deadline = time.monotonic() + max(1, wait_seconds)
        while time.monotonic() < deadline:
            if await browser_session_authenticated(page):
                return True
            await asyncio.sleep(1.0)
        return False
    finally:
        await context.close()


__all__ = [
    "DEFAULT_ACCOUNT_ENV",
    "DEFAULT_ACCOUNT_KEY",
    "DEFAULT_ACCOUNT_REGISTRY",
    "DEFAULT_PROFILES_ROOT",
    "AccountRecord",
    "AccountStore",
    "browser_session_authenticated",
    "find_cloak_executable",
    "manual_cloak_login",
    "resolve_default_account",
    "validate_account_name",
]
