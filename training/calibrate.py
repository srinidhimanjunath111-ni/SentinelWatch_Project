"""Fit the change model's threshold and temperature on out-of-fold validation scores.

    python -m training.calibrate --folds RUN_DIR [RUN_DIR ...] --checkpoint CKPT --report REPORT

Each fold holds out different official train cities, so summing fold histograms gives scores from
models that never saw the city being scored: a far steadier basis for a threshold than two cities.
Test data is never read. The temperature changes the probability scale, not the ranking, and the
threshold is stored on the calibrated scale that inference outputs.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import torch

from training.metrics import (
    THRESHOLD_HIGH,
    THRESHOLD_LOW,
    ScoreHistogram,
    average_precision,
    best_shared_threshold,
    logit_to_probability,
    probability_to_logit,
    temperature_nll,
)

TEMPERATURES = np.arange(0.5, 8.01, 0.05)


def load_histograms(run_dir: Path) -> dict[str, ScoreHistogram]:
    histograms: dict[str, ScoreHistogram] = {}
    with np.load(run_dir / "val_histograms.npz", allow_pickle=False) as data:
        for name in data.files:
            source, part = name.rsplit("_", 1)
            histogram = histograms.setdefault(source, ScoreHistogram())
            setattr(histogram, part, data[name])
    return histograms


def per_city_scores(run_dirs: list[Path]) -> dict[str, dict]:
    """Each OSCD validation city scored by the fold model that did not train on it, on CPU."""

    from training.evaluate import load_change_model
    from training.train import load_change_records, predict_record

    output: dict[str, dict] = {}
    for run in run_dirs:
        cities = json.loads((run / "summary.json").read_text(encoding="utf-8"))["oscd_val_cities"]
        model, payload = load_change_model(run / "best.pt", "cpu")
        for record in load_change_records(["oscd"], "val", cities):
            logits, scored = predict_record(
                model, record, "cpu", str(payload["normalisation"]), align_dates=bool(payload["align_dates"])
            )
            histogram = ScoreHistogram()
            histogram.update(logits, record.mask == 1, scored)
            threshold, best = histogram.best_threshold()
            output[record.name] = {
                "fold": run.name,
                "change_fraction": float((record.mask == 1).mean()),
                "average_precision": average_precision(histogram),
                "f1_at_raw_0.5": histogram.metrics_at(0.5)["f1"],
                "best_f1": best["f1"],
                "best_threshold_raw": threshold,
            }
    return output


def _final_model_check(checkpoint: Path, raw_threshold: float) -> dict | None:
    """Does the out-of-fold threshold suit the final model? Only its forest validation can say."""

    run = checkpoint.parent
    if not (run / "val_histograms.npz").is_file():
        return None
    result = {}
    for source, histogram in load_histograms(run).items():
        best_threshold, best = histogram.best_threshold()
        result[source] = {
            "f1_at_out_of_fold_threshold": histogram.metrics_at(raw_threshold)["f1"],
            "recall_at_out_of_fold_threshold": histogram.metrics_at(raw_threshold)["recall"],
            "precision_at_out_of_fold_threshold": histogram.metrics_at(raw_threshold)["precision"],
            "own_best_f1": best["f1"],
            "own_best_threshold_raw": best_threshold,
            "average_precision": average_precision(histogram),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--folds", type=Path, nargs="+", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    folds = [load_histograms(run) for run in args.folds]
    pooled: dict[str, ScoreHistogram] = {}
    for fold in folds:
        for source, histogram in fold.items():
            pooled.setdefault(source, ScoreHistogram()).add(histogram)
    raw_threshold, mean_f1 = best_shared_threshold(pooled)
    # Each source counts equally when fitting T, as it does when choosing the threshold.
    nll = [float(np.mean([temperature_nll(h, t) for h in pooled.values()])) for t in TEMPERATURES]
    temperature = float(TEMPERATURES[int(np.argmin(nll))])
    calibrated = logit_to_probability(probability_to_logit(raw_threshold) / temperature)
    # Honest estimate: score each fold at a threshold chosen on the other folds alone.
    leave_one_out = []
    for index, fold in enumerate(folds):
        others: dict[str, ScoreHistogram] = {}
        for other_index, other in enumerate(folds):
            if other_index != index:
                for source, histogram in other.items():
                    others.setdefault(source, ScoreHistogram()).add(histogram)
        threshold, _ = best_shared_threshold(others)
        leave_one_out.append(
            {
                "fold": args.folds[index].name,
                "threshold_raw": threshold,
                "own_best_threshold_raw": best_shared_threshold(fold)[0],
                "per_source": {s: h.metrics_at(threshold) for s, h in fold.items()},
            }
        )
    payload = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
    payload["threshold"] = calibrated
    payload["temperature"] = temperature
    payload["threshold_source"] = (
        f"out-of-fold validation over {len(folds)} folds of the official OSCD train cities plus "
        "the India forest validation split; test data never read"
    )
    payload["version"] = args.version
    torch.save(payload, args.checkpoint)
    at_threshold = {source: h.metrics_at(raw_threshold) for source, h in pooled.items()}
    oscd_cities = sorted(
        city
        for run in args.folds
        for city in json.loads((run / "summary.json").read_text(encoding="utf-8"))["oscd_val_cities"]
    )
    report = {
        "status": "COMPLETED",
        "key": "change_calibration",
        "version": args.version,
        "created_at": datetime.now(UTC).isoformat(),
        "split": "validation (out of fold)",
        "folds": [str(run) for run in args.folds],
        "oscd_cities": oscd_cities,
        "threshold_raw": raw_threshold,
        "threshold_calibrated": calibrated,
        "temperature": temperature,
        "search_limits_raw": [THRESHOLD_LOW, THRESHOLD_HIGH],
        "threshold_at_search_limit": raw_threshold <= THRESHOLD_LOW * 1.01 or raw_threshold >= THRESHOLD_HIGH * 0.999,
        "mean_source_f1": mean_f1,
        "per_source": at_threshold,
        "leave_one_fold_out": leave_one_out,
        "per_city": per_city_scores(args.folds),
        "final_model_forest_val": _final_model_check(args.checkpoint, raw_threshold),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("threshold_raw", "threshold_calibrated", "temperature", "mean_source_f1", "threshold_at_search_limit", "per_source", "final_model_forest_val")}, indent=2))
    for row in leave_one_out:
        print(row["fold"], round(row["threshold_raw"], 3), round(row["own_best_threshold_raw"], 3), {s: round(m["f1"], 3) for s, m in row["per_source"].items()})


if __name__ == "__main__":
    main()
