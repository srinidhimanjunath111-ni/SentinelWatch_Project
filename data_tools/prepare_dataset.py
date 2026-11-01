"""Tile a LEVIR-style source dataset into model-friendly fixed-size image patches."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare paired change-detection images")
    parser.add_argument(
        "--source", type=Path, required=True, help="Source root containing train/val/test/A,B,label"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tile-size", type=int, default=256)
    parser.add_argument("--stride", type=int, default=256)
    parser.add_argument("--min-positive-fraction", type=float, default=0.002)
    parser.add_argument("--keep-negative-probability", type=float, default=0.22)
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def _prepare_split(args: argparse.Namespace, split: str, rng: np.random.Generator) -> int:
    source = args.source / split
    target = args.output / split
    for folder in ("A", "B", "label"):
        (target / folder).mkdir(parents=True, exist_ok=True)
    count = 0
    for before_path in sorted((source / "A").iterdir()):
        if before_path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        after_path, label_path = (
            source / "B" / before_path.name,
            source / "label" / before_path.name,
        )
        if not after_path.exists() or not label_path.exists():
            continue
        with (
            Image.open(before_path) as source_before,
            Image.open(after_path) as source_after,
            Image.open(label_path) as source_label,
        ):
            before = source_before.convert("RGB")
            after = source_after.convert("RGB").resize(before.size, Image.Resampling.BILINEAR)
            label = source_label.convert("L").resize(before.size, Image.Resampling.NEAREST)
            label_np = np.asarray(label)
            for top in range(0, before.height - args.tile_size + 1, args.stride):
                for left in range(0, before.width - args.tile_size + 1, args.stride):
                    crop_mask = label_np[top : top + args.tile_size, left : left + args.tile_size]
                    changed_fraction = float((crop_mask > 0).mean())
                    if (
                        changed_fraction < args.min_positive_fraction
                        and rng.random() > args.keep_negative_probability
                    ):
                        continue
                    name = f"{before_path.stem}_{top:04d}_{left:04d}.png"
                    box = (left, top, left + args.tile_size, top + args.tile_size)
                    before.crop(box).save(target / "A" / name)
                    after.crop(box).save(target / "B" / name)
                    label.crop(box).save(target / "label" / name)
                    count += 1
    return count


def main() -> None:
    args = parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError(
            f"Output {args.output} is not empty; choose a new folder to protect prior data."
        )
    rng = np.random.default_rng(args.seed)
    required_splits = [
        split for split in ("train", "val", "test") if (args.source / split).is_dir()
    ]
    if not required_splits:
        raise FileNotFoundError("Expected train, val, and/or test folders in the source dataset.")
    for split in required_splits:
        count = _prepare_split(args, split, rng)
        print(f"{split}: {count} tiles")
    print(f"Prepared dataset: {args.output}")


if __name__ == "__main__":
    main()
