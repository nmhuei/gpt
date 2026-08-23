from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PROFILE_DIR = Path.home() / "Downloads" / "webgpt" / "cloak-profile"
DEFAULT_CDP_PORT = 9222
DEFAULT_API_PORT = 8000
DEFAULT_MODEL = "gpt-5-5-thinking"
DEFAULT_EFFORT = "high"
DEFAULT_MAX_WORKERS = 3


@dataclass
class AppConfig:
    """Unified application settings and credential store."""

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


def load_config(env_file: str | Path | None = None) -> AppConfig:
    """Load configuration from .env file or environment variables."""
    target = Path(env_file) if env_file else Path.cwd() / ".env"
    
    email = os.environ.get("CHATGPT_EMAIL")
    password = os.environ.get("CHATGPT_PASSWORD")
    totp_key = os.environ.get("CHATGPT_TOTP_KEY")
    cdp_port = _parse_int(os.environ.get("CDP_PORT"), DEFAULT_CDP_PORT)
    api_port = _parse_int(os.environ.get("API_PORT"), DEFAULT_API_PORT)
    headless = _parse_bool(os.environ.get("BROWSER_HEADLESS"), True)
    profile_str = os.environ.get("PROFILE_DIR")
    profile_dir = Path(profile_str) if profile_str else DEFAULT_PROFILE_DIR
    default_model = os.environ.get("DEFAULT_MODEL", DEFAULT_MODEL)
    default_effort = os.environ.get("DEFAULT_EFFORT", DEFAULT_EFFORT)
    max_workers = _parse_int(os.environ.get("MAX_WORKERS"), DEFAULT_MAX_WORKERS)

    if target.exists():
        try:
            content = target.read_text(encoding="utf-8").strip()
            # Check for legacy pipe format: EMAIL|PASSWORD|TOTP
            lines = [line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")]
            if len(lines) == 1 and "|" in lines[0] and "=" not in lines[0]:
                parts = lines[0].split("|")
                if len(parts) >= 1 and parts[0]:
                    email = parts[0].strip()
                if len(parts) >= 2 and parts[1]:
                    password = parts[1].strip()
                if len(parts) >= 3 and parts[2]:
                    totp_key = parts[2].strip()
            else:
                for line in lines:
                    if "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip("'\"")
                        if key == "CHATGPT_EMAIL":
                            email = val
                        elif key == "CHATGPT_PASSWORD":
                            password = val
                        elif key == "CHATGPT_TOTP_KEY":
                            totp_key = val
                        elif key == "CDP_PORT":
                            cdp_port = _parse_int(val, DEFAULT_CDP_PORT)
                        elif key == "API_PORT":
                            api_port = _parse_int(val, DEFAULT_API_PORT)
                        elif key == "BROWSER_HEADLESS":
                            headless = _parse_bool(val, True)
                        elif key == "PROFILE_DIR":
                            profile_dir = Path(val)
                        elif key == "DEFAULT_MODEL":
                            default_model = val
                        elif key == "DEFAULT_EFFORT":
                            default_effort = val
                        elif key == "MAX_WORKERS":
                            max_workers = _parse_int(val, DEFAULT_MAX_WORKERS)
        except Exception:
            pass

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
