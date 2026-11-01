"""Metric helpers shared by training, validation and held-out evaluation."""

from __future__ import annotations

import numpy as np

HISTOGRAM_BINS = 200


def binary_metrics(
    true_positive: int, false_positive: int, true_negative: int, false_negative: int
) -> dict[str, float | int]:
    """Calculate metrics from aggregate pixel counts without hiding class imbalance."""

    total = true_positive + false_positive + true_negative + false_negative
    precision = true_positive / max(true_positive + false_positive, 1)
    recall = true_positive / max(true_positive + false_negative, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-12)
    iou = true_positive / max(true_positive + false_positive + false_negative, 1)
    accuracy = (true_positive + true_negative) / max(total, 1)
    return {
        "true_positive": int(true_positive),
        "false_positive": int(false_positive),
        "true_negative": int(true_negative),
        "false_negative": int(false_negative),
        "pixels": int(total),
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "iou": iou,
    }


class ScoreHistogram:
    """Per-class probability histograms, so a threshold sweep needs no per-pixel storage."""

    def __init__(self, bins: int = HISTOGRAM_BINS):
        self.bins = bins
        self.positive = np.zeros(bins, dtype=np.int64)
        self.negative = np.zeros(bins, dtype=np.int64)

    def update(self, probability: np.ndarray, target: np.ndarray, valid: np.ndarray) -> None:
        indices = np.minimum((probability[valid] * self.bins).astype(np.int64), self.bins - 1)
        labels = target[valid].astype(bool)
        self.positive += np.bincount(indices[labels], minlength=self.bins)
        self.negative += np.bincount(indices[~labels], minlength=self.bins)

    def metrics_at(self, threshold: float) -> dict[str, float | int]:
        cut = int(np.ceil(threshold * self.bins))
        tp = int(self.positive[cut:].sum())
        fn = int(self.positive[:cut].sum())
        fp = int(self.negative[cut:].sum())
        tn = int(self.negative[:cut].sum())
        return binary_metrics(tp, fp, tn, fn)

    def best_threshold(self, low: float = 0.05, high: float = 0.95) -> tuple[float, dict]:
        """Threshold maximising F1. Call this on validation data only."""

        best_threshold, best = 0.5, self.metrics_at(0.5)
        for cut in range(int(low * self.bins), int(high * self.bins) + 1):
            threshold = cut / self.bins
            metrics = self.metrics_at(threshold)
            if metrics["f1"] > best["f1"]:
                best_threshold, best = threshold, metrics
        return best_threshold, best


def average_precision(histogram: ScoreHistogram) -> float:
    """Threshold-free area under the precision-recall curve, stepped over histogram bins."""

    total_positive = int(histogram.positive.sum())
    if total_positive == 0:
        return 0.0
    tp = fp = 0
    area = previous_recall = 0.0
    for index in range(histogram.bins - 1, -1, -1):
        tp += int(histogram.positive[index])
        fp += int(histogram.negative[index])
        if tp + fp == 0:
            continue
        recall = tp / total_positive
        area += (recall - previous_recall) * (tp / (tp + fp))
        previous_recall = recall
    return area


def best_shared_threshold(
    histograms: dict[str, ScoreHistogram], low: float = 0.05, high: float = 0.95
) -> tuple[float, float]:
    """The one threshold maximising mean F1 across sources. Call on validation data only.

    Averaging per source stops the source with most labelled pixels from setting the threshold
    alone, which matters because a deployed model has exactly one.
    """

    bins = next(iter(histograms.values())).bins
    best_threshold, best_score = 0.5, -1.0
    for cut in range(int(low * bins), int(high * bins) + 1):
        threshold = cut / bins
        score = float(np.mean([h.metrics_at(threshold)["f1"] for h in histograms.values()]))
        if score > best_score:
            best_threshold, best_score = threshold, score
    return best_threshold, best_score


def multiclass_metrics(confusion: np.ndarray, classes: tuple[str, ...]) -> dict:
    """Per-class and macro metrics from a confusion matrix with rows = truth, columns = prediction."""

    per_class = {}
    f1s, ious = [], []
    for index, name in enumerate(classes):
        tp = int(confusion[index, index])
        fp = int(confusion[:, index].sum() - tp)
        fn = int(confusion[index, :].sum() - tp)
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-12)
        iou = tp / max(tp + fp + fn, 1)
        support = tp + fn
        per_class[name] = {
            "f1": f1,
            "iou": iou,
            "precision": precision,
            "recall": recall,
            "support_pixels": support,
        }
        if support > 0:
            f1s.append(f1)
            ious.append(iou)
    total = int(confusion.sum())
    return {
        "accuracy": float(np.trace(confusion)) / max(total, 1),
        "macro_f1": float(np.mean(f1s)) if f1s else 0.0,
        "macro_iou": float(np.mean(ious)) if ious else 0.0,
        "pixels": total,
        "per_class": per_class,
    }
