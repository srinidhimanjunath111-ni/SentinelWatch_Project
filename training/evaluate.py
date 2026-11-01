"""Held-out evaluation and report export. Test numbers are produced only here.

    python -m training.evaluate change --checkpoint CKPT --dataset oscd --report reports/ml/change_v1_oscd_test.json
    python -m training.evaluate export-change --run-dir data/processed/runs/change_v1 --report reports/ml/change_v1_training.json
    python -m training.evaluate landcover --checkpoint CKPT --report reports/ml/landcover_test.json

A test report for a given checkpoint and dataset is written once. Re-running is refused unless
--force is given, so a test split cannot be consulted repeatedly by accident while tuning.
Thresholds come from the checkpoint, where validation put them; nothing is tuned here.
"""

from __future__ import annotations

import os

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.65")
os.environ.setdefault("PYTORCH_MPS_LOW_WATERMARK_RATIO", "0.5")

import argparse
import hashlib
import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import numpy as np
import torch

from app.ml.inference import LANDCOVER_CLASSES, select_device, sliding_window_logits
from app.ml.landcover_unet import LandCoverUNet
from app.ml.preprocessing import NORMALISE_POOLED, normalise_scene
from app.ml.siamese_unet import SiameseUNet
from data_tools.fetch_oscd import IGNORE, ChangeRecord
from training import reporting
from training.dataset import to_reflectance
from training.metrics import binary_metrics, multiclass_metrics
from training.train import load_change_records, predict_record

