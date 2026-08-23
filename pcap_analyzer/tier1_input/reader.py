"""Minimal PCAP/PCAPNG metadata reader with integrity verification."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from pcap_analyzer.types import PcapMetadata

_PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": ("<", 1_000_000),
    b"\xa1\xb2\xc3\xd4": (">", 1_000_000),
    b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000),
    b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000),
}
_PCAPNG_MAGIC = b"\x0a\x0d\x0d\x0a"


def compute_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_integrity(path: str | Path, expected_sha256: str | None = None) -> bool:
    """Return whether a file matches *expected_sha256* (or is readable)."""
    actual = compute_sha256(path)
    return expected_sha256 is None or actual.lower() == expected_sha256.lower()


class PcapReader:
    """Extract packet/timestamp metadata without decoding packet payloads."""

    def read(self, path: str | Path, expected_sha256: str | None = None) -> PcapMetadata:
        target = Path(path)
        if expected_sha256 and not verify_integrity(target, expected_sha256):
            raise ValueError("PCAP SHA-256 does not match the expected value")
        data = target.read_bytes()
        if len(data) < 4:
            raise ValueError("PCAP is too short to contain a header")
        if data[:4] == _PCAPNG_MAGIC:
            count, first, last, linktype = self._parse_pcapng(data)
            file_format = "pcapng"
        else:
            count, first, last, linktype = self._parse_pcap(data)
            file_format = "pcap"
        duration = max(0.0, (last or 0.0) - (first or 0.0)) if first is not None else 0.0
        return PcapMetadata(
            path=str(target), sha256=compute_sha256(target), file_size=len(data),
            packet_count=count, first_timestamp=first, last_timestamp=last,
            duration_seconds=duration, linktype=linktype, format=file_format,
        )

    @staticmethod
    def _parse_pcap(data: bytes) -> tuple[int, float | None, float | None, int]:
        if len(data) < 24 or data[:4] not in _PCAP_MAGIC:
            raise ValueError("Unsupported or malformed PCAP header")
        endian, precision = _PCAP_MAGIC[data[:4]]
        linktype = struct.unpack_from(f"{endian}I", data, 20)[0]
        offset, count, first, last = 24, 0, None, None
        while offset + 16 <= len(data):
            seconds, fraction, captured_length, _ = struct.unpack_from(f"{endian}IIII", data, offset)
            offset += 16
            if offset + captured_length > len(data):
                raise ValueError("Truncated PCAP packet record")
            timestamp = seconds + fraction / precision
            first = timestamp if first is None else min(first, timestamp)
            last = timestamp if last is None else max(last, timestamp)
            count += 1
            offset += captured_length
        if offset != len(data):
            raise ValueError("Malformed PCAP packet header")
        return count, first, last, linktype

    @staticmethod
    def _parse_pcapng(data: bytes) -> tuple[int, float | None, float | None, int | None]:
        offset, endian, interfaces = 0, "<", {}
        count, first, last, linktype = 0, None, None, None
        while offset + 12 <= len(data):
            block_type = data[offset : offset + 4]
            if block_type == _PCAPNG_MAGIC:
                byte_order = data[offset + 8 : offset + 12]
                endian = "<" if byte_order == b"\x4d\x3c\x2b\x1a" else ">" if byte_order == b"\x1a\x2b\x3c\x4d" else ""
                if not endian:
                    raise ValueError("Malformed PCAPNG section header")
            if not endian:
                raise ValueError("PCAPNG has no section header")
            length = struct.unpack_from(f"{endian}I", data, offset + 4)[0]
            if length < 12 or offset + length > len(data):
                raise ValueError("Malformed PCAPNG block")
            if struct.unpack_from(f"{endian}I", data, offset + length - 4)[0] != length:
                raise ValueError("PCAPNG block length mismatch")
            kind = struct.unpack_from(f"{endian}I", data, offset)[0]
            if kind == 1 and length >= 20:
                interface_id = len(interfaces)
                interfaces[interface_id] = struct.unpack_from(f"{endian}H", data, offset + 8)[0]
                linktype = linktype or interfaces[interface_id]
            elif kind == 6 and length >= 32:
                interface_id, hi, lo = struct.unpack_from(f"{endian}III", data, offset + 8)
                # PCAPNG defaults to microsecond units; options can refine this but do not
                # affect packet counting or ordering, the primary tier-one guarantees.
                timestamp = ((hi << 32) | lo) / 1_000_000
                first = timestamp if first is None else min(first, timestamp)
                last = timestamp if last is None else max(last, timestamp)
                count += 1
                linktype = linktype or interfaces.get(interface_id)
            offset += length
        if offset != len(data):
            raise ValueError("Malformed trailing PCAPNG data")
        return count, first, last, linktype


def read_pcap(path: str | Path, expected_sha256: str | None = None) -> PcapMetadata:
    return PcapReader().read(path, expected_sha256)
