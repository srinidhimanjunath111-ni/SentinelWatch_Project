"""Train the Siamese U-Net change model on real Sentinel-2 change datasets.

    python -m training.train --datasets oscd --version v1 --steps 6000 --run-dir data/processed/runs/change_v1

Selection and threshold tuning use validation data only. This script never opens a test split;
test numbers come from training/evaluate.py. The best validation checkpoint and a resumable `last`
checkpoint are written as training goes, so a crash loses at most one validation interval.
"""

from __future__ import annotations

import os

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
# Measured on the 8 GB M1: without a cap the MPS allocator grows to its 5.7 GB recommended limit.
# 0.65 of that keeps the process footprint under about 4 GB, with no loss of speed.
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

from app.ml.inference import select_device, sliding_window_logits
from app.ml.preprocessing import (
    BAND_ORDER,
    NORMALISE_PER_DATE,
    NORMALISE_POOLED,
    normalise_pair,
)
from app.ml.siamese_unet import SiameseUNet
from data_tools.fetch_oscd import IGNORE, ChangeRecord, load_records
from training.dataset import ChangeAugment, ChangeSampler, to_reflectance
from training.metrics import ScoreHistogram, average_precision, best_shared_threshold


def segmentation_loss(
    logits: torch.Tensor,
    target: torch.Tensor,
    pos_weight: float,
    dice_weight: float = 0.5,
) -> tuple[torch.Tensor, dict[str, float]]:
    """Weighted BCE plus soft Dice over the batch; pixels marked IGNORE contribute nothing.

    Dice is pooled over the whole batch rather than averaged per sample, because a crop with no
    change has an undefined Dice and would otherwise push every prediction towards zero.
    """

    logits = logits.squeeze(1)
    valid = target != IGNORE
    truth = (target == 1).float()
    weights = valid.float() * (1.0 + (pos_weight - 1.0) * truth)
    per_pixel = torch.nn.functional.binary_cross_entropy_with_logits(
        logits, truth, weight=weights, reduction="sum"
    )
    bce = per_pixel / weights.sum().clamp(min=1.0)
    probability = torch.sigmoid(logits)
    intersection = (probability * truth * valid).sum()
    denominator = (probability * valid).sum() + (truth * valid).sum()
    dice = 1.0 - (2.0 * intersection + 1.0) / (denominator + 1.0)
    return bce + dice_weight * dice, {"bce": float(bce.detach()), "dice": float(dice.detach())}


def predict_record(
    model: torch.nn.Module,
    record: ChangeRecord,
    device: str,
    normalisation: str,
    *,
    batch_size: int = 4,
) -> tuple[np.ndarray, np.ndarray]:
    """Whole-record probability, normalised as live inference is. Returns (probability, scored).

    Statistics use every pixel seen clear, labelled or not, because live inference cannot know
    which pixels a label would have covered. Only labelled clear pixels are scored.
    """

    seen = record.pixel_valid()
    before, after = normalise_pair(
        to_reflectance(record.before), to_reflectance(record.after), seen, normalisation
    )
    logits = sliding_window_logits(model, (before, after), 1, device=device, batch_size=batch_size)[0]
    probability = torch.sigmoid(torch.from_numpy(logits)).numpy()
    return probability, seen & (record.mask != IGNORE)


def validate(
    model: torch.nn.Module,
    records: list[ChangeRecord],
    device: str,
    normalisation: str,
    *,
    batch_size: int = 4,
) -> dict:
    """Mean per-source F1 at one shared threshold, with threshold-free AP per source."""

    model.eval()
    per_source: dict[str, ScoreHistogram] = {}
    for record in records:
        probability, scored = predict_record(
            model, record, device, normalisation, batch_size=batch_size
        )
        per_source.setdefault(record.source, ScoreHistogram()).update(
            probability, record.mask == 1, scored
        )
    threshold, mean_f1 = best_shared_threshold(per_source)
    result = {"threshold": threshold, "f1": mean_f1, "per_source": {}}
    for source, histogram in per_source.items():
        best_threshold, best = histogram.best_threshold()
        result["per_source"][source] = {
            "at_shared_threshold": histogram.metrics_at(threshold),
            "best_threshold": best_threshold,
            "best_f1": best["f1"],
            "average_precision": average_precision(histogram),
        }
    result["average_precision"] = float(
        np.mean([v["average_precision"] for v in result["per_source"].values()])
    )
    return result


def load_change_records(datasets: list[str], split: str) -> list[ChangeRecord]:
    records: list[ChangeRecord] = []
    for dataset in datasets:
        if dataset == "oscd":
            records.extend(load_records(split))
        elif dataset == "india_forest":
            from data_tools.build_india_forest_change import load_records as load_forest

            records.extend(load_forest(split))
        else:
            raise ValueError(f"Unknown dataset {dataset!r}")
    return records


