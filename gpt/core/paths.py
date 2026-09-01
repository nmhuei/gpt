from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _xdg(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else fallback


@dataclass(frozen=True, slots=True)
class WebGPTPaths:
    """Canonical XDG locations for new WebGPT user-facing state.

    Legacy paths remain readable by the compatibility layer; new code should
    use this object instead of inventing another ~/.local/share/webgpt path.
    """

    config_home: Path
    data_home: Path
    state_home: Path
    cache_home: Path
    runtime_home: Path

    @classmethod
    def discover(cls) -> WebGPTPaths:
        home = Path.home()
        config_base = _xdg("XDG_CONFIG_HOME", home / ".config")
        data_base = _xdg("XDG_DATA_HOME", home / ".local" / "share")
        state_base = _xdg("XDG_STATE_HOME", home / ".local" / "state")
        cache_base = _xdg("XDG_CACHE_HOME", home / ".cache")
        runtime_raw = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        runtime_base = (
            Path(runtime_raw).expanduser()
            if runtime_raw
            else state_base / "webgpt" / "run"
        )
        return cls(
            config_home=config_base / "webgpt",
            data_home=data_base / "webgpt",
            state_home=state_base / "webgpt",
            cache_home=cache_base / "webgpt",
            runtime_home=runtime_base / "webgpt" if runtime_raw else runtime_base,
        )

    @property
    def config_file(self) -> Path:
        return self.config_home / "config.toml"

    @property
    def accounts_file(self) -> Path:
        return self.config_home / "accounts.json"

    @property
    def sessions_dir(self) -> Path:
        return self.state_home / "sessions"

    @property
    def logs_dir(self) -> Path:
        return self.state_home / "logs"

    @property
    def traces_dir(self) -> Path:
        return self.state_home / "traces"

    @property
    def profiles_dir(self) -> Path:
        # Existing browser profiles are already stored under data/.
        return self.data_home / "profiles"

    @property
    def gateway_socket(self) -> Path:
        return self.runtime_home / "gateway.sock"

    def ensure(self) -> WebGPTPaths:
        for directory in (
            self.config_home,
            self.data_home,
            self.state_home,
            self.cache_home,
            self.runtime_home,
            self.sessions_dir,
            self.logs_dir,
            self.traces_dir,
            self.profiles_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        return self


__all__ = ["WebGPTPaths"]
