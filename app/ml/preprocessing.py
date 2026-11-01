"""Radiometric preprocessing shared by training, evaluation and live inference.

Change model: per-pair, per-band robust standardisation
-------------------------------------------------------
OSCD is Sentinel-2 L1C (top-of-atmosphere) while live imagery is L2A (surface reflectance).
Between the two levels, each band differs mostly by an additive path-radiance term and a
multiplicative gain, and both vary by band (blue most of all). Subtracting a per-band centre and
dividing by a per-band spread removes exactly that first-order difference, so a model trained on
L1C sees inputs on the same scale as L2A.

Two modes exist, and each change checkpoint records which one it was trained with.

`NORMALISE_POOLED` (v1) pools statistics over both dates, keeping any scene-wide difference.
`NORMALISE_PER_DATE` (v2) standardises each date on its own statistics, measured over the pixels
valid in both dates. Median and IQR are used, so a change covering a minority of the AOI barely moves
them and stays visible after normalisation. What it removes is the date-to-date atmospheric and
illumination shift, which on OSCD is as large as the change signal in many bands. Measured with no
model involved, the pixel AUC of the distance between normalised dates rose from 0.802 to 0.845 on
the 14 OSCD train cities (better in 13 of them) and from 0.852 to 0.874 on India forest validation.
See reports/ml/change_v1_investigation.json. The cost is real: a change covering most of an AOI, such
as a flood filling the window, moves the median with it and can be suppressed. Use a wider AOI there.

Median and IQR also stop residual cloud, glint and the changed pixels themselves from dominating.

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
# Pooled: one centre and scale per band over both dates (change model v1).
NORMALISE_POOLED = "pair_band_median_iqr_v1"
# Per date: each date gets its own centre and scale, from the pixels valid in both dates.
NORMALISE_PER_DATE = "date_band_median_iqr_v1"
NORMALISATION_MODES = (NORMALISE_POOLED, NORMALISE_PER_DATE)
ALIGN_REACH = 6
ALIGN_CROP = 384
ALIGN_MIN_GAIN = 0.01


def finite_valid(valid: np.ndarray, *stacks: np.ndarray) -> np.ndarray:
    """Combine a caller's valid mask with a check that every band is finite on every date."""

    mask = np.asarray(valid, dtype=bool)
    for stack in stacks:
        mask = mask & np.isfinite(stack).all(axis=0)
    return mask


