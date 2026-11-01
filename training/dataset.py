"""Random-crop samplers for the change and land-cover models.

Batches are built directly rather than through a DataLoader: crop size changes per batch, the
arrays are already in memory, and worker processes would only add RAM on an 8 GB machine.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.ml.preprocessing import apply_statistics, normalise_scene, pair_statistics
from data_tools.fetch_oscd import IGNORE, ChangeRecord

CROP_SIZES = (128, 160, 192, 224, 256)
REFLECTANCE_SCALE = 1.0 / 10000.0


def to_reflectance(stack: np.ndarray) -> np.ndarray:
    """OSCD and the India caches store reflectance x 10000 as uint16."""

    return stack.astype(np.float32) * REFLECTANCE_SCALE


def _geometric(arrays: list[np.ndarray], rng: np.random.Generator) -> list[np.ndarray]:
    """Apply one identical flip and 90-degree rotation to every array; dates stay co-registered."""

    if rng.random() < 0.5:
        arrays = [np.flip(a, axis=-1) for a in arrays]
    if rng.random() < 0.5:
        arrays = [np.flip(a, axis=-2) for a in arrays]
    turns = int(rng.integers(0, 4))
    if turns:
        arrays = [np.rot90(a, turns, axes=(-2, -1)) for a in arrays]
    return [a.copy() for a in arrays]


@dataclass
class ChangeAugment:
    """Training augmentation strengths for the change sampler."""

    positive_crop_probability: float = 0.75
    gain_jitter: float = 0.05
    offset_jitter: float = 0.005
    full_scene_stats_probability: float = 0.5
    cutout_probability: float = 0.15
    identity_probability: float = 0.05


class ChangeSampler:
    """Draws augmented batches from change records, optionally balancing named sources.

    Normalisation statistics come from the crop half the time and the whole record the other half.
    Live AOIs vary in size, so the model should not depend on one statistics footprint.
    """

    def __init__(
        self,
        records: list[ChangeRecord],
        *,
        seed: int,
        source_weights: dict[str, float] | None = None,
        augment: ChangeAugment | None = None,
    ):
        if not records:
            raise ValueError("ChangeSampler needs at least one record")
        self.records = records
        self.augment = augment or ChangeAugment()
        self.rng = np.random.default_rng(seed)
        # City sizes differ by a factor of 10; sqrt keeps big cities from dominating.
        weights = np.array([np.sqrt(r.mask.size) for r in records], dtype=np.float64)
        if source_weights:
            for source, share in source_weights.items():
                members = np.array([r.source == source for r in records])
                if members.any():
                    weights[members] *= share / weights[members].sum()
        self.weights = weights / weights.sum()
        # Index positives once; re-scanning a mask per crop would dominate the sampling time.
        self.positives = [np.argwhere(r.mask == 1) for r in records]
        self.record_stats = [
            pair_statistics(
                to_reflectance(r.before), to_reflectance(r.after), r.mask != IGNORE
            )
            for r in records
        ]

    def _crop_origin(self, index: int, size: int) -> tuple[int, int, int, int]:
        record = self.records[index]
        height, width = record.mask.shape
        crop_h, crop_w = min(size, height), min(size, width)
        positives = self.positives[index]
        if len(positives) and self.rng.random() < self.augment.positive_crop_probability:
            row, col = positives[self.rng.integers(len(positives))]
            top = int(row) - int(self.rng.integers(0, crop_h))
            left = int(col) - int(self.rng.integers(0, crop_w))
            top = int(np.clip(top, 0, height - crop_h))
            left = int(np.clip(left, 0, width - crop_w))
        else:
            top = int(self.rng.integers(0, height - crop_h + 1))
            left = int(self.rng.integers(0, width - crop_w + 1))
        return top, left, crop_h, crop_w

    def _radiometric(self, stack: np.ndarray) -> np.ndarray:
        """Mild per-band gain and offset, independent per date, like a small atmospheric shift."""

        rng, aug = self.rng, self.augment
        gain = rng.uniform(1 - aug.gain_jitter, 1 + aug.gain_jitter, (stack.shape[0], 1, 1))
        offset = rng.uniform(-aug.offset_jitter, aug.offset_jitter, (stack.shape[0], 1, 1))
        return stack * gain.astype(np.float32) + offset.astype(np.float32)

    def _sample(self, size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        rng, aug = self.rng, self.augment
        index = int(rng.choice(len(self.records), p=self.weights))
        record = self.records[index]
        top, left, crop_h, crop_w = self._crop_origin(index, size)
        slices = (slice(top, top + crop_h), slice(left, left + crop_w))
        before = to_reflectance(record.before[:, slices[0], slices[1]])
        after = to_reflectance(record.after[:, slices[0], slices[1]])
        mask = record.mask[slices].astype(np.uint8)
        if crop_h != size or crop_w != size:
            pad = ((0, size - crop_h), (0, size - crop_w))
            before = np.pad(before, ((0, 0), *pad), mode="reflect")
            after = np.pad(after, ((0, 0), *pad), mode="reflect")
            mask = np.pad(mask, pad, mode="reflect")
        before, after, mask = _geometric([before, after, mask], rng)
        if rng.random() < 0.5:
            before, after = after, before
        if rng.random() < aug.identity_probability:
            # A date compared with itself contains no change, whatever the radiometry says.
            after = before.copy()
            mask = np.where(mask == IGNORE, IGNORE, 0).astype(np.uint8)
        before = self._radiometric(before)
        after = self._radiometric(after)
        valid = mask != IGNORE
        if rng.random() < aug.cutout_probability:
            # Teaches the model that blanked pixels (cloud at inference) carry no change evidence.
            cut_h, cut_w = rng.integers(size // 8, size // 2, 2)
            cut_top = int(rng.integers(0, size - cut_h))
            cut_left = int(rng.integers(0, size - cut_w))
            valid[cut_top : cut_top + cut_h, cut_left : cut_left + cut_w] = False
            mask[cut_top : cut_top + cut_h, cut_left : cut_left + cut_w] = IGNORE
        if rng.random() < aug.full_scene_stats_probability:
            centre, scale = self.record_stats[index]
        else:
            centre, scale = pair_statistics(before, after, valid)
        return (
            apply_statistics(before, centre, scale, valid),
            apply_statistics(after, centre, scale, valid),
            mask,
        )

    def batch(self, batch_size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        size = int(self.rng.choice(CROP_SIZES))
        # Hold memory per step near constant: larger crops get a smaller batch. A full batch of 8
        # at 256 px exhausts the MPS allowance on the 8 GB machine (measured: out-of-memory at 3.47 GB).
        count = max(4, min(batch_size, int(batch_size * (192 / size) ** 2)))
        samples = [self._sample(size) for _ in range(count)]
        return (
            np.stack([s[0] for s in samples]),
            np.stack([s[1] for s in samples]),
            np.stack([s[2] for s in samples]),
        )


class LandCoverSampler:
    """Draws augmented land-cover batches. Chips are stored uint16 [6,H,W] with uint8 labels."""

    def __init__(
        self,
        scenes: list[np.ndarray],
        labels: list[np.ndarray],
        band_mean: np.ndarray,
        band_std: np.ndarray,
        *,
        seed: int,
        chip_weights: np.ndarray | None = None,
        crop: int = 192,
        gain_jitter: float = 0.05,
    ):
        self.scenes = scenes
        self.labels = labels
        self.mean = band_mean
        self.std = band_std
        self.rng = np.random.default_rng(seed)
        self.crop = crop
        self.gain_jitter = gain_jitter
        weights = np.ones(len(scenes)) if chip_weights is None else np.asarray(chip_weights, float)
        self.weights = weights / weights.sum()

    def _sample(self) -> tuple[np.ndarray, np.ndarray]:
        rng = self.rng
        index = int(rng.choice(len(self.scenes), p=self.weights))
        scene, label = self.scenes[index], self.labels[index]
        height, width = label.shape
        top = int(rng.integers(0, height - self.crop + 1))
        left = int(rng.integers(0, width - self.crop + 1))
        window = to_reflectance(scene[:, top : top + self.crop, left : left + self.crop])
        target = label[top : top + self.crop, left : left + self.crop]
        window, target = _geometric([window, target], rng)
        gain = rng.uniform(1 - self.gain_jitter, 1 + self.gain_jitter, (window.shape[0], 1, 1))
        window = window * gain.astype(np.float32)
        return normalise_scene(window, self.mean, self.std, target != IGNORE), target

    def batch(self, batch_size: int) -> tuple[np.ndarray, np.ndarray]:
        samples = [self._sample() for _ in range(batch_size)]
        return np.stack([s[0] for s in samples]), np.stack([s[1] for s in samples])
