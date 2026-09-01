from __future__ import annotations

from gpt.auth.accounts import AccountStore


def run_account_command(action: str, name: str | None = None) -> int:
    store = AccountStore()
    if action == "list":
        default = store.get_default()
        records = store.list()
        if not records:
            print("No registered accounts.")
            return 0
        for record in records:
            marker = "*" if record.name == default else " "
            print(f"{marker} {record.name:<18} {record.auth_status}")
        return 0
    if action == "default":
        if name:
            record = store.set_default(name)
            print(f"Default account: {record.name}")
        else:
            print(store.get_default() or "<unset>")
        return 0
    if action == "status":
        if not name:
            raise ValueError("account status requires a name")
        record = store.get(name)
        print(f"name        {record.name}")
        print(f"auth        {record.auth_status}")
        print(f"profile     {record.profile_dir}")
        print(f"credentials {'set' if record.credentials_file else 'not set'}")
        return 0
    raise ValueError(f"unknown account action: {action}")


__all__ = ["run_account_command"]
