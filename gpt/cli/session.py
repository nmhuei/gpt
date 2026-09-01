from __future__ import annotations

from pathlib import Path

from gpt.agent.session import SessionStore


def run_session_command(workspace: Path, action: str) -> int:
    store = SessionStore.default()
    if action == "list":
        rows = store.list()
        if not rows:
            print("No remembered sessions.")
            return 0
        for row in rows:
            marker = "*" if row.get("workspace") == str(workspace.resolve()) else " "
            # Deliberately hide raw gateway ids from normal UX.
            print(f"{marker} {row.get('workspace', '-')}")
        return 0
    if action == "current":
        print("remembered" if store.get(workspace) else "new")
        return 0
    if action in {"new", "clear"}:
        removed = store.clear(workspace)
        print("Session cleared." if removed else "No remembered session.")
        return 0
    raise ValueError(f"unknown session action: {action}")


__all__ = ["run_session_command"]
