from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gpt.profile import DEFAULT_ARTIFACTS_DIR
from gpt.reverse.redact import default_redactor


class ArtifactManager:
    """Stores experiment and run artifacts with automatic redaction and safe permissions."""

    def __init__(self, base_dir: Path | str | None = None):
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_ARTIFACTS_DIR
        self.base_dir = self.base_dir.expanduser().resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.base_dir, 0o700)
        except OSError:
            pass

    def create_run_dir(self, experiment_id: str) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe_id = "".join(c for c in experiment_id if c.isalnum() or c in "-_") or "experiment"
        run_name = f"run-{ts}-{safe_id}-{uuid.uuid4().hex[:6]}"
        run_dir = self.base_dir / run_name
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(run_dir, 0o700)
        except OSError:
            pass
        return run_dir

    def save_json(
        self,
        run_dir: Path,
        filename: str,
        data: Any,
        redact: bool = True,
        normalize_ids: bool = False,
    ) -> Path:
        target = self._target(run_dir, filename)
        processed = (
            default_redactor.redact_json(data, normalize_ids=normalize_ids)
            if redact
            else data
        )
        content = json.dumps(processed, indent=2, ensure_ascii=False)
        target.write_text(content, encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target

    def save_ndjson(
        self,
        run_dir: Path,
        filename: str,
        items: list[dict[str, Any]],
        redact: bool = True,
        normalize_ids: bool = False,
    ) -> Path:
        target = self._target(run_dir, filename)
        lines = []
        for item in items:
            processed = (
                default_redactor.redact_event(item, normalize_ids=normalize_ids)
                if redact
                else item
            )
            lines.append(json.dumps(processed, ensure_ascii=False))
        target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target

    def save_text(
        self, run_dir: Path, filename: str, content: str, redact: bool = True
    ) -> Path:
        target = self._target(run_dir, filename)
        target.write_text(
            default_redactor.redact_string(content) if redact else content,
            encoding="utf-8",
        )
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target

    def save_bytes(self, run_dir: Path, filename: str, data: bytes) -> Path:
        target = self._target(run_dir, filename)
        target.write_bytes(data)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target

    @staticmethod
    def _target(run_dir: Path, filename: str) -> Path:
        if Path(filename).name != filename:
            raise ValueError("Artifact filename must not contain a path.")
        return run_dir / filename