def positive_fraction(records: list[ChangeRecord]) -> float:
    positive = sum(int((r.mask == 1).sum()) for r in records)
    total = sum(int((r.mask != IGNORE).sum()) for r in records)
    return positive / max(total, 1)


def _last_step(run_dir: Path) -> int:
    history_path = run_dir / "history.json"
    if not history_path.is_file():
        return 0
    return int(json.loads(history_path.read_text(encoding="utf-8"))[-1]["step"])


def _count_by_source(records: list[ChangeRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.source] = counts.get(record.source, 0) + 1
    return counts


def build_payload(model, args, step, threshold, val_metrics, device, seconds) -> dict:
    return {
        "model_state": {k: v.detach().cpu() for k, v in model.state_dict().items()},
        "config": {
            "in_channels": len(BAND_ORDER),
            "base_channels": args.base_channels,
            "dropout": args.dropout,
            "norm": args.norm,
        },
        "version": args.version,
        "threshold": float(threshold),
        "bands": list(BAND_ORDER),
        "normalisation": args.normalisation_id,
        "datasets": list(args.datasets),
        "step": int(step),
        "device": device,
        "training_seconds": float(seconds),
        "val_f1": float(val_metrics["f1"]),
        "trained_at": datetime.now(UTC).isoformat(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train the SentinelWatch change model")
    parser.add_argument("--datasets", type=lambda s: s.split(","), default=["oscd"])
    parser.add_argument("--version", default="v1")
    parser.add_argument("--run-dir", type=Path, default=Path("data/processed/runs/change_v1"))
    parser.add_argument("--steps", type=int, default=6000)
    parser.add_argument("--val-every", type=int, default=250)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--base-channels", type=int, default=32)
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--dice-weight", type=float, default=0.5)
    parser.add_argument("--max-pos-weight", type=float, default=10.0)
    parser.add_argument("--norm", choices=("batch", "group"), default="batch")
    parser.add_argument("--normalisation", choices=("pooled", "per_date"), default="pooled")
    parser.add_argument("--ema-decay", type=float, default=0.0, help="0 disables weight averaging")
    parser.add_argument("--india-share", type=float, default=0.5, help="Sampling share of India forest")
    parser.add_argument("--init-from", type=Path, default=None, help="Warm-start weights")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.normalisation_id = NORMALISE_PER_DATE if args.normalisation == "per_date" else NORMALISE_POOLED
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    device = args.device or select_device()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    train_records = load_change_records(args.datasets, "train")
    val_records = load_change_records(args.datasets, "val")
    pos_fraction = positive_fraction(train_records)
    pos_weight = min(args.max_pos_weight, (1 - pos_fraction) / max(pos_fraction, 1e-6))
    source_weights = (
        {"oscd": 1.0 - args.india_share, "india_forest": args.india_share}
        if "india_forest" in args.datasets and "oscd" in args.datasets
        else None
    )
    # Sampler state is not checkpointed; a resumed run takes a different random stream on purpose.
    resume_seed = args.seed + (1 + _last_step(args.run_dir) if args.resume else 0)
    sampler = ChangeSampler(
        train_records,
        seed=resume_seed,
        source_weights=source_weights,
        augment=ChangeAugment(),
        normalisation=args.normalisation_id,
    )
    model = SiameseUNet(
        in_channels=len(BAND_ORDER),
        base_channels=args.base_channels,
        dropout=args.dropout,
        norm=args.norm,
    ).to(device)
    ema_model = None
    if args.ema_decay > 0:
        ema_model = SiameseUNet(
            in_channels=len(BAND_ORDER),
            base_channels=args.base_channels,
            dropout=args.dropout,
            norm=args.norm,
        ).to(device)
        ema_model.load_state_dict(model.state_dict())
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=args.learning_rate, weight_decay=args.weight_decay
    )
    warmup = max(1, int(0.03 * args.steps))

    def schedule(step: int) -> float:
        if step < warmup:
            return (step + 1) / warmup
        progress = (step - warmup) / max(1, args.steps - warmup)
        return 0.02 + 0.98 * 0.5 * (1 + math.cos(math.pi * progress))

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, schedule)
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
        print(f"Resumed at step {start_step}, best val F1 {best_f1:.4f}", flush=True)
    elif args.init_from is not None:
        state = torch.load(args.init_from, map_location=device, weights_only=True)
        model.load_state_dict(state["model_state"])
        print(f"Warm-started from {args.init_from}", flush=True)
    parameters = sum(p.numel() for p in model.parameters())
    print(
        f"device={device} params={parameters} train={len(train_records)} val={len(val_records)} "
        f"pos_fraction={pos_fraction:.4f} pos_weight={pos_weight:.2f}",
        flush=True,
    )
    started = perf_counter()
    window_loss: list[float] = []
    model.train()
    for step in range(start_step, args.steps):
        before, after, target = sampler.batch(args.batch_size)
        optimizer.zero_grad(set_to_none=True)
        try:
            logits = model(
                torch.from_numpy(before).to(device), torch.from_numpy(after).to(device)
            )
            loss, _ = segmentation_loss(
                logits, torch.from_numpy(target).to(device), pos_weight, args.dice_weight
            )
            loss.backward()
        except RuntimeError as error:
            # Other agents share this machine, so MPS headroom varies. Dropping one batch is
            # better than losing the run; repeated failures still surface in the log.
            if "out of memory" not in str(error) or device != "mps":
                raise
            print(f"step {step + 1}: MPS out of memory, batch skipped", flush=True)
            optimizer.zero_grad(set_to_none=True)
            del error
            torch.mps.empty_cache()
            scheduler.step()
            continue
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        scheduler.step()
        window_loss.append(float(loss.detach()))
        done = step + 1
        if ema_model is not None:
            with torch.no_grad():
                # Warm up the average so the first random weights fade quickly.
                decay = min(args.ema_decay, (1 + done) / (10 + done))
                source = model.state_dict()
                for name, value in ema_model.state_dict().items():
                    if value.dtype.is_floating_point:
                        value.mul_(decay).add_(source[name].detach(), alpha=1 - decay)
                    else:
                        value.copy_(source[name])
        if device == "mps" and done % 25 == 0:
            # The MPS allocator holds freed buffers until told otherwise, which fragments memory.
            torch.mps.empty_cache()
        if done % args.val_every == 0 or done == args.steps:
            seconds = elapsed_before + perf_counter() - started
            scored_model = ema_model if ema_model is not None else model
            metrics = validate(scored_model, val_records, device, args.normalisation_id)
            record = {
                "step": done,
                "train_loss": float(np.mean(window_loss)),
                "seconds": seconds,
                "lr": float(optimizer.param_groups[0]["lr"]),
                "f1": metrics["f1"],
                "threshold": metrics["threshold"],
                "average_precision": metrics["average_precision"],
                "per_source": metrics["per_source"],
            }
            history.append(record)
            history_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
            per_source = " ".join(
                f"{s}: f1 {v['at_shared_threshold']['f1']:.3f} ap {v['average_precision']:.3f} "
                f"p {v['at_shared_threshold']['precision']:.2f} r {v['at_shared_threshold']['recall']:.2f}"
                for s, v in metrics["per_source"].items()
            )
            print(
                f"step {done} loss {record['train_loss']:.4f} val_f1 {metrics['f1']:.4f} "
                f"t {metrics['threshold']:.3f} [{per_source}] {seconds:.0f}s",
                flush=True,
            )
            window_loss = []
            if metrics["f1"] > best_f1:
                best_f1 = metrics["f1"]
                payload = build_payload(
                    scored_model, args, done, metrics["threshold"], metrics, device, seconds
                )
                torch.save(payload, best_path)
            torch.save(
                {
                    "model_state": {k: v.detach().cpu() for k, v in model.state_dict().items()},
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
    final_seconds = elapsed_before + perf_counter() - started
    best = torch.load(best_path, map_location="cpu", weights_only=True)
    summary = {
        "key": "change",
        "version": args.version,
        "datasets": list(args.datasets),
        "seed": args.seed,
        "device": device,
        "steps": args.steps,
        "batch_size": args.batch_size,
        "crop_sizes": [128, 160, 192, 224, 256],
        "base_channels": args.base_channels,
        "dropout": args.dropout,
        "parameters": parameters,
        "pos_fraction": pos_fraction,
        "pos_weight": pos_weight,
        "loss": f"weighted BCE (pos_weight {pos_weight:.2f}) + {args.dice_weight} soft Dice, ignore mask",
        "normalisation": args.normalisation_id,
        "norm": args.norm,
        "ema_decay": args.ema_decay,
        "learning_rate": args.learning_rate,
        "train_samples": _count_by_source(train_records),
        "val_samples": _count_by_source(val_records),
        "train_names": [r.name for r in train_records],
        "val_names": [r.name for r in val_records],
        "best_step": int(best["step"]),
        "best_val_f1": float(best["val_f1"]),
        "threshold": float(best["threshold"]),
        "training_seconds": final_seconds,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    (args.run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"Done. Best val F1 {best['val_f1']:.4f} at step {best['step']}, threshold {best['threshold']:.3f}",
        flush=True,
    )


if __name__ == "__main__":
    main()
