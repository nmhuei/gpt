from __future__ import annotations

import os
import stat

import pytest

from gpt.runtime_paths import (
    assert_runtime_path,
    ensure_runtime_layout,
    free_anonymous_gateway_lock,
)


def test_runtime_path_rejects_escape(tmp_path):
    root = tmp_path / "webgpt"
    ensure_runtime_layout(root)

    assert assert_runtime_path("runs/smoke/a.json", root).is_relative_to(root)
    with pytest.raises(ValueError):
        assert_runtime_path(tmp_path / "outside.json", root)


def test_free_anonymous_gateway_lock_is_exclusive_and_private(tmp_path):
    root = tmp_path / "webgpt"

    with free_anonymous_gateway_lock(root) as lock_path:
        assert lock_path.parent == root / "tmp"
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        assert lock_path.read_text(encoding="utf-8") == f"pid={os.getpid()}\n"
        with pytest.raises(RuntimeError, match="already active"), free_anonymous_gateway_lock(root):
            pass

    with free_anonymous_gateway_lock(root):
        pass
