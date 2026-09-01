from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

RUBRIC = {
    "architecture": 10,
    "metadata": 10,
    "zeek": 15,
    "suricata": 10,
    "rita": 10,
    "fallback": 15,
    "mitre": 10,
    "json_report": 5,
    "markdown_report": 5,
    "cli_tests_safety": 10,
}


class Grader:
    def __init__(self, project: Path) -> None:
        self.project = project.resolve()
        self.score: dict[str, int] = {key: 0 for key in RUBRIC}
        self.notes: dict[str, list[str]] = {key: [] for key in RUBRIC}

    def note(self, category: str, text: str) -> None:
        self.notes[category].append(text)

    def award(self, category: str, points: int, text: str) -> None:
        self.score[category] += points
        self.note(category, f"+{points}: {text}")

    def run(self, args: list[str], *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=self.project,
            text=True,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "PYTHONPATH": str(self.project)},
            check=False,
        )

    def run_cli(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return self.run([sys.executable, "-m", "pcap_analysis_automation", *args], timeout=timeout)

    def grade_architecture(self) -> None:
        required = [
            "pyproject.toml",
            "README.md",
            "pcap_analysis_automation/__init__.py",
            "pcap_analysis_automation/__main__.py",
            "pcap_analysis_automation/cli.py",
            "pcap_analysis_automation/models.py",
            "pcap_analysis_automation/metadata.py",
            "pcap_analysis_automation/fallback.py",
            "pcap_analysis_automation/mitre.py",
            "pcap_analysis_automation/report.py",
            "pcap_analysis_automation/integrations/zeek.py",
            "pcap_analysis_automation/integrations/suricata.py",
            "pcap_analysis_automation/integrations/rita.py",
        ]
        missing = [path for path in required if not (self.project / path).is_file()]
        if not missing:
            self.award("architecture", 5, "required package layout exists")
        else:
            self.note("architecture", f"missing required paths: {missing}")
        compiled = self.run([sys.executable, "-m", "compileall", "-q", "."])
        if compiled.returncode == 0:
            self.award("architecture", 5, "compileall passes")
        else:
            self.note("architecture", f"compileall failed: {compiled.stderr[-1000:]}")

    @staticmethod
    def _ipv4_packet(src: str, dst: str, proto: int, src_port: int, dst_port: int) -> bytes:
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
        total_length = 20 + len(transport)
        ip = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            total_length,
            0,
            0,
            64,
            proto,
            0,
            socket.inet_aton(src),
            socket.inet_aton(dst),
        )
        ethernet = b"\xaa\xbb\xcc\xdd\xee\xff" + b"\x00\x11\x22\x33\x44\x55" + struct.pack("!H", 0x0800)
        return ethernet + ip + transport

    @classmethod
    def make_pcap(cls, path: Path) -> None:
        packets: list[tuple[int, int, bytes]] = []
        base = 1_700_000_000
        for index in range(5):
            packets.append(
                (
                    base + index * 10,
                    0,
                    cls._ipv4_packet("10.1.2.3", "10.9.8.7", 6, 41000, 443),
                )
            )
        for offset, port in enumerate(range(1000, 1012), start=1):
            packets.append(
                (
                    base + 100 + offset,
                    0,
                    cls._ipv4_packet("10.5.5.5", "10.9.8.7", 6, 42000 + offset, port),
                )
            )
        packets.append(
            (
                base + 300,
                0,
                cls._ipv4_packet("10.1.2.3", "8.8.8.8", 17, 53000, 53),
            )
        )
        with path.open("wb") as stream:
            stream.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
            for ts_sec, ts_usec, packet in packets:
                stream.write(struct.pack("<IIII", ts_sec, ts_usec, len(packet), len(packet)))
                stream.write(packet)

    @staticmethod
    def read_report(out_dir: Path) -> dict[str, Any] | None:
        path = out_dir / "report.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def findings(report: dict[str, Any] | None, *, source: str | None = None) -> list[dict[str, Any]]:
        if not report:
            return []
        values = [item for item in report.get("findings", []) if isinstance(item, dict)]
        if source is not None:
            values = [item for item in values if item.get("source") == source]
        return values

    def grade_base(self, work: Path, pcap: Path) -> dict[str, Any] | None:
        out = work / "base-out"
        result = self.run_cli("--input", str(pcap), "--out", str(out), "--format", "both")
        if result.returncode != 0:
            self.note("metadata", f"base CLI failed rc={result.returncode}: {result.stderr[-1000:]}")
            return None
        report = self.read_report(out)
        if report is None:
            self.note("metadata", "base report.json missing or invalid")
            return None

        expected_hash = hashlib.sha256(pcap.read_bytes()).hexdigest()
        input_obj = report.get("input")
        input_data: dict[str, Any] = input_obj if isinstance(input_obj, dict) else {}
        metadata_checks = [
            input_data.get("sha256") == expected_hash,
            input_data.get("size_bytes") == pcap.stat().st_size,
            input_data.get("format") == "pcap",
            isinstance(input_data.get("path"), str) and bool(input_data.get("path")),
        ]
        if all(metadata_checks):
            self.award("metadata", 10, "path/hash/size/format match independent PCAP")
        else:
            self.note("metadata", f"metadata mismatch: {input_data}")

        types = {item.get("type") for item in self.findings(report, source="fallback")}
        for finding_type in ("periodic_beaconing", "port_scan", "dns_activity"):
            if finding_type in types:
                self.award("fallback", 5, f"fallback detected {finding_type}")
            else:
                self.note("fallback", f"missing fallback finding {finding_type}")

        all_mitre = {
            mapping.get("technique_id")
            for item in self.findings(report)
            for mapping in item.get("mitre", [])
            if isinstance(mapping, dict)
        }
        for technique in ("T1046", "T1071", "T1071.004"):
            if technique in all_mitre:
                points = 4 if technique == "T1046" else 3
                self.award("mitre", points, f"evidence-backed {technique} present")
            else:
                self.note("mitre", f"missing {technique}")

        findings = self.findings(report)
        summary_obj = report.get("summary")
        summary: dict[str, Any] = summary_obj if isinstance(summary_obj, dict) else {}
        severities = {name: 0 for name in ("info", "low", "medium", "high", "critical")}
        for item in findings:
            severity = item.get("severity")
            if severity in severities:
                severities[severity] += 1
        if (
            report.get("schema_version") == "1.0"
            and summary.get("finding_count") == len(findings)
            and summary.get("severity_counts") == severities
            and isinstance(summary.get("flow_count"), int)
        ):
            self.award("json_report", 5, "JSON schema/counts are internally consistent")
        else:
            self.note("json_report", f"report consistency failed: summary={summary}")

        markdown_path = out / "report.md"
        markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.is_file() else ""
        required = ("Summary", "Input", "Tool Status", "Findings", "MITRE ATT&CK")
        if markdown.strip() and all(value.casefold() in markdown.casefold() for value in required):
            self.award("markdown_report", 3, "required Markdown sections present")
        else:
            self.note("markdown_report", "required Markdown sections missing")
        if "T1046" in markdown and "T1071" in markdown:
            self.award("markdown_report", 2, "Markdown surfaces MITRE IDs")
        else:
            self.note("markdown_report", "MITRE IDs missing from Markdown")
        return report

    def grade_zeek(self, work: Path, pcap: Path) -> None:
        zeek = work / "conn.log"
        zeek.write_text(
            "#separator \\x09\n"
            "#fields\tts\tuid\tid.orig_h\tid.orig_p\tid.resp_h\tid.resp_p\tproto\tservice\tduration\torig_bytes\tresp_bytes\tconn_state\n"
            "#types\ttime\tstring\taddr\tport\taddr\tport\tenum\tstring\tinterval\tcount\tcount\tstring\n"
            "1700000500.0\tC-GRADER\t10.22.33.44\t51000\t8.8.8.8\t53\tudp\tdns\t0.1\t40\t80\tSF\n",
            encoding="utf-8",
        )
        out = work / "zeek-out"
        result = self.run_cli(
            "--input", str(pcap), "--out", str(out), "--format", "json", "--zeek-log", str(zeek)
        )
        report = self.read_report(out) if result.returncode == 0 else None
        hits = self.findings(report, source="zeek")
        serialized = json.dumps(hits, sort_keys=True)
        if hits and "10.22.33.44" in serialized and "8.8.8.8" in serialized:
            self.award("zeek", 10, "Zeek TSV replay normalized addresses/evidence")
        else:
            self.note("zeek", f"Zeek replay not normalized: rc={result.returncode}, hits={hits}")
        tools = report.get("tools", {}) if report else {}
        status = tools.get("zeek", {}) if isinstance(tools, dict) else {}
        if isinstance(status, dict) and status.get("status") == "ok":
            self.award("zeek", 5, "Zeek replay status is ok")
        else:
            self.note("zeek", f"Zeek tool status not ok: {status}")

    def grade_suricata(self, work: Path, pcap: Path) -> None:
        eve = work / "eve.json"
        record = {
            "timestamp": "2026-08-17T00:00:00Z",
            "event_type": "alert",
            "src_ip": "10.44.55.66",
            "src_port": 51515,
            "dest_ip": "203.0.113.20",
            "dest_port": 443,
            "proto": "TCP",
            "alert": {
                "signature_id": 991122,
                "signature": "GRADER Test Callback",
                "category": "Potentially Bad Traffic",
                "severity": 2,
            },
        }
        eve.write_text(json.dumps(record) + "\n" + json.dumps({"event_type": "flow"}) + "\n")
        out = work / "suricata-out"
        result = self.run_cli(
            "--input", str(pcap), "--out", str(out), "--format", "json", "--suricata-eve", str(eve)
        )
        report = self.read_report(out) if result.returncode == 0 else None
        hits = self.findings(report, source="suricata")
        serialized = json.dumps(hits, sort_keys=True)
        if len(hits) == 1 and "991122" in serialized and "GRADER Test Callback" in serialized:
            self.award("suricata", 7, "Suricata alert replay normalized and non-alert ignored")
        else:
            self.note("suricata", f"Suricata normalization mismatch: rc={result.returncode}, hits={hits}")
        tools = report.get("tools", {}) if report else {}
        status = tools.get("suricata", {}) if isinstance(tools, dict) else {}
        if isinstance(status, dict) and status.get("status") == "ok":
            self.award("suricata", 3, "Suricata replay status is ok")
        else:
            self.note("suricata", f"Suricata tool status not ok: {status}")

    def grade_rita(self, work: Path, pcap: Path) -> None:
        rita = work / "rita.json"
        rita.write_text(
            json.dumps({"beacons": [{"source": "10.66.77.88", "destination": "198.51.100.9", "beacon_score": 0.93}]})
            + "\n"
        )
        out = work / "rita-out"
        result = self.run_cli(
            "--input", str(pcap), "--out", str(out), "--format", "json", "--rita-json", str(rita)
        )
        report = self.read_report(out) if result.returncode == 0 else None
        hits = self.findings(report, source="rita")
        serialized = json.dumps(hits, sort_keys=True)
        mitre_ids = {
            mapping.get("technique_id")
            for item in hits
            for mapping in item.get("mitre", [])
            if isinstance(mapping, dict)
        }
        if hits and "10.66.77.88" in serialized and "198.51.100.9" in serialized:
            self.award("rita", 7, "RITA alias fields normalized into beacon evidence")
        else:
            self.note("rita", f"RITA normalization mismatch: rc={result.returncode}, hits={hits}")
        if "T1071" in mitre_ids:
            self.award("rita", 3, "RITA beacon maps to T1071")
        else:
            self.note("rita", "RITA beacon missing T1071")

    def grade_cli_tests_safety(self, work: Path) -> None:
        help_result = self.run_cli("--help")
        if help_result.returncode == 0 and "--input" in help_result.stdout and "--out" in help_result.stdout:
            self.award("cli_tests_safety", 2, "CLI help works")
        else:
            self.note("cli_tests_safety", "CLI help failed")

        pytest_result = self.run([sys.executable, "-m", "pytest", "-q"], timeout=180)
        if pytest_result.returncode == 0:
            self.award("cli_tests_safety", 4, "generated project pytest passes")
        else:
            self.note("cli_tests_safety", f"pytest failed: {pytest_result.stdout[-1200:]} {pytest_result.stderr[-1200:]}")

        missing = work / "does-not-exist.pcap"
        bad = self.run_cli("--input", str(missing), "--out", str(work / "bad-out"), "--format", "both")
        if bad.returncode == 2 and bad.stderr.strip() and "Traceback" not in bad.stderr:
            self.award("cli_tests_safety", 2, "missing input returns clean exit code 2")
        else:
            self.note("cli_tests_safety", f"bad-path contract failed rc={bad.returncode} stderr={bad.stderr[-500:]}")

        unsafe: list[str] = []
        for path in (self.project / "pcap_analysis_automation").rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                        unsafe.append(str(path.relative_to(self.project)))
        if not unsafe:
            self.award("cli_tests_safety", 2, "no subprocess shell=True in package AST")
        else:
            self.note("cli_tests_safety", f"unsafe shell=True usage: {unsafe}")

    def result(self) -> dict[str, Any]:
        total = sum(self.score.values())
        return {
            "score": total,
            "required": 100,
            "passed": total == 100,
            "categories": {
                key: {
                    "score": self.score[key],
                    "max": RUBRIC[key],
                    "notes": self.notes[key],
                }
                for key in RUBRIC
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    grader = Grader(args.project)
    if not grader.project.is_dir():
        raise SystemExit(f"project directory not found: {grader.project}")
    runtime_tmp = Path(
        os.environ.get("WEBGPT_RUNTIME_ROOT", "~/Downloads/webgpt")
    ).expanduser() / "tmp"
    runtime_tmp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pcap-grader-", dir=runtime_tmp) as raw:
        work = Path(raw)
        pcap = work / "grader-fixture.pcap"
        grader.make_pcap(pcap)
        grader.grade_architecture()
        grader.grade_base(work, pcap)
        grader.grade_zeek(work, pcap)
        grader.grade_suricata(work, pcap)
        grader.grade_rita(work, pcap)
        grader.grade_cli_tests_safety(work)
    result = grader.result()
    encoded = json.dumps(result, indent=2, sort_keys=True)
    print(encoded)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded + "\n", encoding="utf-8")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
