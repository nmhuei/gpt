"""Normalize Zeek TSV and JSON logs into the pipeline record types."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pcap_analyzer.types import ConnRecord, DnsRecord, FileRecord, HttpRecord, TlsRecord

_EMPTY = {"", "-", "(empty)", "null", "None"}


def _value(row: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and (not isinstance(value, str) or value not in _EMPTY):
            return value
    return default


def _number(value: Any, kind: type[int] | type[float], default: Any = None) -> Any:
    if value in _EMPTY or value is None:
        return default
    try:
        return kind(value)
    except (TypeError, ValueError):
        return default


def _strings(value: Any) -> list[str]:
    if value is None or (isinstance(value, str) and value in _EMPTY):
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return str(value).replace(";", ",").split(",")


class ZeekNormalizer:
    """Parse Zeek's standard log schemas, in TSV or JSON-lines form."""

    def parse(self, path: str | Path, log_type: str | None = None) -> list[Any]:
        target = Path(path)
        resolved_type = (log_type or target.stem).replace(".log", "").lower()
        rows = list(self._rows(target))
        return self.normalize(rows, resolved_type)

    def normalize(self, rows: Iterable[Mapping[str, Any]], log_type: str) -> list[Any]:
        parser = {
            "conn": self.conn, "dns": self.dns, "http": self.http,
            "ssl": self.tls, "tls": self.tls, "files": self.files, "file": self.files,
        }.get(log_type.lower().replace(".log", ""))
        if parser is None:
            raise ValueError(f"Unsupported Zeek log type: {log_type}")
        return [parser(row) for row in rows]

    @staticmethod
    def _rows(path: Path) -> Iterable[dict[str, Any]]:
        text = path.read_text(encoding="utf-8")
        stripped = text.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            if stripped.startswith("["):
                payload = json.loads(text)
                if not isinstance(payload, list):
                    raise ValueError("Zeek JSON document must contain a list")
                yield from payload
            else:
                for line in text.splitlines():
                    if line.strip():
                        yield json.loads(line)
            return
        separator, fields = "\t", []
        for line in text.splitlines():
            if line.startswith("#separator"):
                raw = line.split(" ", 1)[1].strip()
                separator = "\t" if raw == "\\x09" else raw.encode().decode("unicode_escape")
            elif line.startswith("#fields"):
                fields = line.rstrip("\n").split(separator)[1:]
            elif line and not line.startswith("#"):
                if not fields:
                    raise ValueError("Zeek TSV is missing a #fields header")
                values = line.rstrip("\n").split(separator)
                yield dict(zip(fields, values, strict=False))

    @staticmethod
    def conn(row: Mapping[str, Any]) -> ConnRecord:
        return ConnRecord(
            timestamp=_number(_value(row, "ts"), float, 0.0), uid=str(_value(row, "uid")),
            source_ip=str(_value(row, "id.orig_h", "src_ip")), source_port=_number(_value(row, "id.orig_p", "src_port"), int),
            dest_ip=str(_value(row, "id.resp_h", "dest_ip")), dest_port=_number(_value(row, "id.resp_p", "dest_port"), int),
            protocol=str(_value(row, "proto", "protocol")), service=str(_value(row, "service")),
            duration=_number(_value(row, "duration"), float), orig_bytes=_number(_value(row, "orig_bytes"), int, 0),
            resp_bytes=_number(_value(row, "resp_bytes"), int, 0), conn_state=str(_value(row, "conn_state")),
        )

    @staticmethod
    def dns(row: Mapping[str, Any]) -> DnsRecord:
        return DnsRecord(
            timestamp=_number(_value(row, "ts"), float, 0.0), uid=str(_value(row, "uid")),
            source_ip=str(_value(row, "id.orig_h", "src_ip")), dest_ip=str(_value(row, "id.resp_h", "dest_ip")),
            query=str(_value(row, "query")), query_type=str(_value(row, "qtype_name", "qtype")),
            answers=_strings(_value(row, "answers")), rcode=str(_value(row, "rcode_name", "rcode")),
        )

    @staticmethod
    def http(row: Mapping[str, Any]) -> HttpRecord:
        return HttpRecord(
            timestamp=_number(_value(row, "ts"), float, 0.0), uid=str(_value(row, "uid")),
            source_ip=str(_value(row, "id.orig_h", "src_ip")), dest_ip=str(_value(row, "id.resp_h", "dest_ip")),
            host=str(_value(row, "host")), uri=str(_value(row, "uri")), method=str(_value(row, "method")),
            status_code=_number(_value(row, "status_code"), int), user_agent=str(_value(row, "user_agent")),
        )

    @staticmethod
    def tls(row: Mapping[str, Any]) -> TlsRecord:
        established = _value(row, "established", default=None)
        return TlsRecord(
            timestamp=_number(_value(row, "ts"), float, 0.0), uid=str(_value(row, "uid")),
            source_ip=str(_value(row, "id.orig_h", "src_ip")), dest_ip=str(_value(row, "id.resp_h", "dest_ip")),
            server_name=str(_value(row, "server_name", "sni")), version=str(_value(row, "version")),
            cipher=str(_value(row, "cipher")),
            established=None if established is None else str(established).lower() in {"t", "true", "1"},
        )

    @staticmethod
    def files(row: Mapping[str, Any]) -> FileRecord:
        return FileRecord(
            timestamp=_number(_value(row, "ts"), float, 0.0), fuid=str(_value(row, "fuid")), uid=str(_value(row, "uid")),
            source_ip=str(_value(row, "tx_hosts", "source_ip")), dest_ip=str(_value(row, "rx_hosts", "dest_ip")),
            filename=str(_value(row, "filename", "name")), mime_type=str(_value(row, "mime_type")),
            sha256=str(_value(row, "sha256")), total_bytes=_number(_value(row, "total_bytes", "seen_bytes"), int, 0),
        )


def parse_zeek_log(path: str | Path, log_type: str | None = None) -> list[Any]:
    return ZeekNormalizer().parse(path, log_type)
