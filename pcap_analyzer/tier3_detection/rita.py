"""Deterministic, explainable beaconing scoring inspired by RITA."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from itertools import pairwise

from pcap_analyzer.types import BeaconScore, ConnRecord


class RitaDetector:
    """Score repeated outbound flows by timing regularity and byte variability."""

    def __init__(self, min_connections: int = 3, threshold: float = 0.65) -> None:
        self.min_connections = min_connections
        self.threshold = threshold

    def score(self, connections: Iterable[ConnRecord]) -> list[BeaconScore]:
        groups: dict[tuple[str, str, int | None, str], list[ConnRecord]] = defaultdict(list)
        for connection in connections:
            groups[(connection.source_ip, connection.dest_ip, connection.dest_port, connection.protocol)].append(connection)
        results: list[BeaconScore] = []
        for flow, records in groups.items():
            if len(records) < self.min_connections:
                continue
            ordered = sorted(records, key=lambda item: item.timestamp)
            intervals = [right.timestamp - left.timestamp for left, right in pairwise(ordered)]
            mean = statistics.fmean(intervals) if intervals else 0.0
            stddev = statistics.pstdev(intervals) if len(intervals) > 1 else 0.0
            regularity = 1.0 if mean > 0 and stddev == 0 else max(0.0, 1.0 - stddev / mean) if mean > 0 else 0.0
            entropy = _normalized_entropy([record.orig_bytes + record.resp_bytes for record in ordered])
            # Regular cadence is the dominant C2 indicator. Low byte variability is an
            # additional signal; recurrence provides a small confidence boost.
            recurrence = min(1.0, len(ordered) / 10)
            score = min(1.0, 0.65 * regularity + 0.2 * (1.0 - entropy) + 0.15 * recurrence)
            source, destination, port, protocol = flow
            results.append(BeaconScore(
                source_ip=source, dest_ip=destination, dest_port=port, protocol=protocol,
                score=round(score, 4), connection_count=len(ordered), interval_mean=mean,
                interval_stddev=stddev, regularity=round(regularity, 4), byte_entropy=round(entropy, 4),
                evidence=[f"{len(ordered)} connections; mean interval {mean:.3f}s; interval stddev {stddev:.3f}s"],
            ))
        return sorted(results, key=lambda item: item.score, reverse=True)

    def detect(self, connections: Iterable[ConnRecord]) -> list[BeaconScore]:
        return [score for score in self.score(connections) if score.score >= self.threshold]


def _normalized_entropy(values: list[int]) -> float:
    if not values or len(values) == 1:
        return 0.0
    counts: dict[int, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    entropy = -sum((count / len(values)) * math.log2(count / len(values)) for count in counts.values())
    return entropy / math.log2(len(values))


def calculate_beacon_scores(connections: Iterable[ConnRecord], **kwargs: object) -> list[BeaconScore]:
    return RitaDetector(**kwargs).score(connections)
