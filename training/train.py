"""Train a Siamese U-Net on lawful paired binary change masks.

Example:
    python -m training.train --data data/processed/levir-256 --epochs 40 --batch-size 8

The script emits only metrics measured during the run. It never creates a synthetic
checkpoint or inserts a performance claim into the dashboard automatically.
"""

from __future__ import annotations

import argparse
import json
import random
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader

from app.ml.siamese_unet import SiameseUNet
from training.dataset import ChangePairDataset
from training.metrics import BinaryMetricAccumulator


def dice_loss(logits: torch.Tensor, target: torch.Tensor, smooth: float = 1.0) -> torch.Tensor:
    probability = torch.sigmoid(logits)
    intersection = (probability * target).sum(dim=(1, 2, 3))
    denominator = probability.sum(dim=(1, 2, 3)) + target.sum(dim=(1, 2, 3))
    return 1.0 - ((2.0 * intersection + smooth) / (denominator + smooth)).mean()


def combined_loss(logits: torch.Tensor, target: torch.Tensor, bce: nn.Module) -> torch.Tensor:
    """Imbalance-aware BCE plus Dice: explainable and suitable for sparse masks."""

    return 0.55 * bce(logits, target) + 0.45 * dice_loss(logits, target)


def autocast_context(device: torch.device, enabled: bool):
    if enabled:
        return torch.autocast(device_type=device.type, enabled=True)
    return nullcontext()


