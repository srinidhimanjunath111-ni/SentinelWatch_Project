"""Radiometric preprocessing shared by training, evaluation and live inference.

Change model: per-pair, per-band robust standardisation
-------------------------------------------------------
OSCD is Sentinel-2 L1C (top-of-atmosphere) while live imagery is L2A (surface reflectance).
Between the two levels, each band differs mostly by an additive path-radiance term and a
multiplicative gain, and both vary by band (blue most of all). Subtracting a per-band centre and
dividing by a per-band spread removes exactly that first-order difference, so a model trained on
L1C sees inputs on the same scale as L2A.

Statistics are pooled over *both* dates, not computed per date. Per-date standardisation would
erase a genuine scene-wide change (a flood filling the AOI, a whole-window clearance) because each
date would be re-centred on itself. Pooling keeps the before/after contrast that is the signal.
Median and IQR are used rather than mean and standard deviation so that residual cloud, glint and
the changed pixels themselves barely move the statistics.

`SCALE_FLOOR` stops a uniform AOI (open water, say) from amplifying sensor noise into apparent
change. It is expressed in reflectance units, so it behaves the same across levels.

Land-cover model: fixed per-band standardisation
------------------------------------------------
That model trains and runs on L2A alone and needs absolute reflectance to separate classes, so
it uses global constants computed from its training set and stored in its checkpoint.
"""

from __future__ import annotations

import numpy as np

BAND_ORDER = ("B02", "B03", "B04", "B08", "B11", "B12")
SCALE_FLOOR = 0.01
CLIP = 6.0
_MAX_STAT_SAMPLES = 200_000
NORMALISATION_ID = "pair_band_median_iqr_v1"


def finite_valid(valid: np.ndarray, *stacks: np.ndarray) -> np.ndarray:
    """Combine a caller's valid mask with a check that every band is finite on every date."""

    mask = np.asarray(valid, dtype=bool)
    for stack in stacks:
        mask = mask & np.isfinite(stack).all(axis=0)
    return mask


def pair_statistics(
    before: np.ndarray, after: np.ndarray, valid: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Return per-band (centre, scale), each shaped [C,1,1], pooled over both dates."""

    channels = before.shape[0]
    centre = np.zeros((channels, 1, 1), dtype=np.float32)
    scale = np.full((channels, 1, 1), 1.0, dtype=np.float32)
    count = int(valid.sum())
    if count == 0:
        return centre, scale
    step = max(1, count // _MAX_STAT_SAMPLES)
    for band in range(channels):
        samples = np.concatenate((before[band][valid][::step], after[band][valid][::step]))
        low, mid, high = np.percentile(samples, (25, 50, 75))
        centre[band] = mid
        # For a normal distribution IQR = 1.349 sigma, so this estimates a standard deviation.
        scale[band] = max((high - low) / 1.349, SCALE_FLOOR)
    return centre, scale


def apply_statistics(
    stack: np.ndarray, centre: np.ndarray, scale: np.ndarray, valid: np.ndarray
) -> np.ndarray:
    """Standardise one date. Invalid pixels become 0, the pair's central value."""

    output = np.clip((stack - centre) / scale, -CLIP, CLIP).astype(np.float32)
    output[:, ~valid] = 0.0
    return output


def normalise_pair(
    before: np.ndarray, after: np.ndarray, valid: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Per-pair per-band standardisation for reflectance [C,H,W] inputs."""

    centre, scale = pair_statistics(before, after, valid)
    return (
        apply_statistics(before, centre, scale, valid),
        apply_statistics(after, centre, scale, valid),
    )


def normalise_scene(
    scene: np.ndarray, mean: np.ndarray, std: np.ndarray, valid: np.ndarray
) -> np.ndarray:
    """Fixed per-band standardisation for the land-cover model."""

    mean = np.asarray(mean, dtype=np.float32).reshape(-1, 1, 1)
    std = np.asarray(std, dtype=np.float32).reshape(-1, 1, 1)
    output = np.clip((scene - mean) / std, -CLIP, CLIP).astype(np.float32)
    output[:, ~valid] = 0.0
    return output
