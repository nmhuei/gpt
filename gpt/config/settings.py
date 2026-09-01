from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PROFILE_DIR = Path.home() / ".local" / "share" / "webgpt" / "cloak-profile"
DEFAULT_CDP_PORT = 9222
DEFAULT_API_PORT = 8000
DEFAULT_MODEL = "gpt-5-5-thinking"
DEFAULT_EFFORT = "high"
DEFAULT_MAX_WORKERS = 3


@dataclass
class AppConfig:
    """Legacy browser/bootstrap credential settings.

    User-facing agent/gateway configuration lives in ``gpt.core.Settings``.
    This class remains for login/CDP/profile compatibility only.
    """

    email: str | None = None
    password: str | None = None
    totp_key: str | None = None
    cdp_port: int = DEFAULT_CDP_PORT
    api_port: int = DEFAULT_API_PORT
    headless: bool = True
    profile_dir: Path = field(default_factory=lambda: DEFAULT_PROFILE_DIR)
    default_model: str = DEFAULT_MODEL
    default_effort: str = DEFAULT_EFFORT
    max_workers: int = DEFAULT_MAX_WORKERS

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.cdp_port}"

    def masked_summary(self) -> str:
        """Safe printable summary with credentials masked."""
        masked_email = self.email if self.email else "<none>"
        has_pwd = bool(self.password)
        has_totp = bool(self.totp_key)
        return (
            f"AppConfig(email={masked_email}, password_set={has_pwd}, totp_set={has_totp}, "
            f"cdp_port={self.cdp_port}, api_port={self.api_port}, headless={self.headless}, "
            f"default_model={self.default_model}, default_effort={self.default_effort}, "
            f"max_workers={self.max_workers})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "cdp_port": self.cdp_port,
            "api_port": self.api_port,
            "headless": self.headless,
            "profile_dir": str(self.profile_dir),
            "default_model": self.default_model,
            "default_effort": self.default_effort,
            "max_workers": self.max_workers,
        }


def _parse_bool(val: str | None, default: bool = True) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes", "on")


def _parse_int(val: str | None, default: int) -> int:
    if not val:
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


def _read_env_file(target: Path) -> dict[str, str]:
    """Parse a .env file (or legacy single-line pipe format) into key/value pairs."""
    values: dict[str, str] = {}
    try:
        content = target.read_text(encoding="utf-8").strip()
    except OSError:
        return values
    lines = [line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")]
    if len(lines) == 1 and "|" in lines[0] and "=" not in lines[0]:
        parts = lines[0].split("|")
        if len(parts) >= 1 and parts[0]:
            values["CHATGPT_EMAIL"] = parts[0].strip()
        if len(parts) >= 2 and parts[1]:
            values["CHATGPT_PASSWORD"] = parts[1].strip()
        if len(parts) >= 3 and parts[2]:
            values["CHATGPT_TOTP_KEY"] = parts[2].strip()
        return values
    for line in lines:
        if "=" in line:
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip("'\"")
    return values


def load_config(env_file: str | Path | None = None) -> AppConfig:
    """Load configuration.

    Precedence is ``environ > .env > default`` so a terminal can scope its own
    overrides (e.g. ``ANTHROPIC_BASE_URL`` for one shell only) without touching
    the shared project .env file.
    """
    target = Path(env_file) if env_file else Path.cwd() / ".env"
    file_values = _read_env_file(target) if target.exists() else {}

    def resolve(name: str) -> str | None:
        # Environment wins over the shared .env file on purpose.
        val = os.environ.get(name)
        if val is None:
            val = file_values.get(name)
        return val if val else None

    email = resolve("CHATGPT_EMAIL")
    password = resolve("CHATGPT_PASSWORD")
    totp_key = resolve("CHATGPT_TOTP_KEY")
    cdp_port = _parse_int(resolve("CDP_PORT"), DEFAULT_CDP_PORT)
    api_port = _parse_int(resolve("API_PORT"), DEFAULT_API_PORT)
    headless = _parse_bool(resolve("BROWSER_HEADLESS"), True)
    profile_str = resolve("PROFILE_DIR")
    profile_dir = Path(profile_str) if profile_str else DEFAULT_PROFILE_DIR
    default_model = resolve("DEFAULT_MODEL") or DEFAULT_MODEL
    default_effort = resolve("DEFAULT_EFFORT") or DEFAULT_EFFORT
    max_workers = _parse_int(resolve("MAX_WORKERS"), DEFAULT_MAX_WORKERS)

    return AppConfig(
        email=email,
        password=password,
        totp_key=totp_key,
        cdp_port=cdp_port,
        api_port=api_port,
        headless=headless,
        profile_dir=profile_dir,
        default_model=default_model,
        default_effort=default_effort,
        max_workers=max_workers,
    )


_GLOBAL_CONFIG: AppConfig | None = None


def get_config(env_file: str | Path | None = None, reload: bool = False) -> AppConfig:
    global _GLOBAL_CONFIG
    if _GLOBAL_CONFIG is None or reload:
        _GLOBAL_CONFIG = load_config(env_file)
    return _GLOBAL_CONFIG
