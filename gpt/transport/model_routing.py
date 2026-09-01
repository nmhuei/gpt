from __future__ import annotations

import json
import os
from dataclasses import dataclass

from gpt.types import SendRequest

_MODEL_ALIAS_ENV = "WEBGPT_MODEL_ALIAS"
_MODEL_FALLBACK_ENV = "WEBGPT_MODEL_FALLBACK"
_MODEL_FALLBACK_POLICIES = ("warn", "retry-once")


@dataclass(frozen=True)
class ModelRoute:
    """Explicit requested-model -> upstream ChatGPT model route."""

    slug: str
    effort: str | None = None


def parse_model_alias_env(raw: str | None) -> dict[str, ModelRoute]:
    """Parse WEBGPT_MODEL_ALIAS in JSON-object or comma pair-list form."""
    if raw is None:
        return {}
    text = raw.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{_MODEL_ALIAS_ENV} is not valid JSON: {exc}") from exc
        if not isinstance(decoded, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in decoded.items()
        ):
            raise ValueError(f"{_MODEL_ALIAS_ENV} JSON must map strings to strings.")
        entries = list(decoded.items())
    else:
        entries = []
        for chunk in text.split(","):
            entry = chunk.strip()
            if not entry:
                continue
            key, sep, value = entry.partition("=")
            if not sep or not key.strip() or not value.strip():
                raise ValueError(
                    f"{_MODEL_ALIAS_ENV} pair {entry!r} must be 'from=to[:effort]'."
                )
            entries.append((key, value))

    routes: dict[str, ModelRoute] = {}
    for key, value in entries:
        slug, effort_sep, effort = value.strip().partition(":")
        slug = slug.strip()
        effort = effort.strip() if effort_sep else ""
        if not slug:
            raise ValueError(
                f"{_MODEL_ALIAS_ENV} value {value!r} needs a slug before ':'."
            )
        if effort_sep and not effort:
            raise ValueError(f"{_MODEL_ALIAS_ENV} value {value!r} has an empty effort.")
        routes[key.casefold().strip()] = ModelRoute(
            slug=slug,
            effort=effort or None,
        )
    return routes


def model_route_for(requested: str | None) -> ModelRoute | None:
    mapping = parse_model_alias_env(os.environ.get(_MODEL_ALIAS_ENV))
    if not mapping or not requested:
        return None
    return mapping.get(requested.casefold().strip())


def upstream_fconv_model(request: SendRequest) -> str | None:
    """Exact explicit model the f/conversation payload will request."""
    model = request.model.id if request.model and request.model.id else None
    model = model or (request.model.label if request.model else None)
    if not model or model == "auto":
        return None
    route = model_route_for(model)
    return route.slug if route is not None else model


def parse_model_fallback_env(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return "warn"
    policy = text.casefold()
    if policy not in _MODEL_FALLBACK_POLICIES:
        raise ValueError(
            f"{_MODEL_FALLBACK_ENV} must be "
            f"{'|'.join(_MODEL_FALLBACK_POLICIES)}, got {raw!r}."
        )
    return policy


def model_fallback_policy() -> str:
    return parse_model_fallback_env(os.environ.get(_MODEL_FALLBACK_ENV))


__all__ = [
    "ModelRoute",
    "model_fallback_policy",
    "model_route_for",
    "parse_model_alias_env",
    "parse_model_fallback_env",
    "upstream_fconv_model",
]
