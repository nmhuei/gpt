"""Timeline, kill-chain, Markdown, and JSON report generation."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from pcap_analyzer.types import AnalysisReport, Finding, TimelineEvent

_KILL_CHAIN = ["Reconnaissance", "Resource Development", "Initial Access", "Execution", "Persistence", "Privilege Escalation", "Defense Evasion", "Credential Access", "Discovery", "Lateral Movement", "Collection", "Command and Control", "Exfiltration", "Impact"]


class Reporter:
    def timeline(self, findings: list[Finding]) -> list[TimelineEvent]:
        return sorted([
            TimelineEvent(timestamp=finding.timestamp or 0.0, event_type=finding.tactic,
                          summary=finding.title, finding=finding, evidence=finding.evidence)
            for finding in findings
        ], key=lambda event: event.timestamp)

    def reconstruct_kill_chain(self, findings: list[Finding]) -> list[str]:
        present = {finding.tactic for finding in findings}
        return [stage for stage in _KILL_CHAIN if stage in present]

    def build_report(self, report: AnalysisReport) -> AnalysisReport:
        report.timeline = self.timeline(report.findings)
        report.kill_chain = self.reconstruct_kill_chain(report.findings)
        return report

    def to_json(self, report: AnalysisReport, *, indent: int | None = 2) -> str:
        return json.dumps(asdict(self.build_report(report)), indent=indent, sort_keys=True, default=str)

    def to_markdown(self, report: AnalysisReport) -> str:
        report = self.build_report(report)
        lines = ["# PCAP Analysis Report", "", "## Summary", ""]
        if report.metadata:
            lines.extend([
                f"- Packets: {report.metadata.packet_count}", f"- Duration: {report.metadata.duration_seconds:.3f}s",
                f"- SHA-256: `{report.metadata.sha256}`", "",
            ])
        lines.extend([f"- Findings: {len(report.findings)}", "", "## Kill Chain", ""])
        lines.extend([f"- {stage}" for stage in report.kill_chain] or ["- No mapped ATT&CK stages"])
        lines.extend(["", "## Findings", ""])
        for finding in report.findings:
            lines.append(f"- **{finding.title}** — {finding.tactic} / {finding.technique_id} (confidence {finding.confidence:.0%})")
        lines.extend(["", "## Timeline", ""])
        for event in report.timeline:
            lines.append(f"- {event.timestamp:.3f}: {event.summary}")
        return "\n".join(lines) + "\n"

    def synthesize(self, report: AnalysisReport) -> dict[str, Any]:
        return {"markdown": self.to_markdown(report), "json": self.to_json(report)}

    def generate_report(self, report: AnalysisReport, format: str = "markdown") -> str:
        """Generate a serialized report; accepted formats are ``markdown`` and ``json``."""
        if format.lower() == "markdown":
            return self.to_markdown(report)
        if format.lower() == "json":
            return self.to_json(report)
        raise ValueError(f"Unsupported report format: {format}")
