"""Evidence-preserving MITRE ATT&CK mapping for detection output."""

from __future__ import annotations

from collections.abc import Iterable

from pcap_analyzer.types import BeaconScore, Finding, RawAlert

_RULE_MAPPINGS = {
    "c2": ("Command and Control", "T1071", "Application Layer Protocol"),
    "command and control": ("Command and Control", "T1071", "Application Layer Protocol"),
    "beacon": ("Command and Control", "T1071.001", "Web Protocols"),
    "powershell": ("Execution", "T1059.001", "PowerShell"),
    "exploit": ("Initial Access", "T1190", "Exploit Public-Facing Application"),
    "ransomware": ("Impact", "T1486", "Data Encrypted for Impact"),
    "malware": ("Execution", "T1204.002", "Malicious File"),
    "scan": ("Discovery", "T1046", "Network Service Discovery"),
}


class MitreMapper:
    def map_alert(self, alert: RawAlert) -> list[Finding]:
        pairs = list(zip(alert.mitre_tactics, alert.mitre_techniques, strict=False))
        if not pairs and alert.mitre_techniques:
            pairs = [("", technique) for technique in alert.mitre_techniques]
        if not pairs:
            combined = f"{alert.signature} {alert.category}".lower()
            for term, mapping in _RULE_MAPPINGS.items():
                if term in combined:
                    tactic, technique, _name = mapping
                    pairs = [(tactic, technique)]
                    break
            else:
                pairs = [("Unknown", "T0000")]
        findings = []
        for tactic, technique in pairs:
            confidence = min(1.0, 0.55 + (4 - min(3, alert.severity)) * 0.1 + (0.15 if alert.mitre_techniques else 0))
            findings.append(Finding(
                title=alert.signature or "Suricata alert", tactic=tactic or "Unknown", technique_id=technique,
                confidence=round(confidence, 2), description=alert.category, severity=_severity(alert.severity),
                source=f"suricata:{alert.signature_id}", evidence=alert.evidence or [f"{alert.source_ip} -> {alert.dest_ip}"],
                timestamp=alert.timestamp,
            ))
        return findings

    def map_beacon(self, score: BeaconScore) -> Finding:
        return Finding(
            title="Probable command-and-control beacon", tactic="Command and Control", technique_id="T1071",
            confidence=round(score.score, 2), description="Statistically regular repeated network connection",
            severity="high" if score.score >= 0.8 else "medium", source="rita",
            evidence=[*score.evidence, f"{score.source_ip} -> {score.dest_ip}:{score.dest_port}"],
        )

    def map(self, alerts: Iterable[RawAlert], beacon_scores: Iterable[BeaconScore]) -> list[Finding]:
        findings = [finding for alert in alerts for finding in self.map_alert(alert)]
        findings.extend(self.map_beacon(score) for score in beacon_scores)
        return findings

    def map_alerts(self, alerts: Iterable[RawAlert]) -> list[Finding]:
        return [finding for alert in alerts for finding in self.map_alert(alert)]

    def map_beacons(self, beacon_scores: Iterable[BeaconScore]) -> list[Finding]:
        return [self.map_beacon(score) for score in beacon_scores]


def _severity(value: int) -> str:
    return {1: "critical", 2: "high", 3: "medium"}.get(value, "low")