def _robust_statistics(stacks: tuple[np.ndarray, ...], valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-band median and IQR-derived scale, each [C,1,1], over the valid pixels of `stacks`."""

    channels = stacks[0].shape[0]
    centre = np.zeros((channels, 1, 1), dtype=np.float32)
    scale = np.full((channels, 1, 1), 1.0, dtype=np.float32)
    count = int(valid.sum())
    if count == 0:
        return centre, scale
    step = max(1, count // _MAX_STAT_SAMPLES)
    for band in range(channels):
        samples = np.concatenate([stack[band][valid][::step] for stack in stacks])
        low, mid, high = np.percentile(samples, (25, 50, 75))
        centre[band] = mid
        # For a normal distribution IQR = 1.349 sigma, so this estimates a standard deviation.
        scale[band] = max((high - low) / 1.349, SCALE_FLOOR)
    return centre, scale


def pair_statistics(
    before: np.ndarray, after: np.ndarray, valid: np.ndarray, mode: str = NORMALISE_POOLED
) -> tuple[tuple[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]:
    """Return ((before centre, before scale), (after centre, after scale)), each [C,1,1]."""

    if mode == NORMALISE_POOLED:
        shared = _robust_statistics((before, after), valid)
        return shared, shared
    if mode == NORMALISE_PER_DATE:
        return _robust_statistics((before,), valid), _robust_statistics((after,), valid)
    raise ValueError(f"Unknown normalisation {mode!r}")


def apply_statistics(
    stack: np.ndarray, centre: np.ndarray, scale: np.ndarray, valid: np.ndarray
) -> np.ndarray:
    """Standardise one date. Invalid pixels become 0, the pair's central value."""

    output = np.clip((stack - centre) / scale, -CLIP, CLIP).astype(np.float32)
    output[:, ~valid] = 0.0
    return output


def normalise_pair(
    before: np.ndarray, after: np.ndarray, valid: np.ndarray, mode: str = NORMALISE_POOLED
) -> tuple[np.ndarray, np.ndarray]:
    """Per-pair per-band standardisation for reflectance [C,H,W] inputs."""

    stats_before, stats_after = pair_statistics(before, after, valid, mode)
    return (
        apply_statistics(before, *stats_before, valid),
        apply_statistics(after, *stats_after, valid),
    )


def estimate_date_shift(
    before: np.ndarray, after: np.ndarray, valid: np.ndarray
) -> tuple[int, int]:
    """Integer (row, column) shift of `after` that best matches `before`, from imagery alone.

    Measured on OSCD train and validation cities, dates sit up to 3 px apart (saclay_e), and the
    change maps follow the before grid. Uncorrected, every edge in the scene reads as change. Bands
    B04 and B08 are used because they are native 10 m. A shift is only taken when correlation
    clearly improves, so a well-registered pair (live L2A, typically) is left untouched.
    """

    height, width = valid.shape
    if min(height, width) < 2 * ALIGN_REACH + 16 or valid.mean() < 0.5:
        return 0, 0
    # A centred crop keeps cost independent of AOI size, with margin to hold any tested shift.
    side = min(ALIGN_CROP, height - 2 * ALIGN_REACH, width - 2 * ALIGN_REACH)
    top, left = (height - side) // 2, (width - side) // 2
    scores: dict[tuple[int, int], float] = {}
    mask = valid[top : top + side, left : left + side]
    first = before[[2, 3]][:, top : top + side, left : left + side]
    for dy in range(-ALIGN_REACH, ALIGN_REACH + 1):
        for dx in range(-ALIGN_REACH, ALIGN_REACH + 1):
            second = after[[2, 3]][:, top - dy : top - dy + side, left - dx : left - dx + side]
            shifted_mask = mask & valid[top - dy : top - dy + side, left - dx : left - dx + side]
            total = 0.0
            for band in range(2):
                x = first[band][shifted_mask]
                y = second[band][shifted_mask]
                x, y = x - x.mean(), y - y.mean()
                total += float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-9))
            scores[(dy, dx)] = total / 2
    best = max(scores, key=scores.get)
    if scores[best] - scores[(0, 0)] < ALIGN_MIN_GAIN:
        return 0, 0
    return best


def apply_date_shift(
    after: np.ndarray, valid: np.ndarray, shift: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """Move `after` by shift. Pixels with no source after the move become invalid."""

    if shift == (0, 0):
        return after, valid
    dy, dx = shift
    moved = np.roll(after, (dy, dx), axis=(-2, -1))
    moved_valid = np.roll(valid, (dy, dx), axis=(-2, -1)) & valid
    if dy > 0:
        moved_valid[:dy, :] = False
    elif dy < 0:
        moved_valid[dy:, :] = False
    if dx > 0:
        moved_valid[:, :dx] = False
    elif dx < 0:
        moved_valid[:, dx:] = False
    return moved, moved_valid


def normalise_scene(
    scene: np.ndarray, mean: np.ndarray, std: np.ndarray, valid: np.ndarray
) -> np.ndarray:
    """Fixed per-band standardisation for the land-cover model."""

    mean = np.asarray(mean, dtype=np.float32).reshape(-1, 1, 1)
    std = np.asarray(std, dtype=np.float32).reshape(-1, 1, 1)
    output = np.clip((scene - mean) / std, -CLIP, CLIP).astype(np.float32)
    output[:, ~valid] = 0.0
    return output
