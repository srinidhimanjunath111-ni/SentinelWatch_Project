"""Evaluate a saved Siamese U-Net on a held-out test split.

This command is deliberately separate from training: test metrics are never used
to select a checkpoint.  Its JSON report is the only source that should be
registered as a model-performance claim in the application database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import torch
from torch.utils.data import DataLoader

from app.ml.siamese_unet import SiameseUNet
from training.dataset import ChangePairDataset
from training.metrics import BinaryMetricAccumulator
from training.train import autocast_context, combined_loss


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate a SentinelWatch Siamese U-Net")
    parser.add_argument("--data", type=Path, required=True, help="Prepared dataset root")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--image-size", type=int, default=256)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--workers", type=int, default=0, help="Use 0 on Windows by default")
    parser.add_argument("--threshold", type=float, default=None, help="Fixed validation-selected threshold")
    parser.add_argument("--mixed-precision", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("reports/evaluation"))
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@torch.no_grad()
def evaluate(
    model: torch.nn.Module,
    loader: DataLoader,
    device: torch.device,
    *,
    threshold: float,
    use_mixed_precision: bool,
    pos_weight: float,
) -> dict[str, float | int | None]:
    """Compute aggregate test counts/metrics and real runtime measurements."""

    model.eval()
    accumulator = BinaryMetricAccumulator(threshold=threshold, collect_scores=True)
    loss_function = torch.nn.BCEWithLogitsLoss(
        pos_weight=torch.tensor([pos_weight], device=device)
    )
    losses: list[float] = []
    pixels = 0
    started = perf_counter()
    for batch in loader:
        before = batch["before"].to(device, non_blocking=use_mixed_precision)
        after = batch["after"].to(device, non_blocking=use_mixed_precision)
        target = batch["mask"].to(device, non_blocking=use_mixed_precision)
        with autocast_context(device, use_mixed_precision):
            logits = model(before, after)
            loss = combined_loss(logits, target, loss_function)
            probabilities = torch.sigmoid(logits)
        losses.append(float(loss.detach().cpu()))
        accumulator.update(probabilities, target)
        pixels += target.numel()
    elapsed = perf_counter() - started
    metrics = accumulator.result()
    metrics.update(
        {
            "loss": sum(losses) / max(len(losses), 1),
            "samples": len(loader.dataset),
            "pixels": pixels,
            "elapsed_seconds": elapsed,
            "samples_per_second": len(loader.dataset) / max(elapsed, 1e-9),
            "pixels_per_second": pixels / max(elapsed, 1e-9),
            "threshold": threshold,
        }
    )
    return metrics


def main() -> None:
    args = parse_args()
    if not args.checkpoint.is_file():
        raise FileNotFoundError(f"Checkpoint does not exist: {args.checkpoint}")
    if args.batch_size < 1 or args.image_size < 32:
        raise ValueError("--batch-size must be positive and --image-size must be at least 32.")
    checkpoint = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    if not isinstance(checkpoint, dict) or "model_state" not in checkpoint:
        raise ValueError("Checkpoint must contain a model_state mapping created by training.train.")
    config = checkpoint.get("config", {})
    checkpoint_training = checkpoint.get("training", {})
    threshold = float(args.threshold if args.threshold is not None else checkpoint_training.get("threshold", 0.5))
    if not 0 < threshold < 1:
        raise ValueError("--threshold must be between 0 and 1.")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    use_amp = bool(args.mixed_precision and device.type == "cuda")
    model = SiameseUNet(base_channels=int(config.get("base_channels", 32))).to(device)
    model.load_state_dict(checkpoint["model_state"])
    dataset = ChangePairDataset(args.data, "test", args.image_size, augment=False)
    loader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.workers,
        pin_memory=use_amp,
    )
    metrics = evaluate(
        model,
        loader,
        device,
        threshold=threshold,
        use_mixed_precision=use_amp,
        pos_weight=float(checkpoint_training.get("pos_weight", 1.0)),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / f"{args.checkpoint.stem}.test_metrics.json"
    report = {
        "status": "COMPLETED",
        "created_at": datetime.now(UTC).isoformat(),
        "split": "test",
        "dataset": str(args.data),
        "checkpoint": str(args.checkpoint),
        "checkpoint_sha256": file_sha256(args.checkpoint),
        "checkpoint_epoch": checkpoint.get("epoch"),
        "architecture": "Siamese U-Net shared encoder",
        "model_config": config,
        "training_provenance": checkpoint_training,
        "evaluation_config": {
            "image_size": args.image_size,
            "batch_size": args.batch_size,
            "workers": args.workers,
            "device": str(device),
            "mixed_precision": use_amp,
        },
        "metrics": metrics,
        "notice": (
            "Metrics were calculated on the supplied held-out test split. They measure binary "
            "physical-change masks only; they do not demonstrate illegal-activity classification."
        ),
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"Saved held-out evaluation report: {report_path}")


if __name__ == "__main__":
    main()
