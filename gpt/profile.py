from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

DEFAULT_PROFILE_DIR = Path.home() / ".local" / "share" / "bqa" / "chatgpt-profile"
DEFAULT_BRAVE_PROFILE_DIR = (
    Path.home() / ".local" / "share" / "bqa" / "brave-chatgpt-profile"
)
DEFAULT_ARTIFACTS_DIR = Path.home() / ".local" / "share" / "bqa" / "webchat-reverse"


def ensure_profile_dir(profile_path: Path | str | None = None) -> Path:
    """Ensure that the persistent profile directory exists with safe permissions (0700)."""
    target = Path(profile_path) if profile_path else DEFAULT_PROFILE_DIR
    target = target.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass
    # Clean stale Chromium singleton locks
    for lock_file in target.glob("Singleton*"):
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass
    return target


def create_ephemeral_profile() -> tuple[Path, Callable[[], None]]:
    """Create a temporary directory for clean, anonymous browser testing."""
    temp_dir = tempfile.mkdtemp(prefix="bqa-chatgpt-anon-")
    path = Path(temp_dir).resolve()
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass

    def cleanup() -> None:
        shutil.rmtree(path, ignore_errors=True)

    return path, cleanup
