# Pcap Analysis Automation Pipeline Implementation Plan

Automate PCAP network traffic analysis and intrusion diagnosis for Incident Response (IR) teams using a modular 5-tier pipeline integrating Zeek normalization, Suricata signature detection, RITA statistical beaconing detection, MITRE ATT&CK mapping, and timeline/killchain reporting with ground-truth benchmark validation.

## Architecture & File Layout

- `pcap_analyzer/`
  - `__init__.py`: Package export interface.
  - `types.py`: Core dataclasses (`PcapMetadata`, `ConnRecord`, `DnsRecord`, `HttpRecord`, `TlsRecord`, `FileRecord`, `RawAlert`, `BeaconScore`, `Finding`, `TimelineEvent`, `AnalysisReport`).
  - `pipeline.py`: 5-Tier Pipeline Orchestrator executing the end-to-end analysis workflow.
  - `tier1_input/`
    - `__init__.py`
    - `reader.py`: PCAP hash integrity (SHA-256), packet count, and timestamp metadata extraction.
  - `tier2_zeek/`
    - `__init__.py`
    - `normalizer.py`: Structured parser for `conn.log`, `dns.log`, `http.log`, `ssl.log`, `files.log`.
  - `tier3_detection/`
    - `__init__.py`
    - `suricata.py`: Signature-based detection parsing `eve.json` alerts and rule metadata.
    - `rita.py`: Statistical behavior detection (Beacon interval regularity, duration score, entropy).
  - `tier4_mitre/`
    - `__init__.py`
    - `mapper.py`: MITRE ATT&CK Tactic/Technique mapping, lookup tables, and confidence scoring.
  - `tier5_reporting/`
    - `__init__.py`
    - `reporter.py`: Timeline reconstruction, kill chain phase ordering, and Markdown/JSON report synthesis.
  - `benchmarks/`
    - `__init__.py`
    - `evaluator.py`: Ground truth validation metrics (Precision, Recall, F1, FPR, Alert Reduction Rate).
    - `datasets.py`: Ground truth fixtures for CTU-13, UGR'16, Malware-Traffic-Analysis, and MAWI.

---

## Tasks

### Task 1: Pipeline Core Data Types & Tier 1 Input Verification
- **Goal**: Implement SHA-256 hash integrity checking, PCAP reader, and metadata extractor.
- **Files**: `pcap_analyzer/types.py`, `pcap_analyzer/tier1_input/reader.py`, `tests/test_pcap_tier1.py`
- **Verification**: `pytest tests/test_pcap_tier1.py`

### Task 2: Tier 2 Zeek Log Normalization Engine
- **Goal**: Implement structured parsers for `conn.log`, `dns.log`, `http.log`, `ssl.log`, `files.log`.
- **Files**: `pcap_analyzer/tier2_zeek/normalizer.py`, `tests/test_pcap_tier2.py`
- **Verification**: `pytest tests/test_pcap_tier2.py`

### Task 3: Tier 3 Detection Engine (Suricata + RITA Beaconing)
- **Goal**: Signature matching via `eve.json` and statistical beaconing detection (Interval regularity & entropy).
- **Files**: `pcap_analyzer/tier3_detection/suricata.py`, `pcap_analyzer/tier3_detection/rita.py`, `tests/test_pcap_tier3.py`
- **Verification**: `pytest tests/test_pcap_tier3.py`

### Task 4: Tier 4 MITRE ATT&CK Mapping & Confidence Scoring
- **Goal**: Rule lookup table and MITRE ATT&CK Tactic/Technique mapping with confidence weighting.
- **Files**: `pcap_analyzer/tier4_mitre/mapper.py`, `tests/test_pcap_tier4.py`
- **Verification**: `pytest tests/test_pcap_tier4.py`

### Task 5: Tier 5 Timeline Correlation & Report Generation
- **Goal**: Chronological timeline synthesis, Kill Chain reconstruction, and Markdown/JSON reporting.
- **Files**: `pcap_analyzer/tier5_reporting/reporter.py`, `pcap_analyzer/pipeline.py`, `tests/test_pcap_tier5.py`
- **Verification**: `pytest tests/test_pcap_tier5.py`

### Task 6: Ground Truth Benchmark Evaluation (CTU-13, UGR'16, MTA, MAWI)
- **Goal**: Calculate Precision, Recall, F1-Score, FPR, and Alert Reduction Rate against dataset baselines.
- **Files**: `pcap_analyzer/benchmarks/evaluator.py`, `pcap_analyzer/benchmarks/datasets.py`, `tests/test_pcap_benchmarks.py`
- **Verification**: `pytest tests/test_pcap_benchmarks.py`

