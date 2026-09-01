from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gpt.core.paths import WebGPTPaths


def workspace_key(workspace: Path) -> str:
    resolved = str(workspace.expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    return f"{workspace.name or 'root'}-{digest}"


@dataclass(slots=True)
class SessionStore:
    path: Path

    @classmethod
    def default(cls, paths: WebGPTPaths | None = None) -> SessionStore:
        locations = (paths or WebGPTPaths.discover()).ensure()
        return cls(locations.sessions_dir / "index.json")

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): value
            for key, value in raw.items()
            if isinstance(value, dict)
        }

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, self.path)

    def get(self, workspace: Path) -> str | None:
        entry = self._read().get(workspace_key(workspace))
        value = entry.get("session_id") if entry else None
        return value if isinstance(value, str) and value else None

    def remember(self, workspace: Path, session_id: str) -> None:
        data = self._read()
        data[workspace_key(workspace)] = {
            "session_id": session_id,
            "workspace": str(workspace.expanduser().resolve()),
            "updated_at": int(time.time()),
        }
        self._write(data)

    def clear(self, workspace: Path) -> bool:
        data = self._read()
        removed = data.pop(workspace_key(workspace), None) is not None
        if removed:
            self._write(data)
        return removed

    def list(self) -> list[dict[str, Any]]:
        values = list(self._read().values())
        values.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
        return values


__all__ = ["SessionStore", "workspace_key"]
