"""Metric helpers shared by training, validation and held-out evaluation."""

from __future__ import annotations

import numpy as np

HISTOGRAM_BINS = 480
LOGIT_LIMIT = 12.0
# Both search limits are far from anything a useful threshold should reach. A result at either one
# means the model is badly calibrated, and callers report it rather than hide it.
THRESHOLD_LOW = 0.01
THRESHOLD_HIGH = 0.995


def probability_to_logit(probability: float) -> float:
    return float(np.log(probability / (1.0 - probability)))


def logit_to_probability(logit: float) -> float:
    return float(1.0 / (1.0 + np.exp(-logit)))


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
    """Per-class histograms over logits, so a threshold sweep needs no per-pixel storage.

    Bins are in logit space because a saturated model puts most scores within a hair of 0 or 1.
    Uniform probability bins cannot resolve those, which hid the true optimum for change model v1.
    """

    def __init__(self, bins: int = HISTOGRAM_BINS, limit: float = LOGIT_LIMIT):
        self.bins = bins
        self.limit = limit
        self.positive = np.zeros(bins, dtype=np.int64)
        self.negative = np.zeros(bins, dtype=np.int64)

    def update(self, logits: np.ndarray, target: np.ndarray, valid: np.ndarray) -> None:
        scaled = (np.clip(logits[valid], -self.limit, self.limit) + self.limit) / (2 * self.limit)
        indices = np.minimum((scaled * self.bins).astype(np.int64), self.bins - 1)
        labels = target[valid].astype(bool)
        self.positive += np.bincount(indices[labels], minlength=self.bins)
        self.negative += np.bincount(indices[~labels], minlength=self.bins)

    def add(self, other: ScoreHistogram) -> None:
        self.positive += other.positive
        self.negative += other.negative

    def centres(self) -> np.ndarray:
        width = 2 * self.limit / self.bins
        return -self.limit + (np.arange(self.bins) + 0.5) * width

    def cut_for_logit(self, logit: float) -> int:
        position = (logit + self.limit) / (2 * self.limit) * self.bins
        return int(np.clip(np.ceil(position), 0, self.bins))

    def logit_at(self, cut: int) -> float:
        return -self.limit + cut * 2 * self.limit / self.bins

    def metrics_at_cut(self, cut: int) -> dict[str, float | int]:
        tp = int(self.positive[cut:].sum())
        fn = int(self.positive[:cut].sum())
        fp = int(self.negative[cut:].sum())
        tn = int(self.negative[:cut].sum())
        return binary_metrics(tp, fp, tn, fn)

    def metrics_at(self, threshold: float) -> dict[str, float | int]:
        """Metrics at a probability threshold on the raw (temperature 1) scale."""

        return self.metrics_at_cut(self.cut_for_logit(probability_to_logit(threshold)))

    def best_threshold(self, low: float = THRESHOLD_LOW, high: float = THRESHOLD_HIGH) -> tuple[float, dict]:
        """Raw-scale threshold maximising F1. Call this on validation data only."""

        first = self.cut_for_logit(probability_to_logit(low))
        last = self.cut_for_logit(probability_to_logit(high))
        best_cut = max(range(first, last + 1), key=lambda cut: self.metrics_at_cut(cut)["f1"])
        return logit_to_probability(self.logit_at(best_cut)), self.metrics_at_cut(best_cut)


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
    histograms: dict[str, ScoreHistogram],
    low: float = THRESHOLD_LOW,
    high: float = THRESHOLD_HIGH,
) -> tuple[float, float]:
    """The one raw-scale threshold maximising mean F1 across sources. Validation data only.

    Averaging per source stops the source with most labelled pixels from setting the threshold
    alone, which matters because a deployed model has exactly one.
    """

    any_histogram = next(iter(histograms.values()))
    first = any_histogram.cut_for_logit(probability_to_logit(low))
    last = any_histogram.cut_for_logit(probability_to_logit(high))
    best_cut, best_score = first, -1.0
    for cut in range(first, last + 1):
        score = float(np.mean([h.metrics_at_cut(cut)["f1"] for h in histograms.values()]))
        if score > best_score:
            best_cut, best_score = cut, score
    return logit_to_probability(any_histogram.logit_at(best_cut)), best_score


def temperature_nll(histogram: ScoreHistogram, temperature: float) -> float:
    """Mean negative log likelihood of the histogram's scores after dividing logits by T."""

    centres = histogram.centres() / temperature
    positive_loss = np.logaddexp(0.0, -centres)
    negative_loss = np.logaddexp(0.0, centres)
    total = histogram.positive.sum() + histogram.negative.sum()
    return float(((histogram.positive * positive_loss) + (histogram.negative * negative_loss)).sum() / max(total, 1))


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
