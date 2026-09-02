from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - supported Python 3.10.
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

from .paths import WebGPTPaths

DEFAULT_BASE_URL = "http://127.0.0.1:18000"
DEFAULT_MODEL = "gpt-5-6-thinking"


def _truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed) if minimum is not None else parsed


def _float(value: Any, default: float, *, minimum: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed) if minimum is not None else parsed


def _nested(data: Mapping[str, Any], *path: str) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _find_project_config(workspace: Path) -> Path | None:
    current = workspace.resolve()
    for directory in (current, *current.parents):
        candidate = directory / "webgpt.toml"
        if candidate.is_file():
            return candidate
        # A git root is the natural configuration boundary.
        if (directory / ".git").exists():
            break
    return None


@dataclass(frozen=True, slots=True)
class Settings:
    """Typed settings for the user-facing runtime.

    Precedence:
        explicit CLI overrides > environment > project webgpt.toml
        > user ~/.config/webgpt/config.toml > defaults.

    Old modules can keep their historical env reads during migration; new
    user-facing code should receive one Settings instance instead.
    """

    workspace: Path = field(default_factory=lambda: Path.cwd().resolve())

    # Agent
    model: str = DEFAULT_MODEL
    max_rounds: int = 20
    max_tokens: int = 8192
    timeout_seconds: float = 180.0
    verify: str = "auto"

    # Gateway
    base_url: str = DEFAULT_BASE_URL
    api_key: str = "sk-webgpt-local"
    transport: str = "hybrid"
    workers: int = 8

    # Account/features/usage
    account: str = "personal"
    image_upload: bool = False
    fconv_resume: bool = False
    usage_poll_seconds: float = 0.0

    # Presentation
    verbosity: int = 0

    # Diagnostics only
    user_config_file: Path | None = None
    project_config_file: Path | None = None

    @classmethod
    def load(
        cls,
        *,
        workspace: Path | str | None = None,
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
        paths: WebGPTPaths | None = None,
    ) -> Settings:
        env = os.environ if environ is None else environ
        work = Path(workspace or Path.cwd()).expanduser().resolve()
        discovered = paths or WebGPTPaths.discover()
        user_file = discovered.config_file
        project_file = _find_project_config(work)
        user = _read_toml(user_file)
        project = _read_toml(project_file) if project_file else {}

        def cfg(section: str, key: str, default: Any) -> Any:
            user_value = _nested(user, section, key)
            project_value = _nested(project, section, key)
            value = default
            if user_value is not None:
                value = user_value
            if project_value is not None:
                value = project_value
            return value

        values: dict[str, Any] = {
            "workspace": work,
            "model": cfg("agent", "model", DEFAULT_MODEL),
            "max_rounds": cfg("agent", "max_rounds", 20),
            "max_tokens": cfg("agent", "max_tokens", 8192),
            "timeout_seconds": cfg("agent", "timeout", 180.0),
            "verify": cfg("agent", "verify", "auto"),
            "base_url": cfg("gateway", "base_url", DEFAULT_BASE_URL),
            "api_key": cfg("gateway", "api_key", "sk-webgpt-local"),
            "transport": cfg("gateway", "transport", "hybrid"),
            "workers": cfg("gateway", "workers", 8),
            "account": cfg("account", "default", "personal"),
            "image_upload": cfg("features", "image_upload", False),
            "fconv_resume": cfg("features", "fconv_resume", False),
            "usage_poll_seconds": cfg("usage", "poll_seconds", 0.0),
            "verbosity": cfg("output", "verbosity", 0),
            "user_config_file": user_file,
            "project_config_file": project_file,
        }

        env_map = {
            "model": "WEBGPT_DIRECT_MODEL",
            "max_rounds": "WEBGPT_MAX_ROUNDS",
            "max_tokens": "WEBGPT_MAX_OUTPUT_TOKENS",
            "timeout_seconds": "WEBGPT_GENERATION_TIMEOUT",
            "verify": "WEBGPT_VERIFY",
            "base_url": "WEBGPT_GATEWAY_URL",
            "api_key": "WEBGPT_API_KEY",
            "transport": "WEBGPT_TRANSPORT",
            "workers": "WEBGPT_MAX_WORKERS",
            "account": "WEBGPT_DEFAULT_ACCOUNT",
            "image_upload": "WEBGPT_IMAGE_UPLOAD_WEB",
            "fconv_resume": "WEBGPT_FCONV_RESUME",
            "usage_poll_seconds": "WEBGPT_USAGE_POLL_SECONDS",
            "verbosity": "WEBGPT_VERBOSITY",
        }
        # Compatibility with the API-client ecosystem.
        compatibility = {
            "base_url": "ANTHROPIC_BASE_URL",
            "api_key": "ANTHROPIC_API_KEY",
        }
        for field_name, env_name in env_map.items():
            raw = env.get(env_name)
            if raw is not None and str(raw).strip():
                values[field_name] = raw
        for field_name, env_name in compatibility.items():
            raw = env.get(env_name)
            if raw is not None and str(raw).strip():
                values[field_name] = raw

        if overrides:
            for key, value in overrides.items():
                if value is not None and key in values:
                    values[key] = value

        values["model"] = str(values["model"]).strip() or DEFAULT_MODEL
        values["base_url"] = str(values["base_url"]).strip() or DEFAULT_BASE_URL
        values["api_key"] = str(values["api_key"])
        values["account"] = str(values["account"]).strip() or "personal"
        values["transport"] = str(values["transport"]).strip() or "hybrid"
        verify = str(values["verify"]).strip().casefold()
        values["verify"] = verify if verify in {"auto", "quick", "full", "off"} else "auto"
        values["max_rounds"] = _int(values["max_rounds"], 20, minimum=1)
        values["max_tokens"] = _int(values["max_tokens"], 8192, minimum=256)
        values["workers"] = _int(values["workers"], 8, minimum=1)
        values["timeout_seconds"] = _float(values["timeout_seconds"], 180.0, minimum=1.0)
        values["usage_poll_seconds"] = _float(
            values["usage_poll_seconds"], 0.0, minimum=0.0
        )
        values["verbosity"] = _int(values["verbosity"], 0, minimum=0)
        values["image_upload"] = _truthy(values["image_upload"], False)
        values["fconv_resume"] = _truthy(values["fconv_resume"], False)
        return cls(**values)

    def with_overrides(self, **values: Any) -> Settings:
        clean = {key: value for key, value in values.items() if value is not None}
        return replace(self, **clean)

    def public_dict(self) -> dict[str, Any]:
        return {
            "agent": {
                "model": self.model,
                "max_rounds": self.max_rounds,
                "max_tokens": self.max_tokens,
                "timeout": self.timeout_seconds,
                "verify": self.verify,
            },
            "gateway": {
                "base_url": self.base_url,
                "transport": self.transport,
                "workers": self.workers,
            },
            "account": {"default": self.account},
            "features": {
                "image_upload": self.image_upload,
                "fconv_resume": self.fconv_resume,
            },
            "usage": {"poll_seconds": self.usage_poll_seconds},
            "output": {"verbosity": self.verbosity},
        }


__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "Settings"]
