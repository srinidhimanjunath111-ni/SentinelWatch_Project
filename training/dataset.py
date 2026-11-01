"""Paired image/mask dataset compatible with LEVIR-style directory layouts."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

try:
    import torch
    from torch.utils.data import Dataset
except ImportError as error:  # pragma: no cover
    raise ImportError("Install requirements-ml.txt for model training.") from error


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


class ChangePairDataset(Dataset):
    """Reads `<root>/<split>/{A,B,label}/same_filename` folders.

    `A` is the before image and `B` is the after image. Mask values above zero
    represent change. The paired geometric augmentation is important: applying a
    different flip/crop to dates would corrupt the learning target.
    """

    def __init__(self, root: str | Path, split: str, image_size: int = 256, augment: bool = False):
        self.root = Path(root) / split
        self.image_size = image_size
        self.augment = augment
        self.before_dir = self.root / "A"
        self.after_dir = self.root / "B"
        self.label_dir = self.root / "label"
        if not all(path.is_dir() for path in (self.before_dir, self.after_dir, self.label_dir)):
            raise FileNotFoundError(
                f"Expected {self.root}/A, {self.root}/B, and {self.root}/label directories."
            )
        self.samples = []
        for before in sorted(self.before_dir.iterdir()):
            if before.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            after, label = self.after_dir / before.name, self.label_dir / before.name
            if after.exists() and label.exists():
                self.samples.append((before, after, label))
        if not self.samples:
            raise ValueError(f"No matching image pairs found in {self.root}")

    def __len__(self) -> int:
        return len(self.samples)

    @staticmethod
    def _read_image(path: Path, mode: str) -> Image.Image:
        with Image.open(path) as image:
            return image.convert(mode).copy()

    def __getitem__(self, index: int) -> dict[str, torch.Tensor | str]:
        before_path, after_path, label_path = self.samples[index]
        before = self._read_image(before_path, "RGB").resize(
            (self.image_size, self.image_size), Image.Resampling.BILINEAR
        )
        after = self._read_image(after_path, "RGB").resize(
            (self.image_size, self.image_size), Image.Resampling.BILINEAR
        )
        label = self._read_image(label_path, "L").resize(
            (self.image_size, self.image_size), Image.Resampling.NEAREST
        )
        if self.augment:
            # torch RNG means workers still produce different deterministic samples
            # under the train script's seeded DataLoader.
            if torch.rand(()) > 0.5:
                before = before.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                after = after.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                label = label.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if torch.rand(()) > 0.5:
                before = before.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                after = after.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                label = label.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        before_tensor = torch.from_numpy(np.asarray(before).copy()).permute(2, 0, 1).float() / 255.0
        after_tensor = torch.from_numpy(np.asarray(after).copy()).permute(2, 0, 1).float() / 255.0
        label_tensor = torch.from_numpy(np.asarray(label).copy()).unsqueeze(0).float() / 255.0
        return {
            "before": before_tensor,
            "after": after_tensor,
            "mask": (label_tensor > 0.5).float(),
            "id": before_path.name,
        }
