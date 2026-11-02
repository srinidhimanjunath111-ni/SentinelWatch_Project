"""Diagnose change model v1 on train and validation data only. The test split is never loaded.

    python -m training.diagnose_change --run-dir data/processed/runs/change_v1 --report reports/ml/change_v1_investigation.json

Runs on CPU, so it can share the machine with a training job. It measures: selection stability,
calibration and threshold position, change signal under each normalisation, co-registration of
dates, bands and labels, sliding-window agreement, and how enriched training crops are.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import torch

from app.ml.inference import sliding_window_logits
from app.ml.preprocessing import NORMALISE_PER_DATE, NORMALISE_POOLED, normalise_pair
from app.ml.siamese_unet import SiameseUNet
from data_tools.build_india_forest_change import load_records as load_forest
from data_tools.fetch_oscd import ChangeRecord, load_records
from training.dataset import ChangeSampler, to_reflectance
from training.metrics import ScoreHistogram, average_precision, logit_to_probability

DEVICE = "cpu"


def pixel_auc(scores: np.ndarray, truth: np.ndarray) -> float:
    order = np.argsort(scores)
    ranks = np.empty(len(scores))
    ranks[order] = np.arange(1, len(scores) + 1)
    positive = int(truth.sum())
    negative = len(truth) - positive
    return float((ranks[truth].sum() - positive * (positive + 1) / 2) / max(positive * negative, 1))


def signal_auc(record: ChangeRecord, mode: str) -> float:
    """How well the plain distance between normalised dates separates change, with no model."""

    seen = record.pixel_valid()
    before, after = normalise_pair(to_reflectance(record.before), to_reflectance(record.after), seen, mode)
    distance = np.linalg.norm(after - before, axis=0)
    scored = seen & (record.mask != 255)
    return pixel_auc(distance[scored], (record.mask == 1)[scored])


def correlation(first: np.ndarray, second: np.ndarray) -> float:
    first, second = first - first.mean(), second - second.mean()
    return float((first * second).sum() / np.sqrt((first * first).sum() * (second * second).sum() + 1e-9))


def best_offset(first: np.ndarray, second: np.ndarray, reach: int = 3) -> list[int]:
    scores = {
        (dy, dx): correlation(first[8:-8, 8:-8], np.roll(second, (dy, dx), (0, 1))[8:-8, 8:-8])
        for dy in range(-reach, reach + 1)
        for dx in range(-reach, reach + 1)
    }
    return list(max(scores, key=scores.get))


def checkpoint_scores(path: Path, records: dict[str, list[ChangeRecord]], label: str) -> dict:
    state = torch.load(path, map_location="cpu", weights_only=True)["model_state"]
    model = SiameseUNet(6, 32, 0.1)
    model.load_state_dict(state)
    model.eval()
    output = {}
    for split, group in records.items():
        histogram = ScoreHistogram()
        for record in group:
            everything = np.ones(record.mask.shape, dtype=bool)
            before, after = normalise_pair(
                to_reflectance(record.before), to_reflectance(record.after), everything, NORMALISE_POOLED
            )
            logits = sliding_window_logits(model, (before, after), 1, device=DEVICE)[0]
            histogram.update(logits, record.mask == 1, everything)
        threshold, best = histogram.best_threshold(0.001, 0.9999)
        centres = histogram.centres()
        probabilities = np.vectorize(logit_to_probability)(centres)
        output[split] = {
            "average_precision": average_precision(histogram),
            "best_f1": best["f1"],
            "best_threshold": threshold,
            "f1_at_0.5": histogram.metrics_at(0.5)["f1"],
            "recall_at_0.5": histogram.metrics_at(0.5)["recall"],
            "precision_at_0.5": histogram.metrics_at(0.5)["precision"],
            "f1_at_0.95": histogram.metrics_at(0.95)["f1"],
            "recall_at_0.95": histogram.metrics_at(0.95)["recall"],
            "mean_probability_change": float((histogram.positive * probabilities).sum() / max(histogram.positive.sum(), 1)),
            "mean_probability_no_change": float((histogram.negative * probabilities).sum() / max(histogram.negative.sum(), 1)),
        }
        print(label, split, output[split], flush=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    train, val = load_records("train"), load_records("val")
    history = json.loads((args.run_dir / "history.json").read_text(encoding="utf-8"))
    curve = [row["f1"] for row in history]
    report: dict = {
        "status": "COMPLETED",
        "key": "change_investigation",
        "created_at": datetime.now(UTC).isoformat(),
        "scope": "train and validation data only; the test split is not loaded",
        "selection": {
            "steps_run": history[-1]["step"],
            "early_stopping_fired": False,
            "best_step": history[int(np.argmax(curve))]["step"],
            "val_f1_by_step": {row["step"]: row["f1"] for row in history},
            "val_f1_mean": float(np.mean(curve)),
            "val_f1_std": float(np.std(curve)),
            "val_f1_step_to_step_mean_abs_change": float(np.mean(np.abs(np.diff(curve)))),
        },
    }
    report["checkpoints"] = {
        "best_step_400": checkpoint_scores(args.run_dir / "best.pt", {"val": val, "train": train}, "best"),
        "last_step_4000": checkpoint_scores(args.run_dir / "last.pt", {"val": val, "train": train}, "last"),
    }
    forest_train = [r for r in load_forest("train") if (r.mask == 1).sum() > 50]
    forest_val = [r for r in load_forest("val") if (r.mask == 1).sum() > 50]
    report["normalisation_signal_auc"] = {
        "meaning": "Pixel AUC of the distance between normalised dates; higher keeps more change signal",
        "oscd_by_city": {
            r.name: {"pooled": signal_auc(r, NORMALISE_POOLED), "per_date": signal_auc(r, NORMALISE_PER_DATE)}
            for r in train + val
        },
        "forest_train_mean": {
            "pooled": float(np.mean([signal_auc(r, NORMALISE_POOLED) for r in forest_train])),
            "per_date": float(np.mean([signal_auc(r, NORMALISE_PER_DATE) for r in forest_train])),
        },
        "forest_val_mean": {
            "pooled": float(np.mean([signal_auc(r, NORMALISE_POOLED) for r in forest_val])),
            "per_date": float(np.mean([signal_auc(r, NORMALISE_PER_DATE) for r in forest_val])),
        },
    }
    by_city = report["normalisation_signal_auc"]["oscd_by_city"].values()
    report["normalisation_signal_auc"]["oscd_mean"] = {
        "pooled": float(np.mean([c["pooled"] for c in by_city])),
        "per_date": float(np.mean([c["per_date"] for c in by_city])),
    }
    alignment = {}
    for record in train + val:
        before, after = to_reflectance(record.before), to_reflectance(record.after)
        difference = np.abs(after - before).mean(axis=0)
        change = record.mask == 1
        scores = {
            (dy, dx): float(difference[np.roll(change, (dy, dx), (0, 1))].mean())
            for dy in range(-3, 4)
            for dx in range(-3, 4)
        }
        alignment[record.name] = {
            "date_offset_B04": best_offset(before[2], after[2]),
            "date_offset_B08": best_offset(before[3], after[3]),
            "date_offset_B11": best_offset(before[4], after[4]),
            "band_offset_B11_to_B08": best_offset(before[3], before[4]),
            "label_offset_maximising_difference": list(max(scores, key=scores.get)),
        }
    report["alignment"] = alignment
    state = torch.load(args.run_dir / "last.pt", map_location="cpu", weights_only=True)["model_state"]
    model = SiameseUNet(6, 32, 0.1)
    model.load_state_dict(state)
    model.eval()
    blending = {}
    for record in val:
        everything = np.ones(record.mask.shape, dtype=bool)
        before, after = normalise_pair(
            to_reflectance(record.before), to_reflectance(record.after), everything, NORMALISE_POOLED
        )
        windowed = sliding_window_logits(model, (before, after), 1, device=DEVICE)[0]
        height, width = (side - side % 8 for side in before.shape[1:])
        with torch.inference_mode():
            whole = model(
                torch.from_numpy(before[None, :, :height, :width]),
                torch.from_numpy(after[None, :, :height, :width]),
            )[0, 0].numpy()
        blending[record.name] = {
            "logit_correlation_with_whole_image": float(np.corrcoef(windowed[:height, :width].ravel(), whole.ravel())[0, 1]),
            "mean_absolute_probability_difference": float(
                np.abs(torch.sigmoid(torch.from_numpy(windowed[:height, :width])).numpy() - torch.sigmoid(torch.from_numpy(whole)).numpy()).mean()
            ),
        }
    report["sliding_window"] = blending
    sampler = ChangeSampler(train, seed=1)
    fractions = [float((sampler.batch(6)[2] == 1).mean()) for _ in range(150)]
    report["crop_sampling"] = {
        "base_rate": float(sum((r.mask == 1).sum() for r in train) / sum(r.mask.size for r in train)),
        "enriched_batches_change_fraction": float(np.mean(fractions)),
        "v1_batches_change_fraction_measured_before_fix": 0.038,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {args.report}")


if __name__ == "__main__":
    main()
