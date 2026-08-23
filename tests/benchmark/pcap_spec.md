# PCAP Analysis Automation - Deterministic Certification Specification

Implement a Python 3.11+ project named `pcap-analysis-automation` with import package
`pcap_analysis_automation`. The project is a defensive/offline network-capture analyzer.
It MUST work without network access and MUST still produce useful output when Zeek,
Suricata, and RITA are not installed.

## 1. Required project layout

At minimum create:

```text
pyproject.toml
README.md
pcap_analysis_automation/
  __init__.py
  __main__.py
  cli.py
  models.py
  metadata.py
  fallback.py
  mitre.py
  report.py
  integrations/
    __init__.py
    zeek.py
    suricata.py
    rita.py
tests/
  ...
```

Keep analysis logic out of the CLI parser. Use typed data structures (dataclasses,
TypedDicts, or equivalent) for normalized findings/report data.

## 2. Command-line contract

The following command MUST work:

```bash
python -m pcap_analysis_automation \
  --input INPUT.pcap \
  --out OUT_DIR \
  --format both
```

Supported options:

```text
--input PATH              required source capture
--out DIR                 required output directory
--format json|markdown|both   default both
--timeout SECONDS         positive external-tool timeout, default 30
--zeek-log PATH           optional deterministic Zeek conn.log replay
--suricata-eve PATH       optional deterministic Suricata eve.json replay
--rita-json PATH          optional deterministic RITA JSON replay
```

When a replay option is supplied, parse that artifact and DO NOT require the
corresponding external executable. When it is absent, an integration MAY attempt
to run the installed executable, but absence/failure of an optional tool MUST be
reported as tool status and MUST NOT make an otherwise parseable capture fail.

Exit codes:

```text
0 = analysis completed and requested reports were written
2 = CLI/input error (missing file, invalid option, invalid output path)
3 = capture is unsupported/corrupt enough that metadata/fallback parsing cannot proceed
4 = report serialization/write failure
```

Errors go to stderr and must contain an actionable, human-readable reason. Do not
print Python tracebacks for ordinary user/input errors.

## 3. Input metadata

For every successful analysis compute from the original input bytes:

```text
path
sha256 (64 lowercase hex characters)
size_bytes
format = pcap | pcapng
```

Classic PCAP magic values for both endian variants must be recognized. PCAPNG
must at least be recognized by its section-header magic; a useful `unsupported`
status is acceptable for packet decoding if full PCAPNG fallback parsing is not
implemented.

Never modify the source capture.

## 4. Pure-Python fallback analyzer

A standard-library-only fallback must parse enough classic Ethernet PCAP to
extract IPv4 TCP/UDP flow records containing at least:

```text
timestamp
src_ip
dst_ip
protocol
dst_port
packet_length
```

Malformed/truncated packets must be skipped safely when possible; a malformed
packet must never lead to arbitrary reads, hangs, or shell execution.

The fallback must implement these deterministic detectors:

1. `periodic_beaconing`: for the same `(src_ip,dst_ip,protocol,dst_port)`, at
   least 4 observations whose positive inter-arrival intervals have coefficient
   of variation <= 0.20. Emit severity `medium` and MITRE `T1071`.
2. `port_scan`: the same source contacts at least 10 distinct destination ports
   on the same destination IP in the capture. Emit severity `medium` and MITRE
   `T1046`.
3. `dns_activity`: UDP or TCP destination port 53. Aggregate evidence and emit
   severity `info` with MITRE `T1071.004` when at least one DNS flow exists.

Findings must be based on parsed evidence. Do not fabricate external-tool hits.

## 5. Zeek normalization

`--zeek-log` accepts a Zeek TSV log containing `#fields` and optional `#types`
headers. At minimum parse `conn.log` fields when present:

```text
ts id.orig_h id.orig_p id.resp_h id.resp_p proto service duration orig_bytes resp_bytes conn_state
```

Normalize rows into evidence/findings without executing Zeek. DNS/HTTP services
may enrich findings. Parsing an empty valid Zeek log is successful and yields no
fabricated findings.

When external Zeek execution is attempted, use an argv list, a bounded timeout,
a dedicated output directory, and never `shell=True`.

## 6. Suricata normalization

`--suricata-eve` accepts newline-delimited `eve.json`. Parse `event_type=alert`
records. Each alert finding must preserve useful evidence including source,
destination, ports when present, signature/signature_id, category, and severity.
Do not turn non-alert events into alert findings.

When a Suricata alert already contains or clearly maps to a known MITRE technique,
preserve it; otherwise use a deterministic local mapping only when supported by
the alert/evidence.

