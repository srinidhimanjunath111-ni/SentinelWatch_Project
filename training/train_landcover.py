"""Train the India land-cover U-Net on held-out-tile splits. Never opens the test split.

    python -m training.train_landcover --version v1 --steps 6000 --run-dir data/processed/runs/landcover_v1
"""

from __future__ import annotations

import os

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.65")
os.environ.setdefault("PYTORCH_MPS_LOW_WATERMARK_RATIO", "0.5")

import argparse
import json
import math
import random
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import numpy as np
import torch

from app.ml.inference import LANDCOVER_CLASSES, select_device, sliding_window_logits
from app.ml.landcover_unet import LandCoverUNet
from app.ml.preprocessing import BAND_ORDER, normalise_scene
from data_tools.build_india_landcover import IGNORE, load_chips
from training import reporting
from training.dataset import LandCoverSampler, to_reflectance
from training.metrics import multiclass_metrics

RARE_CLASSES = ("built", "water", "wetland", "mine")


def band_statistics(chips: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Per-band mean and standard deviation over labelled training pixels."""

    total = np.zeros(len(BAND_ORDER))
    squared = np.zeros(len(BAND_ORDER))
    count = 0
    for chip in chips:
        valid = chip["label"] != IGNORE
        pixels = to_reflectance(chip["image"])[:, valid].astype(np.float64)
        total += pixels.sum(axis=1)
        squared += (pixels**2).sum(axis=1)
        count += pixels.shape[1]
    mean = total / count
    return mean.astype(np.float32), np.sqrt(squared / count - mean**2).astype(np.float32)


def class_weights(chips: list[dict], maximum: float) -> np.ndarray:
    """Inverse square-root frequency, scaled to mean 1 and capped so one class cannot dominate."""

    counts = np.sum([chip["class_pixels"] for chip in chips], axis=0).astype(np.float64)
    weights = np.sqrt(counts.sum() / np.maximum(counts, 1.0))
    weights = weights / weights.mean()
    return np.minimum(weights, maximum).astype(np.float32)


def chip_sampling_weights(chips: list[dict], boost: float) -> np.ndarray:
    """Chips with at least 2 percent of a rare class are drawn `boost` times as often."""

    rare = [LANDCOVER_CLASSES.index(name) for name in RARE_CLASSES]
    weights = np.ones(len(chips))
    for index, chip in enumerate(chips):
        counts = np.asarray(chip["class_pixels"], dtype=np.float64)
        if counts[rare].max() / max(counts.sum(), 1.0) >= 0.02:
            weights[index] = boost
    return weights


def validate(model, chips: list[dict], mean, std, device: str) -> dict:
    model.eval()
    count = len(LANDCOVER_CLASSES)
    confusion = np.zeros((count, count), dtype=np.int64)
    for chip in chips:
        valid = chip["label"] != IGNORE
        scene = normalise_scene(to_reflectance(chip["image"]), mean, std, valid)
        logits = sliding_window_logits(model, (scene,), count, device=device)
        prediction = logits.argmax(axis=0)
        cells = chip["label"][valid].astype(np.int64) * count + prediction[valid]
        confusion += np.bincount(cells, minlength=count * count).reshape(count, count)
    return multiclass_metrics(confusion, LANDCOVER_CLASSES)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", default="v1")
    parser.add_argument("--run-dir", type=Path, default=Path("data/processed/runs/landcover_v1"))
    parser.add_argument("--steps", type=int, default=6000)
    parser.add_argument("--val-every", type=int, default=300)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--crop", type=int, default=192)
    parser.add_argument("--base-channels", type=int, default=32)
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--max-class-weight", type=float, default=4.0)
    parser.add_argument("--rare-boost", type=float, default=2.0)
    parser.add_argument("--report", type=Path, default=Path("reports/ml/landcover_v1_training.json"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    device = args.device or select_device()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    train, val = load_chips("train"), load_chips("val")
    band_mean, band_std = band_statistics(train)
    weights = class_weights(train, args.max_class_weight)
    sampler = LandCoverSampler(
        [c["image"] for c in train],
        [c["label"] for c in train],
        band_mean,
        band_std,
        seed=args.seed,
        chip_weights=chip_sampling_weights(train, args.rare_boost),
        crop=args.crop,
    )
    model = LandCoverUNet(
        in_channels=len(BAND_ORDER),
        classes=len(LANDCOVER_CLASSES),
        base_channels=args.base_channels,
        dropout=args.dropout,
    ).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=args.weight_decay)
    warmup = max(1, int(0.03 * args.steps))

    def schedule(step: int) -> float:
        if step < warmup:
            return (step + 1) / warmup
        progress = (step - warmup) / max(1, args.steps - warmup)
        return 0.02 + 0.98 * 0.5 * (1 + math.cos(math.pi * progress))

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, schedule)
    criterion = torch.nn.CrossEntropyLoss(
        weight=torch.from_numpy(weights).to(device), ignore_index=IGNORE
    )
    start_step, best_f1, elapsed_before = 0, -1.0, 0.0
    history: list[dict] = []
    last_path, best_path = args.run_dir / "last.pt", args.run_dir / "best.pt"
    history_path = args.run_dir / "history.json"
    if args.resume and last_path.is_file():
        state = torch.load(last_path, map_location=device, weights_only=True)
        model.load_state_dict(state["model_state"])
        optimizer.load_state_dict(state["optimizer_state"])
        scheduler.load_state_dict(state["scheduler_state"])
        start_step, best_f1 = int(state["step"]), float(state["best_f1"])
        elapsed_before = float(state["elapsed"])
        history = json.loads(history_path.read_text(encoding="utf-8"))
    parameters = sum(p.numel() for p in model.parameters())
    print(
        f"device={device} params={parameters} train={len(train)} val={len(val)} "
        f"class_weights={np.round(weights, 2).tolist()}",
        flush=True,
    )
    started = perf_counter()
    window_loss: list[float] = []
    model.train()
    for step in range(start_step, args.steps):
        scenes, targets = sampler.batch(args.batch_size)
        logits = model(torch.from_numpy(scenes).to(device))
        loss = criterion(logits, torch.from_numpy(targets.astype(np.int64)).to(device))
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        scheduler.step()
        window_loss.append(float(loss.detach()))
        done = step + 1
        if done % args.val_every == 0 or done == args.steps:
            seconds = elapsed_before + perf_counter() - started
            metrics = validate(model, val, band_mean, band_std, device)
            per_class_f1 = {name: value["f1"] for name, value in metrics["per_class"].items()}
            record = {
                "step": done,
                "epoch": done * args.batch_size / len(train),
                "train_loss": float(np.mean(window_loss)),
                "seconds": seconds,
                "macro_f1": metrics["macro_f1"],
                "macro_iou": metrics["macro_iou"],
                "accuracy": metrics["accuracy"],
                "per_class_f1": per_class_f1,
            }
            history.append(record)
            history_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
            print(
                f"step {done} loss {record['train_loss']:.4f} val_macro_f1 {metrics['macro_f1']:.4f} "
                f"acc {metrics['accuracy']:.3f} "
                + " ".join(f"{n[:5]}={v:.2f}" for n, v in per_class_f1.items())
                + f" {seconds:.0f}s",
                flush=True,
            )
            window_loss = []
            model_state = {k: v.detach().cpu() for k, v in model.state_dict().items()}
            if metrics["macro_f1"] > best_f1:
                best_f1 = metrics["macro_f1"]
                torch.save(
                    {
                        "model_state": model_state,
                        "config": {
                            "in_channels": len(BAND_ORDER),
                            "base_channels": args.base_channels,
                            "dropout": args.dropout,
                        },
                        "classes": list(LANDCOVER_CLASSES),
                        "bands": list(BAND_ORDER),
                        "band_mean": band_mean.tolist(),
                        "band_std": band_std.tolist(),
                        "version": args.version,
                        "step": done,
                        "device": device,
                        "training_seconds": seconds,
                        "val_macro_f1": metrics["macro_f1"],
                        "trained_at": datetime.now(UTC).isoformat(),
                    },
                    best_path,
                )
            torch.save(
                {
                    "model_state": model_state,
                    "optimizer_state": optimizer.state_dict(),
                    "scheduler_state": scheduler.state_dict(),
                    "step": done,
                    "best_f1": best_f1,
                    "elapsed": seconds,
                },
                last_path,
            )
            if device == "mps":
                torch.mps.empty_cache()
            model.train()
    best = torch.load(best_path, map_location="cpu", weights_only=True)
    summary = {
        "status": "COMPLETED",
        "key": "landcover",
        "version": args.version,
        "seed": args.seed,
        "device": device,
        "steps": args.steps,
        "epochs": round(int(best["step"]) * args.batch_size / len(train)),
        "batch_size": args.batch_size,
        "crop": args.crop,
        "base_channels": args.base_channels,
        "dropout": args.dropout,
        "parameters": parameters,
        "class_weights": dict(zip(LANDCOVER_CLASSES, [float(w) for w in weights], strict=True)),
        "loss": "class-weighted cross-entropy with ignore index 255",
        "train_samples": len(train),
        "val_samples": len(val),
        "train_tiles": sorted({c["tile"] for c in train}),
        "val_tiles": sorted({c["tile"] for c in val}),
        "best_step": int(best["step"]),
        "best_val_macro_f1": float(best["val_macro_f1"]),
        "training_seconds": elapsed_before + perf_counter() - started,
        "completed_at": datetime.now(UTC).isoformat(),
        "history": history,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    reporting.learning_curve(
        history,
        "step",
        "training step",
        "macro_f1",
        "macro F1",
        f"Land cover {args.version}, India L2A chips, held-out tiles",
        Path(f"{args.report.with_suffix('')}_curve.png"),
    )
    print(f"Done. Best val macro F1 {best['val_macro_f1']:.4f} at step {best['step']}", flush=True)


if __name__ == "__main__":
    main()
