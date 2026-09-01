from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

from gpt.core.paths import WebGPTPaths
from gpt.core.settings import Settings

_ALLOWED = {
    "agent.model": str,
    "agent.max_rounds": int,
    "agent.max_tokens": int,
    "agent.timeout": float,
    "agent.verify": str,
    "gateway.base_url": str,
    "gateway.transport": str,
    "gateway.workers": int,
    "account.default": str,
    "features.image_upload": bool,
    "features.fconv_resume": bool,
    "usage.poll_seconds": float,
    "output.verbosity": int,
}


def _parse_value(key: str, raw: str) -> Any:
    expected = _ALLOWED[key]
    if expected is bool:
        lowered = raw.strip().casefold()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{key} expects true/false")
    if expected is int:
        return int(raw)
    if expected is float:
        return float(raw)
    return raw


def _load(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dump(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for section in (
        "agent",
        "gateway",
        "account",
        "features",
        "usage",
        "output",
    ):
        values = data.get(section)
        if not isinstance(values, dict) or not values:
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float)):
                rendered = str(value)
            else:
                rendered = _quote(str(value))
            lines.append(f"{key} = {rendered}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _set(path: Path, key: str, raw: str) -> None:
    if key not in _ALLOWED:
        raise ValueError(
            "unknown key; allowed: " + ", ".join(sorted(_ALLOWED))
        )
    section, field = key.split(".", 1)
    data = _load(path)
    values = data.setdefault(section, {})
    if not isinstance(values, dict):
        values = {}
        data[section] = values
    values[field] = _parse_value(key, raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(data), encoding="utf-8")


def run_config_command(
    settings: Settings,
    action: str,
    *,
    key: str | None = None,
    value: str | None = None,
) -> int:
    path = WebGPTPaths.discover().config_file
    if action == "path":
        print(path)
        return 0
    if action == "show":
        from .common import json_print

        json_print(settings.public_dict())
        print(f"\nuser config:    {settings.user_config_file}")
        print(f"project config: {settings.project_config_file or '<none>'}")
        return 0
    if action == "init":
        if path.exists():
            print(f"Config already exists: {path}")
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump(settings.public_dict()), encoding="utf-8")
        print(f"Created {path}")
        return 0
    if action == "set":
        if not key or value is None:
            raise ValueError("config set requires KEY VALUE")
        _set(path, key, value)
        print(f"{key} updated in {path}")
        return 0
    raise ValueError(f"unknown config action: {action}")


__all__ = ["run_config_command"]
