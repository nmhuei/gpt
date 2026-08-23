"""Unified five-tier PCAP/log analysis orchestrator."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pcap_analyzer.tier1_input.reader import PcapReader
from pcap_analyzer.tier2_zeek.normalizer import ZeekNormalizer
from pcap_analyzer.tier3_detection.rita import RitaDetector
from pcap_analyzer.tier3_detection.suricata import SuricataDetector
from pcap_analyzer.tier4_mitre.mapper import MitreMapper
from pcap_analyzer.tier5_reporting.reporter import Reporter
from pcap_analyzer.types import AnalysisReport, ConnRecord


class PcapAnalysisPipeline:
    def __init__(self, *, beacon_threshold: float = 0.65) -> None:
        self.reader = PcapReader()
        self.normalizer = ZeekNormalizer()
        self.suricata = SuricataDetector()
        self.rita = RitaDetector(threshold=beacon_threshold)
        self.mapper = MitreMapper()
        self.reporter = Reporter()

    def run(
        self, pcap_path: str | Path | None = None, *, zeek_logs: Mapping[str, str | Path] | None = None,
        eve_path: str | Path | None = None, expected_sha256: str | None = None,
    ) -> AnalysisReport:
        metadata = self.reader.read(pcap_path, expected_sha256) if pcap_path else None
        normalized: dict[str, list[object]] = {}
        for log_type, path in (zeek_logs or {}).items():
            normalized[log_type] = self.normalizer.parse(path, log_type)
        alerts = self.suricata.parse(eve_path) if eve_path else []
        connections = [item for items in normalized.values() for item in items if isinstance(item, ConnRecord)]
        scores = self.rita.detect(connections)
        findings = self.mapper.map(alerts, scores)
        return self.reporter.build_report(AnalysisReport(
            metadata=metadata, findings=findings, alerts=alerts, beacon_scores=scores,
        ))

    def report_markdown(self, report: AnalysisReport) -> str:
        return self.reporter.to_markdown(report)

    def report_json(self, report: AnalysisReport) -> str:
        return self.reporter.to_json(report)

    analyze = run


Pipeline = PcapAnalysisPipeline