@torch.no_grad()
def validation_metrics(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
    *,
    threshold: float = 0.5,
    use_mixed_precision: bool = False,
) -> dict[str, float | int | None]:
    model.eval()
    accumulator = BinaryMetricAccumulator(threshold=threshold)
    started = perf_counter()
    pixels = 0
    for batch in loader:
        before, after = batch["before"].to(device), batch["after"].to(device)
        target = batch["mask"].to(device)
        with autocast_context(device, use_mixed_precision):
            probability = torch.sigmoid(model(before, after))
        accumulator.update(probability, target)
        pixels += target.numel()
    metrics = accumulator.result()
    elapsed = perf_counter() - started
    metrics["seconds"] = elapsed
    metrics["pixels_per_second"] = pixels / max(elapsed, 1e-9)
    return metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train SentinelWatch Siamese U-Net")
    parser.add_argument("--data", type=Path, required=True, help="Prepared dataset root")
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--image-size", type=int, default=256)
    parser.add_argument("--base-channels", type=int, default=32)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--workers", type=int, default=0, help="Use 0 on Windows for reliable startup")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--early-stopping-patience", type=int, default=10)
    parser.add_argument("--mixed-precision", action="store_true", help="Enable CUDA mixed precision")
    parser.add_argument("--output", type=Path, default=Path("models/checkpoints/siamese_unet_best.pt"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports/training"))
    return parser.parse_args()


def _plot_history(records: list[dict], destination: Path) -> bool:
    try:
        import matplotlib.pyplot as plot
    except ImportError:
        return False
    epochs = [record["epoch"] for record in records]
    figure, axes = plot.subplots(1, 2, figsize=(10, 4))
    axes[0].plot(epochs, [record["train_loss"] for record in records], label="train loss")
    axes[0].set_title("Training loss")
    axes[0].set_xlabel("epoch")
    axes[0].grid(alpha=0.25)
    axes[1].plot(epochs, [record["f1"] for record in records], label="F1")
    axes[1].plot(epochs, [record["iou"] for record in records], label="IoU")
    axes[1].set_title("Validation segmentation metrics")
    axes[1].set_xlabel("epoch")
    axes[1].set_ylim(0, 1)
    axes[1].legend()
    axes[1].grid(alpha=0.25)
    figure.tight_layout()
    destination.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(destination, dpi=160)
    plot.close(figure)
    return True


def main() -> None:
    args = parse_args()
    if args.epochs < 1 or args.batch_size < 1 or args.image_size < 32:
        raise ValueError("--epochs, --batch-size, and --image-size must be meaningful positive values.")
    if not 0 < args.threshold < 1:
        raise ValueError("--threshold must be between 0 and 1.")
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    use_amp = bool(args.mixed_precision and device.type == "cuda")
    train_set = ChangePairDataset(args.data, "train", args.image_size, augment=True)
    validation_set = ChangePairDataset(args.data, "val", args.image_size, augment=False)
    train_loader = DataLoader(
        train_set, batch_size=args.batch_size, shuffle=True, num_workers=args.workers, pin_memory=use_amp
    )
    validation_loader = DataLoader(
        validation_set, batch_size=args.batch_size, shuffle=False, num_workers=args.workers, pin_memory=use_amp
    )
    model = SiameseUNet(base_channels=args.base_channels).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="max", patience=4, factor=0.5
    )
    positive_pixels = sum(float(batch["mask"].sum()) for batch in DataLoader(train_set, batch_size=1))
    total_pixels = len(train_set) * args.image_size * args.image_size
    pos_weight = min(20.0, max(1.0, (total_pixels - positive_pixels) / max(positive_pixels, 1)))
    bce = nn.BCEWithLogitsLoss(pos_weight=torch.tensor([pos_weight], device=device))
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)
    best_f1, stalled_epochs = -1.0, 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    history_path = args.reports_dir / f"{args.output.stem}.metrics.jsonl"
    started_at = datetime.now(UTC).isoformat()
    records: list[dict] = []
    print(
        f"Training on {device}; {len(train_set)} train / {len(validation_set)} val samples; "
        f"pos_weight={pos_weight:.2f}; AMP={use_amp}"
    )
    for epoch in range(1, args.epochs + 1):
        model.train()
        losses: list[float] = []
        epoch_started = perf_counter()
        for batch in train_loader:
            before, after, target = (
                batch["before"].to(device, non_blocking=use_amp),
                batch["after"].to(device, non_blocking=use_amp),
                batch["mask"].to(device, non_blocking=use_amp),
            )
            optimizer.zero_grad(set_to_none=True)
            with autocast_context(device, use_amp):
                loss = combined_loss(model(before, after), target, bce)
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            scaler.step(optimizer)
            scaler.update()
            losses.append(float(loss.detach().cpu()))
        metrics = validation_metrics(
            model, validation_loader, device, threshold=args.threshold, use_mixed_precision=use_amp
        )
        scheduler.step(float(metrics["f1"]))
        record = {
            "epoch": epoch,
            "train_loss": float(np.mean(losses)),
            "learning_rate": float(optimizer.param_groups[0]["lr"]),
            "epoch_seconds": perf_counter() - epoch_started,
            **metrics,
        }
        records.append(record)
        with history_path.open("a", encoding="utf-8") as history:
            history.write(json.dumps(record) + "\n")
        print(
            "epoch {epoch:03d} | loss {train_loss:.4f} | F1 {f1:.4f} | IoU {iou:.4f} | "
            "P {precision:.4f} | R {recall:.4f}".format(**record)
        )
        if float(metrics["f1"]) > best_f1:
            best_f1, stalled_epochs = float(metrics["f1"]), 0
            torch.save(
                {
                    "model_state": model.state_dict(),
                    "config": {"base_channels": args.base_channels, "image_size": args.image_size},
                    "metrics": metrics,
                    "epoch": epoch,
                    "training": {
                        "dataset": str(args.data),
                        "seed": args.seed,
                        "loss": "0.55 weighted BCEWithLogits + 0.45 Dice",
                        "pos_weight": pos_weight,
                        "threshold": args.threshold,
                        "device": str(device),
                        "mixed_precision": use_amp,
                    },
                },
                args.output,
            )
        else:
            stalled_epochs += 1
            if stalled_epochs >= args.early_stopping_patience:
                print(f"Early stopping after {epoch} epoch(s): no validation F1 improvement.")
                break
    curve_path = args.reports_dir / f"{args.output.stem}.learning_curve.png"
    plotted = _plot_history(records, curve_path)
    summary = {
        "status": "COMPLETED",
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "checkpoint": str(args.output),
        "history": str(history_path),
        "learning_curve": str(curve_path) if plotted else None,
        "best_validation_f1": best_f1,
        "epochs_completed": len(records),
        "device": str(device),
        "mixed_precision": use_amp,
        "dataset": str(args.data),
        "config": vars(args),
    }
    summary["config"] = {key: str(value) if isinstance(value, Path) else value for key, value in summary["config"].items()}
    summary_path = args.reports_dir / f"{args.output.stem}.training_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Best validation F1: {best_f1:.4f}. Checkpoint: {args.output}")
    print(f"Actual run artifacts: {summary_path}")


if __name__ == "__main__":
    main()
