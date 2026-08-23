from __future__ import annotations

import hashlib
import json
import struct

import pytest

from pcap_analyzer.benchmarks.datasets import CTU13, MAWI
from pcap_analyzer.benchmarks.evaluator import evaluate
from pcap_analyzer.pipeline import PcapAnalysisPipeline
from pcap_analyzer.tier1_input.reader import compute_sha256, read_pcap
from pcap_analyzer.tier2_zeek.normalizer import ZeekNormalizer
from pcap_analyzer.tier3_detection.rita import RitaDetector
from pcap_analyzer.tier3_detection.suricata import SuricataDetector
from pcap_analyzer.tier4_mitre.mapper import MitreMapper
from pcap_analyzer.tier5_reporting.reporter import Reporter
from pcap_analyzer.types import ConnRecord


def _pcap() -> bytes:
    header = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65_535, 1)
    packet = b"\x00" * 4
    return header + struct.pack("<IIII", 100, 100_000, len(packet), len(packet)) + packet + struct.pack("<IIII", 103, 0, len(packet), len(packet)) + packet


def test_tier1_hash_packet_count_and_duration(tmp_path):
    capture = tmp_path / "sample.pcap"
    capture.write_bytes(_pcap())
    metadata = read_pcap(capture, hashlib.sha256(_pcap()).hexdigest())
    assert metadata.packet_count == 2
    assert metadata.duration_seconds == pytest.approx(2.9)
    assert metadata.sha256 == compute_sha256(capture)
    with pytest.raises(ValueError, match="SHA-256"):
        read_pcap(capture, "0" * 64)


def test_tier2_normalizes_zeek_tsv_and_json(tmp_path):
    log = tmp_path / "conn.log"
    log.write_text("#separator \\x09\n#fields\tts\tuid\tid.orig_h\tid.orig_p\tid.resp_h\tid.resp_p\tproto\tduration\torig_bytes\tresp_bytes\n1.5\tC1\t10.0.0.1\t4444\t8.8.8.8\t443\ttcp\t1.2\t10\t20\n")
    connection = ZeekNormalizer().parse(log)[0]
    assert (connection.source_ip, connection.dest_port, connection.orig_bytes) == ("10.0.0.1", 443, 10)
    rows = ZeekNormalizer().normalize([{"ts": 2, "uid": "D1", "query": "example.org", "answers": ["1.2.3.4"]}], "dns")
    assert rows[0].answers == ["1.2.3.4"]


def test_tier3_parses_suricata_and_scores_regular_beacon(tmp_path):
    eve = tmp_path / "eve.json"
    eve.write_text(json.dumps({"event_type": "alert", "timestamp": 1, "src_ip": "10.0.0.1", "dest_ip": "1.1.1.1", "alert": {"signature_id": 202, "signature": "ET C2 beacon", "severity": 1, "metadata": {"mitre_tactic_id": "TA0011", "mitre_technique_id": "T1071"}}}) + "\n")
    alerts = SuricataDetector().parse(eve)
    assert alerts[0].signature_id == 202 and alerts[0].mitre_techniques == ["T1071"]
    flows = [ConnRecord(timestamp=float(index * 60), source_ip="10.0.0.1", dest_ip="1.1.1.1", dest_port=443, protocol="tcp", orig_bytes=100, resp_bytes=100) for index in range(5)]
    score = RitaDetector().detect(flows)[0]
    assert score.regularity == 1 and score.score >= 0.65


def test_tier4_maps_metadata_and_beacons(tmp_path):
    eve = tmp_path / "eve.json"
    eve.write_text(json.dumps({"event_type": "alert", "timestamp": 1, "alert": {"signature_id": 1, "signature": "test", "metadata": {"mitre_tactic_id": "Command and Control", "mitre_technique_id": "T1071"}}}) + "\n")
    findings = MitreMapper().map_alert(SuricataDetector().parse(eve)[0])
    assert findings[0].technique_id == "T1071"
    assert findings[0].confidence > 0.5


def test_tier5_report_and_complete_pipeline(tmp_path):
    capture, conn, eve = tmp_path / "sample.pcap", tmp_path / "conn.log", tmp_path / "eve.json"
    capture.write_bytes(_pcap())
    conn.write_text("#separator \\x09\n#fields\tts\tid.orig_h\tid.resp_h\tid.resp_p\tproto\torig_bytes\tresp_bytes\n" + "\n".join(f"{i * 60}\t10.0.0.1\t1.1.1.1\t443\ttcp\t100\t100" for i in range(5)) + "\n")
    eve.write_text(json.dumps({"event_type": "alert", "timestamp": 1, "alert": {"signature_id": 1, "signature": "ET C2 beacon", "metadata": {"mitre_tactic_id": "Command and Control", "mitre_technique_id": "T1071"}}}) + "\n")
    report = PcapAnalysisPipeline().run(capture, zeek_logs={"conn": conn}, eve_path=eve)
    rendered = Reporter().to_markdown(report)
    assert report.metadata and report.metadata.packet_count == 2
    assert report.timeline == sorted(report.timeline, key=lambda event: event.timestamp)
    assert "Command and Control" in report.kill_chain and "# PCAP Analysis Report" in rendered
    assert json.loads(Reporter().to_json(report))["findings"]


def test_ground_truth_metrics_cover_malicious_and_benign_baselines():
    metrics = evaluate({"ctu13-c2-001", "noise"}, CTU13["ground_truth"], total_events=CTU13["total_events"], baseline_alerts=CTU13["baseline_alerts"])
    assert metrics.precision == metrics.recall == metrics.f1 == 0.5
    assert metrics.false_positive_rate > 0 and metrics.alert_reduction_rate == 0.8
    benign = evaluate({"false-alert"}, MAWI["ground_truth"], total_events=MAWI["total_events"])
    assert benign.true_positives == 0 and benign.false_positive_rate > 0
