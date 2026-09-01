from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

import pytest

from gpt.auth import AccountStore, LoginCredentials


def _backup_files(root: Path) -> list[Path]:
    return sorted(root.glob("accounts.json.bak.*"))


def test_account_store_keeps_metadata_and_credentials_separate(tmp_path: Path):
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    record = store.ensure("personal")
    credentials = LoginCredentials("user@example.com", "secret-password", "ABCDEF123456")

    credential_file = store.save_credentials("personal", credentials)
    payload = json.loads((tmp_path / "accounts.json").read_text())

    assert record.profile_dir == str((tmp_path / "profiles" / "personal").resolve())
    assert payload["accounts"][0]["name"] == "personal"
    assert "secret-password" not in (tmp_path / "accounts.json").read_text()
    assert credential_file.read_text().strip() == "user@example.com|secret-password|ABCDEF123456"
    assert stat.S_IMODE(credential_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "accounts.json").stat().st_mode) == 0o600
    assert store.load_credentials("personal") == credentials


def test_account_store_supports_multiple_profiles(tmp_path: Path):
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    store.ensure("personal")
    store.ensure("work")

    assert [record.name for record in store.list()] == ["personal", "work"]
    assert store.get("personal").profile_dir != store.get("work").profile_dir


def test_delete_credentials_preserves_profile(tmp_path: Path):
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    record = store.ensure("personal")
    store.save_credentials("personal", LoginCredentials("u", "p", "123456"))

    store.delete_credentials("personal")

    assert Path(record.profile_dir).is_dir()
    assert store.get("personal").credentials_file is None


def test_default_account_round_trip(tmp_path: Path):
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    assert store.get_default() is None

    store.ensure("personal")
    store.ensure("work")
    store.set_default("work")

    payload = json.loads((tmp_path / "accounts.json").read_text())
    assert payload["default_account"] == "work"
    # Default survives unrelated writes (status updates).
    store.update_status("personal", "authenticated")
    assert store.get_default() == "work"

    store.clear_default()
    assert store.get_default() is None


def test_set_default_rejects_unknown_account(tmp_path: Path):
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    with pytest.raises(KeyError):
        store.set_default("ghost")


def test_remove_clears_matching_default(tmp_path: Path):
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    store.ensure("personal")
    store.ensure("work")

    store.set_default("work")
    store.remove("work")
    assert store.get_default() is None

    # Removing a non-default account keeps the default intact.
    store.ensure("extra")
    store.set_default("extra")
    store.remove("personal")
    assert store.get_default() == "extra"


def test_reader_tolerates_unknown_keys(tmp_path: Path):
    registry = tmp_path / "accounts.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "default_account": "personal",
                "future_top_level": {"anything": True},
                "accounts": [
                    {
                        "name": "personal",
                        "profile_dir": str(tmp_path / "profiles" / "personal"),
                        "brand_new_field": "ignored",
                    }
                ],
            }
        )
    )
    store = AccountStore(registry, tmp_path / "profiles")

    names = [record.name for record in store.list()]
    assert names == ["personal"]
    assert store.get_default() == "personal"


def test_resolve_default_account_env_overrides_registry(
    tmp_path: Path, monkeypatch
):
    from gpt.auth.accounts import resolve_default_account

    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    store.ensure("registry-default")
    store.ensure("env-account")
    store.set_default("registry-default")

    monkeypatch.delenv("WEBGPT_DEFAULT_ACCOUNT", raising=False)
    assert resolve_default_account(store) == "registry-default"

    monkeypatch.setenv("WEBGPT_DEFAULT_ACCOUNT", "env-account")
    assert resolve_default_account(store) == "env-account"

    # Unknown env override is ignored; registry default wins instead.
    monkeypatch.setenv("WEBGPT_DEFAULT_ACCOUNT", "ghost")
    assert resolve_default_account(store) == "registry-default"


# ---------------------------------------------------------------------------
# Backup-on-write: every save snapshots the previous registry (.bak.1..3)
# ---------------------------------------------------------------------------


def test_first_write_leaves_no_backups(tmp_path: Path):
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")

    store.ensure("personal")

    assert _backup_files(tmp_path) == []


def test_write_snapshots_previous_registry_as_bak(tmp_path: Path):
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    store.ensure("personal")

    store.update_status("personal", "authenticated")

    backup = tmp_path / "accounts.json.bak.1"
    assert backup.exists()
    previous = json.loads(backup.read_text())
    assert previous["accounts"][0]["auth_status"] == "unknown"
    current = json.loads((tmp_path / "accounts.json").read_text())
    assert current["accounts"][0]["auth_status"] == "authenticated"
    # Backups inherit the registry's private permissions.
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_backup_rotation_keeps_at_most_three(tmp_path: Path):
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    store.ensure("personal")

    for i in range(5):  # more writes than kept backups
        store.update_status("personal", f"s{i}")

    names = [p.name for p in _backup_files(tmp_path)]
    assert names == [
        "accounts.json.bak.1",
        "accounts.json.bak.2",
        "accounts.json.bak.3",
    ]

    def status_of(name: str) -> str:
        payload = json.loads((tmp_path / name).read_text())
        return payload["accounts"][0]["auth_status"]

    # Newest snapshot first; the two oldest states (unknown, s0) rotated out.
    assert status_of("accounts.json") == "s4"
    assert status_of("accounts.json.bak.1") == "s3"
    assert status_of("accounts.json.bak.2") == "s2"
    assert status_of("accounts.json.bak.3") == "s1"


# ---------------------------------------------------------------------------
# Startup warn: missing registry with surviving profiles must be loud
# ---------------------------------------------------------------------------


def _capture_init(root: Path, caplog) -> list[logging.LogRecord]:
    logger_name = "gpt.auth.accounts"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        AccountStore(root / "accounts.json", root / "profiles")
    return [r for r in caplog.records if r.name == logger_name]


def test_init_warns_when_registry_missing_but_profiles_exist(
    tmp_path: Path, caplog
):
    profiles_root = tmp_path / "profiles"
    (profiles_root / "personal").mkdir(parents=True)

    records = _capture_init(tmp_path, caplog)

    messages = [r.getMessage() for r in records]
    assert any(
        "registry missing but profiles exist" in message for message in messages
    ), messages


def test_init_silent_when_registry_present(tmp_path: Path, caplog):
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    store.ensure("personal")

    records = _capture_init(tmp_path, caplog)

    assert not [r for r in records if "registry missing" in r.getMessage()]


def test_init_silent_when_no_profiles_yet(tmp_path: Path, caplog):
    records = _capture_init(tmp_path, caplog)

    assert not [r for r in records if "registry missing" in r.getMessage()]
