from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from gpt.profile import DEFAULT_ARTIFACTS_DIR
from gpt.types import ProtocolFinding


class ProtocolLedger:
    """Stores observed protocol behaviors, confidence ratings, and experiment traces."""

    def __init__(self, ledger_file: Path | str | None = None):
        self.ledger_file = (
            Path(ledger_file)
            if ledger_file
            else DEFAULT_ARTIFACTS_DIR / "protocol_findings.json"
        )
        self.findings: dict[str, ProtocolFinding] = {}
        self.load()

    def load(self) -> None:
        if self.ledger_file.exists():
            try:
                data = json.loads(self.ledger_file.read_text(encoding="utf-8"))
                for k, v in data.items():
                    self.findings[k] = ProtocolFinding(**v)
            except Exception:
                pass

    def record_finding(
        self,
        name: str,
        hypothesis: str,
        experiment_id: str,
        confidence: str = "medium",
        contradicts: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        if name not in self.findings:
            self.findings[name] = ProtocolFinding(
                name=name,
                hypothesis=hypothesis,
                supporting_experiments=[],
                contradicting_experiments=[],
                confidence=confidence,  # type: ignore
                details=details or {},
            )

        finding = self.findings[name]
        finding.hypothesis = hypothesis
        if contradicts:
            if experiment_id not in finding.contradicting_experiments:
                finding.contradicting_experiments.append(experiment_id)
        else:
            if experiment_id not in finding.supporting_experiments:
                finding.supporting_experiments.append(experiment_id)

        finding.confidence = confidence  # type: ignore
        if details:
            finding.details.update(details)
        self.save()

    def save(self) -> None:
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.ledger_file.parent, 0o700)
        except OSError:
            pass
        raw = {k: asdict(v) for k, v in self.findings.items()}
        self.ledger_file.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        try:
            os.chmod(self.ledger_file, 0o600)
        except OSError:
            pass
