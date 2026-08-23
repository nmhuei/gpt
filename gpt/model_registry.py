from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelResolution:
    requested: str
    ui_label: str | None
    response_model: str


class ModelRegistry:
    """Explicit request aliases; UI discovery remains the source of availability."""

    default_alias = "chatgpt-web"

    def __init__(self, aliases: Mapping[str, str] | None = None):
        self._aliases = {
            key.casefold().strip(): value.strip()
            for key, value in (aliases or {}).items()
            if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip()
        }

    def resolve(self, requested: str) -> ModelResolution:
        normalized = requested.casefold().strip()
        if normalized in {self.default_alias, "default"}:
            return ModelResolution(
                requested=requested,
                ui_label=None,
                response_model=requested if requested != "default" else self.default_alias,
            )
        # Anthropic-compatible clients (including Claude Code) validate their
        # own Claude-shaped model identifiers before issuing a request. These
        # identifiers name the client protocol contract, not a ChatGPT Web UI
        # option. Keep the browser's current model unless an operator installs
        # an explicit alias; never infer a matching ChatGPT model or account tier.
        if normalized.startswith("claude-") and normalized not in self._aliases:
            return ModelResolution(
                requested=requested,
                ui_label=None,
                response_model=requested,
            )
        ui_label = self._aliases.get(normalized, requested)
        return ModelResolution(
            requested=requested,
            ui_label=ui_label,
            response_model=requested,
        )


def load_model_aliases(path: Path | str | None) -> dict[str, str]:
    """Load an explicit local alias map without guessing provider model names."""
    if path is None:
        return {}
    source = Path(path).expanduser()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load model aliases from {source}: {exc}") from exc
    aliases = raw.get("model_aliases", raw) if isinstance(raw, dict) else None
    if not isinstance(aliases, dict):
        raise ValueError("Model alias file must be an object or contain a model_aliases object.")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in aliases.items()):
        raise ValueError("Every model alias and UI label must be a string.")
    return dict(aliases)