SAMPLE_ROWS = 3
NOTICE = (
    "Measures physical change segmentation on held-out data. It does not measure a legality "
    "determination, which a satellite cannot make."
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _refuse_repeat(report: Path, sha: str, force: bool) -> None:
    if force or not report.is_file():
        return
    existing = json.loads(report.read_text(encoding="utf-8"))
    if existing.get("checkpoint_sha256") == sha:
        raise SystemExit(f"{report} already holds this checkpoint's test result; use --force to redo it.")


def load_change_model(path: Path, device: str) -> tuple[SiameseUNet, dict]:
    payload = torch.load(path, map_location="cpu", weights_only=True)
    config = payload["config"]
    model = SiameseUNet(
        in_channels=int(config["in_channels"]),
        base_channels=int(config["base_channels"]),
        dropout=float(config.get("dropout", 0.0)),
        norm=str(config.get("norm", "batch")),
    )
    model.load_state_dict(payload["model_state"])
    return model.to(device).eval(), payload


def evaluate_change(args: argparse.Namespace) -> None:
    sha = file_sha256(args.checkpoint)
    _refuse_repeat(args.report, sha, args.force)
    device = args.device or select_device()
    model, payload = load_change_model(args.checkpoint, device)
    threshold = float(payload["threshold"])
    normalisation = str(payload.get("normalisation", NORMALISE_POOLED))
    records: list[ChangeRecord] = load_change_records([args.dataset], "test")
    pooled = [0, 0, 0, 0]
    per_item: dict[str, dict] = {}
    sample_rows = []
    started = perf_counter()
    for record in records:
        probability, valid = predict_record(model, record, device, normalisation)
        prediction = probability >= threshold
        truth = record.mask == 1
        shown_truth = np.where(valid, record.mask, IGNORE)
        tp = int((prediction & truth & valid).sum())
        fp = int((prediction & ~truth & valid).sum())
        tn = int((~prediction & ~truth & valid).sum())
        fn = int((~prediction & truth & valid).sum())
        for slot, value in enumerate((tp, fp, tn, fn)):
            pooled[slot] += value
        per_item[record.name] = binary_metrics(tp, fp, tn, fn)
        # First scenes with change, in the fixed list order, so figures are not picked by score.
        if len(sample_rows) < SAMPLE_ROWS and tp + fn > 0:
            sample_rows.append(
                {
                    "name": record.name,
                    "before": reporting.true_colour(to_reflectance(record.before)),
                    "after": reporting.true_colour(to_reflectance(record.after)),
                    "truth": shown_truth,
                    "prediction": prediction,
                    "f1": per_item[record.name]["f1"],
                }
            )
    report = {
        "status": "COMPLETED",
        "key": "change",
        "split": "test",
        "dataset": f"{args.dataset}_test",
        "created_at": datetime.now(UTC).isoformat(),
        "checkpoint": str(args.checkpoint),
        "checkpoint_sha256": sha,
        "version": str(payload.get("version")),
        "threshold": threshold,
        "threshold_source": "tuned on validation data only and stored in the checkpoint",
        "samples": len(records),
        "device": device,
        "elapsed_seconds": perf_counter() - started,
        "metrics": binary_metrics(*pooled),
        "aggregation": "micro: pixel counts pooled over all test scenes, then one F1",
        "macro_f1": float(np.mean([m["f1"] for m in per_item.values()])),
        "per_item": per_item,
        "notice": NOTICE,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    prefix = args.report.with_suffix("")
    tp, fp, tn, fn = pooled
    reporting.confusion_matrix(
        np.array([[tn, fp], [fn, tp]]),
        ("no change", "change"),
        f"{report['dataset']}, {report['version']}, threshold {threshold:.3f}",
        Path(f"{prefix}_confusion.png"),
    )
    reporting.change_samples(
        sample_rows,
        f"{report['dataset']}, {report['version']}: first scenes in list order",
        Path(f"{prefix}_samples.png"),
    )
    print(json.dumps({k: report[k] for k in ("dataset", "version", "threshold", "metrics", "macro_f1")}, indent=2))


def export_change(args: argparse.Namespace) -> None:
    summary = json.loads((args.run_dir / "summary.json").read_text(encoding="utf-8"))
    history = json.loads((args.run_dir / "history.json").read_text(encoding="utf-8"))
    train = load_change_records(summary["datasets"], "train")
    summary["train_pixels"] = int(sum(r.mask.size for r in train))
    # Mean pixels per step for the crop sizes and batch-size rule in ChangeSampler.batch.
    per_size = [max(4, min(summary["batch_size"], int(summary["batch_size"] * (192 / s) ** 2))) * s * s for s in summary["crop_sizes"]]
    pixels_per_step = float(np.mean(per_size))
    summary["pixels_per_step"] = pixels_per_step
    summary["epochs_equivalent"] = round(summary["best_step"] * pixels_per_step / summary["train_pixels"])
    summary["status"] = "COMPLETED"
    summary["history"] = history
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    reporting.learning_curve(
        history,
        "step",
        "training step",
        "f1",
        "F1",
        f"Change model {summary['version']}, trained on {', '.join(summary['datasets'])}",
        Path(f"{args.report.with_suffix('')}_curve.png"),
    )
    print(f"Wrote {args.report}")


def evaluate_landcover(args: argparse.Namespace) -> None:
    from data_tools.build_india_landcover import load_chips

    sha = file_sha256(args.checkpoint)
    _refuse_repeat(args.report, sha, args.force)
    device = args.device or select_device()
    payload = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
    config = payload["config"]
    model = LandCoverUNet(
        in_channels=int(config["in_channels"]),
        classes=len(LANDCOVER_CLASSES),
        base_channels=int(config["base_channels"]),
        dropout=float(config.get("dropout", 0.0)),
    )
    model.load_state_dict(payload["model_state"])
    model.to(device).eval()
    chips = load_chips("test")
    count = len(LANDCOVER_CLASSES)
    confusion = np.zeros((count, count), dtype=np.int64)
    per_tile: dict[str, np.ndarray] = {}
    sample_rows = []
    started = perf_counter()
    for index, chip in enumerate(chips):
        valid = chip["label"] != IGNORE
        scene = normalise_scene(to_reflectance(chip["image"]), payload["band_mean"], payload["band_std"], valid)
        logits = sliding_window_logits(model, (scene,), count, device=device)
        prediction = logits.argmax(axis=0)
        cells = chip["label"][valid].astype(np.int64) * count + prediction[valid]
        matrix = np.bincount(cells, minlength=count * count).reshape(count, count)
        confusion += matrix
        tile = str(chip["tile"])
        per_tile[tile] = per_tile.get(tile, np.zeros_like(matrix)) + matrix
        # Evenly spaced through the fixed order, so figures are not picked by score.
        if index % max(1, len(chips) // 4) == 0 and len(sample_rows) < 4:
            shown = prediction.astype(np.uint8)
            shown[~valid] = IGNORE
            sample_rows.append(
                {
                    "name": f"{tile} / {chip['name']}",
                    "rgb": reporting.true_colour(to_reflectance(chip["image"]), valid),
                    "truth": chip["label"],
                    "prediction": shown,
                }
            )
    metrics = multiclass_metrics(confusion, LANDCOVER_CLASSES)
    report = {
        "status": "COMPLETED",
        "key": "landcover",
        "split": "test",
        "dataset": "india_landcover_test",
        "created_at": datetime.now(UTC).isoformat(),
        "checkpoint": str(args.checkpoint),
        "checkpoint_sha256": sha,
        "version": str(payload.get("version")),
        "samples": len(chips),
        "test_tiles": sorted(per_tile),
        "device": device,
        "elapsed_seconds": perf_counter() - started,
        "metrics": metrics,
        "confusion_matrix": confusion.tolist(),
        "classes": list(LANDCOVER_CLASSES),
        "per_tile_accuracy": {t: float(np.trace(m)) / max(int(m.sum()), 1) for t, m in per_tile.items()},
        "notice": (
            "Reference labels are ESA WorldCover 2021 plus Maus et al. mining polygons, which carry "
            "their own errors. The mine class reports footprint, never legality."
        ),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    prefix = args.report.with_suffix("")
    reporting.confusion_matrix(
        confusion, LANDCOVER_CLASSES, "Land cover, held-out India tiles", Path(f"{prefix}_confusion.png")
    )
    reporting.landcover_samples(
        sample_rows,
        LANDCOVER_CLASSES,
        "Land cover, held-out India tiles: evenly spaced chips",
        Path(f"{prefix}_samples.png"),
    )
    print(json.dumps({k: metrics[k] for k in ("accuracy", "macro_f1", "macro_iou", "per_class")}, indent=2))


def timing(args: argparse.Namespace) -> None:
    """Measure real CPU inference time and peak memory on a real 500 x 500 AOI cut from OSCD."""

    import resource

    from app.ml.inference import ChangeDetector, LandCoverClassifier

    os.environ["SENTINELWATCH_ML_DEVICE"] = "cpu"
    record = next(r for r in load_change_records(["oscd"], "test") if min(r.mask.shape) >= 500)
    before = to_reflectance(record.before[:, :500, :500])
    after = to_reflectance(record.after[:, :500, :500])
    valid = np.ones((500, 500), dtype=bool)
    output: dict = {
        "status": "COMPLETED",
        "created_at": datetime.now(UTC).isoformat(),
        "aoi_pixels": "500x500",
        "aoi_source": f"real pixels, OSCD {record.name}",
        "device": "cpu",
        "cpu_threads": torch.get_num_threads(),
        "machine": platform.platform(),
        "note": "Measured while other agents were using the machine, so treat as a rough upper bound.",
    }
    detector = ChangeDetector()
    if detector.available:
        runs = []
        for _ in range(3):
            started = perf_counter()
            detector.predict(before, after, valid)
            runs.append(perf_counter() - started)
        output["change_seconds_median"] = float(np.median(runs))
        output["change_seconds_runs"] = runs
    classifier = LandCoverClassifier()
    if classifier.available:
        runs = []
        for _ in range(3):
            started = perf_counter()
            classifier.predict(before, valid)
            runs.append(perf_counter() - started)
        output["landcover_seconds_median"] = float(np.median(runs))
        output["landcover_seconds_runs"] = runs
    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    output["peak_rss_mb"] = peak / (1024**2 if platform.system() == "Darwin" else 1024)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    change = commands.add_parser("change")
    change.add_argument("--checkpoint", type=Path, required=True)
    change.add_argument("--dataset", choices=("oscd", "india_forest"), required=True)
    change.add_argument("--report", type=Path, required=True)
    change.add_argument("--device", default=None)
    change.add_argument("--force", action="store_true")
    change.set_defaults(handler=evaluate_change)
    export = commands.add_parser("export-change")
    export.add_argument("--run-dir", type=Path, required=True)
    export.add_argument("--report", type=Path, required=True)
    export.set_defaults(handler=export_change)
    landcover = commands.add_parser("landcover")
    landcover.add_argument("--checkpoint", type=Path, required=True)
    landcover.add_argument("--report", type=Path, required=True)
    landcover.add_argument("--device", default=None)
    landcover.add_argument("--force", action="store_true")
    landcover.set_defaults(handler=evaluate_landcover)
    timer = commands.add_parser("timing")
    timer.add_argument("--report", type=Path, default=Path("reports/ml/inference_timing.json"))
    timer.set_defaults(handler=timing)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
