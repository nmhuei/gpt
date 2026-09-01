from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

DEFAULT_WEBGPT_DIR = Path.home() / ".local" / "share" / "webgpt"
DEFAULT_PROFILE_DIR = DEFAULT_WEBGPT_DIR / "profile"
DEFAULT_BRAVE_PROFILE_DIR = DEFAULT_WEBGPT_DIR / "brave-profile"
DEFAULT_CLOAK_PROFILE_DIR = DEFAULT_WEBGPT_DIR / "cloak-profile"
DEFAULT_ARTIFACTS_DIR = DEFAULT_WEBGPT_DIR / "reverse"
DEFAULT_TMP_DIR = DEFAULT_WEBGPT_DIR / "tmp"



def ensure_profile_dir(profile_path: Path | str | None = None) -> Path:
    """Ensure that the persistent profile directory exists with safe permissions (0700)."""
    target = Path(profile_path) if profile_path else DEFAULT_PROFILE_DIR
    target = target.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass
    # Never remove Chromium Singleton files here. They may belong to an active
    # browser using this profile; browser startup must surface that conflict
    # instead of risking another process's session integrity.
    return target


def create_ephemeral_profile() -> tuple[Path, Callable[[], None]]:
    """Create a temporary directory for clean, anonymous browser testing."""
    DEFAULT_TMP_DIR.mkdir(parents=True, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix="webgpt-anon-", dir=str(DEFAULT_TMP_DIR))
    path = Path(temp_dir).resolve()
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass

    def cleanup() -> None:
        shutil.rmtree(path, ignore_errors=True)

    return path, cleanup
