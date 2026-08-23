from __future__ import annotations

import argparse
import socket
import struct
from pathlib import Path


def ipv4_packet(src: str, dst: str, proto: int, src_port: int, dst_port: int) -> bytes:
    if proto == 6:
        transport = struct.pack(
            "!HHIIHHHH",
            src_port,
            dst_port,
            0,
            0,
            (5 << 12) | 0x002,
            8192,
            0,
            0,
        )
    elif proto == 17:
        transport = struct.pack("!HHHH", src_port, dst_port, 8, 0)
    else:
        raise ValueError(proto)
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(transport),
        0,
        0,
        64,
        proto,
        0,
        socket.inet_aton(src),
        socket.inet_aton(dst),
    )
    ethernet = (
        b"\xaa\xbb\xcc\xdd\xee\xff"
        + b"\x00\x11\x22\x33\x44\x55"
        + struct.pack("!H", 0x0800)
    )
    return ethernet + ip + transport


def write_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    packets: list[tuple[int, bytes]] = []
    base = 1_700_000_000
    for index in range(5):
        packets.append(
            (base + index * 10, ipv4_packet("10.1.2.3", "10.9.8.7", 6, 41000, 443))
        )
    for offset, port in enumerate(range(1000, 1012), start=1):
        packets.append(
            (
                base + 100 + offset,
                ipv4_packet("10.5.5.5", "10.9.8.7", 6, 42000 + offset, port),
            )
        )
    packets.append((base + 300, ipv4_packet("10.1.2.3", "8.8.8.8", 17, 53000, 53)))
    with path.open("wb") as stream:
        stream.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for timestamp, packet in packets:
            stream.write(struct.pack("<IIII", timestamp, 0, len(packet), len(packet)))
            stream.write(packet)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    write_fixture(args.output)


if __name__ == "__main__":
    main()
