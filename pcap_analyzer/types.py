"""Typed data exchanged between the pipeline tiers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class PcapMetadata:
    path: str
    sha256: str
    file_size: int
    packet_count: int
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    duration_seconds: float = 0.0
    linktype: int | None = None
    format: str = "pcap"


@dataclass(slots=True)
class ConnRecord:
    timestamp: float
    uid: str = ""
    source_ip: str = ""
    source_port: int | None = None
    dest_ip: str = ""
    dest_port: int | None = None
    protocol: str = ""
    service: str = ""
    duration: float | None = None
    orig_bytes: int = 0
    resp_bytes: int = 0
    conn_state: str = ""


@dataclass(slots=True)
class DnsRecord:
    timestamp: float
    uid: str = ""
    source_ip: str = ""
    dest_ip: str = ""
    query: str = ""
    query_type: str = ""
    answers: list[str] = field(default_factory=list)
    rcode: str = ""


@dataclass(slots=True)
class HttpRecord:
    timestamp: float
    uid: str = ""
    source_ip: str = ""
    dest_ip: str = ""
    host: str = ""
    uri: str = ""
    method: str = ""
    status_code: int | None = None
    user_agent: str = ""


@dataclass(slots=True)
class TlsRecord:
    timestamp: float
    uid: str = ""
    source_ip: str = ""
    dest_ip: str = ""
    server_name: str = ""
    version: str = ""
    cipher: str = ""
    established: bool | None = None


@dataclass(slots=True)
class FileRecord:
    timestamp: float
    fuid: str = ""
    uid: str = ""
    source_ip: str = ""
    dest_ip: str = ""
    filename: str = ""
    mime_type: str = ""
    sha256: str = ""
    total_bytes: int = 0


@dataclass(slots=True)
class RawAlert:
    timestamp: float
    signature_id: int | str
    signature: str
    category: str = ""
    severity: int = 3
    source_ip: str = ""
    source_port: int | None = None
    dest_ip: str = ""
    dest_port: int | None = None
    protocol: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    mitre_tactics: list[str] = field(default_factory=list)
    mitre_techniques: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BeaconScore:
    source_ip: str
    dest_ip: str
    dest_port: int | None
    protocol: str
    score: float
    connection_count: int
    interval_mean: float = 0.0
    interval_stddev: float = 0.0
    regularity: float = 0.0
    byte_entropy: float = 0.0
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Finding:
    title: str
    tactic: str
    technique_id: str
    confidence: float
    description: str = ""
    severity: str = "medium"
    source: str = ""
    evidence: list[str] = field(default_factory=list)
    timestamp: float | None = None


@dataclass(slots=True)
class TimelineEvent:
    timestamp: float
    event_type: str
    summary: str
    source_ip: str = ""
    dest_ip: str = ""
    finding: Finding | None = None
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AnalysisReport:
    metadata: PcapMetadata | None = None
    findings: list[Finding] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    kill_chain: list[str] = field(default_factory=list)
    alerts: list[RawAlert] = field(default_factory=list)
    beacon_scores: list[BeaconScore] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