External Suricata invocation, if attempted, must be bounded and use argv without
`shell=True`.

## 7. RITA normalization

`--rita-json` accepts JSON in either of these replay-friendly forms:

```json
{"beacons":[{"src":"10.0.0.1","dst":"10.0.0.2","score":0.91}]}
```

or a top-level list of beacon-like objects. Recognize common aliases such as
`src/source/src_ip`, `dst/destination/dst_ip`, and `score/beacon_score`.
A valid beacon record emits a `rita_beacon` finding, severity `medium`, MITRE
`T1071`. Invalid individual records are skipped with a warning/status detail,
not converted into fabricated data.

External RITA execution is optional because installed versions differ. If tried,
it must be bounded, argv-based, and failure must be represented in `tools.rita`.

## 8. MITRE ATT&CK representation

Every finding contains a `mitre` array. Each entry has:

```json
{"technique_id":"T1046","name":"Network Service Discovery"}
```

Use at least these exact mappings:

```text
T1046     Network Service Discovery
T1071     Application Layer Protocol
T1071.004 DNS
```

An informational finding may have an empty MITRE array only if no evidence-backed
mapping applies.

## 9. JSON report contract

`report.json` MUST be valid UTF-8 JSON with this top-level shape:

```json
{
  "schema_version": "1.0",
  "input": {
    "path": "...",
    "sha256": "...",
    "size_bytes": 123,
    "format": "pcap"
  },
  "tools": {
    "fallback": {"status": "ok|partial|error", "detail": "..."},
    "zeek": {"status": "ok|unavailable|error|not_run", "detail": "..."},
    "suricata": {"status": "ok|unavailable|error|not_run", "detail": "..."},
    "rita": {"status": "ok|unavailable|error|not_run", "detail": "..."}
  },
  "summary": {
    "flow_count": 0,
    "finding_count": 0,
    "severity_counts": {"info": 0, "low": 0, "medium": 0, "high": 0, "critical": 0}
  },
  "findings": []
}
```

Each finding MUST contain:

```text
id             non-empty stable string within one report
type           non-empty machine-readable string
severity       info|low|medium|high|critical
title          non-empty human-readable string
description    non-empty human-readable string
evidence       JSON object
mitre          array
source         fallback|zeek|suricata|rita
```

`summary.finding_count` must equal `len(findings)`. Severity counts must exactly
match the findings array. Findings should be deterministically ordered so the
same inputs produce semantically stable reports.

## 10. Markdown report contract

`report.md` must be non-empty UTF-8 Markdown and contain headings/sections for:

```text
Summary
Input
Tool Status
Findings
MITRE ATT&CK
```

It must include finding type/title, severity, evidence summary, and MITRE IDs for
mapped findings. If there are no findings, explicitly state that rather than
omitting the Findings section.

## 11. Security and process behavior

- Never use `shell=True` for external integrations.
- Never interpolate capture paths into a shell command string.
- External commands use the configured timeout and surface timeout status.
- Keep generated integration artifacts under `--out` or a temporary directory.
- Do not write outside `--out` except normal Python cache files created by the interpreter.
- Do not require root.
- Do not require network access.
- Do not mutate input captures.

## 12. Tests required in the generated project

The generated project must include meaningful tests covering at least:

```text
metadata/hash/format detection
classic PCAP fallback parsing
periodic beacon detection
port-scan detection
DNS activity mapping
Zeek TSV replay
Suricata EVE replay
RITA replay
JSON report consistency
Markdown required sections
bad input path / exit code
```

`pytest -q` must pass. `python -m compileall -q .` must pass.

## 13. Certification commands

The final project will be checked with:

```bash
python -m compileall -q .
pytest -q
python -m pcap_analysis_automation --help
python -m pcap_analysis_automation --input <generated fixture.pcap> --out <out> --format both
python -m pcap_analysis_automation --input <missing> --out <out> --format both
```

The grader also supplies independent Zeek, Suricata, and RITA replay fixtures and
independent generated PCAP captures. Do not hard-code fixture filenames, hashes,
addresses, ports, or expected report content.

## 14. Acceptance rubric (100 points, all mandatory)

```text
Architecture / package layout       10
Metadata extraction                 10
Zeek normalization                  15
Suricata normalization              10
RITA normalization                  10
Pure-Python fallback detection      15
MITRE mapping                       10
JSON report consistency              5
Markdown report                      5
CLI / errors / tests / safety       10
TOTAL                              100
```

Certification requires 100/100. A partial score is a failed certification run.
