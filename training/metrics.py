"""Metric helpers shared by legitimate training and held-out evaluation runs."""

from __future__ import annotations

from typing import Any

import torch


def counts_from_predictions(
    prediction: torch.Tensor, target: torch.Tensor
) -> tuple[int, int, int, int]:
    """Return binary TP, FP, TN, FN counts for same-shaped boolean tensors."""

    prediction = prediction.bool()
    target = target.bool()
    true_positive = int((prediction & target).sum().item())
    false_positive = int((prediction & ~target).sum().item())
    true_negative = int((~prediction & ~target).sum().item())
    false_negative = int((~prediction & target).sum().item())
    return true_positive, false_positive, true_negative, false_negative


def binary_metrics(
    true_positive: int, false_positive: int, true_negative: int, false_negative: int
) -> dict[str, float | int]:
    """Calculate metrics from aggregate counts without hiding class imbalance."""

    total = true_positive + false_positive + true_negative + false_negative
    precision = true_positive / max(true_positive + false_positive, 1)
    recall = true_positive / max(true_positive + false_negative, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-12)
    iou = true_positive / max(true_positive + false_positive + false_negative, 1)
    accuracy = (true_positive + true_negative) / max(total, 1)
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "true_negative": true_negative,
        "false_negative": false_negative,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "dice": f1,
        "iou": iou,
    }


class BinaryMetricAccumulator:
    """Accumulate thresholded metrics and raw scores over an evaluation split."""

    def __init__(self, threshold: float = 0.5, collect_scores: bool = False):
        self.threshold = threshold
        self.collect_scores = collect_scores
        self.true_positive = 0
        self.false_positive = 0
        self.true_negative = 0
        self.false_negative = 0
        self._scores: list[float] = []
        self._targets: list[int] = []

    def update(self, probabilities: torch.Tensor, target: torch.Tensor) -> None:
        predicted = probabilities >= self.threshold
        tp, fp, tn, fn = counts_from_predictions(predicted, target)
        self.true_positive += tp
        self.false_positive += fp
        self.true_negative += tn
        self.false_negative += fn
        if self.collect_scores:
            self._scores.extend(probabilities.detach().flatten().float().cpu().tolist())
            self._targets.extend(target.detach().flatten().bool().cpu().int().tolist())

    def result(self) -> dict[str, Any]:
        result: dict[str, Any] = binary_metrics(
            self.true_positive, self.false_positive, self.true_negative, self.false_negative
        )
        if self.collect_scores and len(set(self._targets)) == 2:
            try:
                from sklearn.metrics import average_precision_score, roc_auc_score

                result["roc_auc"] = float(roc_auc_score(self._targets, self._scores))
                result["pr_auc"] = float(average_precision_score(self._targets, self._scores))
            except ImportError:
                result["roc_auc"] = None
                result["pr_auc"] = None
        else:
            result["roc_auc"] = None
            result["pr_auc"] = None
        return result
