"""Small, transparent ground-truth fixtures for benchmark smoke tests.

These are identifiers representative of the named public datasets, not copies of
their packet captures.  They let users validate metric plumbing without bundling
large, redistributed capture files.
"""

CTU13 = {
    "name": "CTU-13 (C2/Botnet)", "ground_truth": {"ctu13-c2-001", "ctu13-botnet-002"},
    "total_events": 20, "baseline_alerts": 10,
}
UGR16 = {
    "name": "UGR'16 (Periodicity)", "ground_truth": {"ugr16-periodic-001"},
    "total_events": 30, "baseline_alerts": 12,
}
MALWARE_TRAFFIC_ANALYSIS = {
    "name": "Malware-Traffic-Analysis (Exploit kit/Ransomware)",
    "ground_truth": {"mta-exploit-001", "mta-ransomware-002"}, "total_events": 25, "baseline_alerts": 15,
}
MAWI = {
    "name": "MAWI (Benign traffic baseline)", "ground_truth": set(), "total_events": 50, "baseline_alerts": 20,
}
BENCHMARK_FIXTURES = {"ctu13": CTU13, "ugr16": UGR16, "malware_traffic_analysis": MALWARE_TRAFFIC_ANALYSIS, "mawi": MAWI}
