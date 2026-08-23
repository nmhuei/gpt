"""Suricata EVE JSON alert parser."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from pcap_analyzer.types import RawAlert


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]


class SuricataDetector:
    def parse(self, path: str | Path) -> list[RawAlert]:
        with Path(path).open(encoding="utf-8") as stream:
            return self.parse_events(json.loads(line) for line in stream if line.strip())

    def parse_events(self, events: Iterable[Mapping[str, Any]]) -> list[RawAlert]:
        alerts: list[RawAlert] = []
        for event in events:
            if event.get("event_type") not in {None, "alert"} or not event.get("alert"):
                continue
            alert = event["alert"]
            metadata = dict(alert.get("metadata") or {})
            tactics = _as_list(metadata.get("mitre_tactic_id") or metadata.get("mitre_tactics"))
            techniques = _as_list(metadata.get("mitre_technique_id") or metadata.get("mitre_techniques"))
            references = _as_list(metadata.get("reference") or metadata.get("references"))
            alerts.append(RawAlert(
                timestamp=_timestamp(event.get("timestamp")), signature_id=alert.get("signature_id", ""),
                signature=str(alert.get("signature", "")), category=str(alert.get("category", "")),
                severity=int(alert.get("severity", 3) or 3), source_ip=str(event.get("src_ip", "")),
                source_port=_port(event.get("src_port")), dest_ip=str(event.get("dest_ip", "")),
                dest_port=_port(event.get("dest_port")), protocol=str(event.get("proto", "")),
                metadata=metadata, mitre_tactics=tactics, mitre_techniques=techniques, evidence=references,
            ))
        return alerts


def _port(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError as error:
            raise ValueError(f"Invalid Suricata timestamp: {value!r}") from error


def parse_eve_json(path: str | Path) -> list[RawAlert]:
    return SuricataDetector().parse(path)
