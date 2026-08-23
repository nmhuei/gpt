"""Ground-truth alert classification metrics."""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BenchmarkMetrics:
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    precision: float
    recall: float
    f1: float
    false_positive_rate: float
    alert_reduction_rate: float

    @property
    def fpr(self) -> float:
        return self.false_positive_rate


def evaluate(
    predicted: Iterable[Hashable], ground_truth: Iterable[Hashable], *,
    total_events: int | None = None, baseline_alerts: int | None = None,
) -> BenchmarkMetrics:
    """Evaluate identifiers; duplicates are intentionally treated as one detection."""
    predicted_set, truth_set = set(predicted), set(ground_truth)
    tp = len(predicted_set & truth_set)
    fp = len(predicted_set - truth_set)
    fn = len(truth_set - predicted_set)
    universe = total_events if total_events is not None else len(predicted_set | truth_set)
    if universe < len(predicted_set | truth_set):
        raise ValueError("total_events cannot be smaller than the labelled event universe")
    tn = universe - tp - fp - fn
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0
    baseline = baseline_alerts if baseline_alerts is not None else universe
    if baseline < len(predicted_set):
        raise ValueError("baseline_alerts cannot be smaller than predictions")
    reduction = 1 - len(predicted_set) / baseline if baseline else 0.0
    return BenchmarkMetrics(tp, fp, fn, tn, precision, recall, f1, fpr, reduction)


class BenchmarkEvaluator:
    """Object-oriented facade for callers that retain benchmark configuration."""

    def evaluate(self, predicted: Iterable[Hashable], ground_truth: Iterable[Hashable], **kwargs: int) -> BenchmarkMetrics:
        return evaluate(predicted, ground_truth, **kwargs)
