"""CLI tests for `gpt-web account default` against a temporary registry."""

from __future__ import annotations

import argparse
import json

import pytest

from gpt.auth import AccountStore
from gpt.debug import cmd_account_default, cmd_account_remove


def _args(**overrides):
    values = {"name": None, "show": False, "clear": False}
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBGPT_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("WEBGPT_PROFILES_ROOT", str(tmp_path / "profiles"))
    store = AccountStore(tmp_path / "accounts.json", tmp_path / "profiles")
    store.ensure("alpha")
    store.ensure("beta")
    return store


def test_default_set_show_clear(tmp_store, capsys):
    # No default yet.
    cmd_account_default(_args(show=True))
    assert json.loads(capsys.readouterr().out)["default"] is None

    cmd_account_default(_args(name="beta"))
    assert "Default account set: beta" in capsys.readouterr().out
    assert tmp_store.get_default() == "beta"

    cmd_account_default(_args(show=True))
    assert json.loads(capsys.readouterr().out)["default"] == "beta"

    cmd_account_default(_args(clear=True))
    assert "Default account cleared." in capsys.readouterr().out
    assert tmp_store.get_default() is None


def test_default_bare_invocation_shows_current(tmp_store, capsys):
    tmp_store.set_default("alpha")
    cmd_account_default(_args())
    assert json.loads(capsys.readouterr().out)["default"] == "alpha"


def test_default_rejects_unknown_account(tmp_store):
    with pytest.raises(SystemExit):
        cmd_account_default(_args(name="ghost"))


def test_account_remove_clears_default(tmp_store, capsys):
    tmp_store.set_default("alpha")
    cmd_account_remove(argparse.Namespace(name="alpha", delete_profile=False))
    capsys.readouterr()
    assert tmp_store.get_default() is None


def test_login_adopts_first_default_only_once(tmp_store):
    from gpt.debug import _maybe_set_first_default

    _maybe_set_first_default(tmp_store, "alpha")
    assert tmp_store.get_default() == "alpha"

    # A second login must not steal the default.
    _maybe_set_first_default(tmp_store, "beta")
    assert tmp_store.get_default() == "alpha"
