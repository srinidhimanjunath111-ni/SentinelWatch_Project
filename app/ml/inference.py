"""Model inference for SentinelWatch (engineering contract, section 3).

Everything here works without a checkpoint and without PyTorch: `available` is simply False, so
the service can run and say so plainly. Checkpoints load with weights_only=True. Inputs are the
contract arrays, float32 reflectance [6,H,W] in band order B02 B03 B04 B08 B11 B12, plus a bool
`valid` H x W.

Set SENTINELWATCH_ML_DEVICE (cpu, mps, cuda) to override device selection. Docker is CPU-only and
picks cpu automatically.
"""

from __future__ import annotations

import logging
import os
import pickle
import threading
from pathlib import Path

import numpy as np

from app.ml.preprocessing import (
    BAND_ORDER,
    NORMALISATION_MODES,
    NORMALISE_POOLED,
    apply_date_shift,
    estimate_date_shift,
    finite_valid,
    normalise_pair,
    normalise_scene,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CHANGE_CHECKPOINT = ROOT / "models" / "checkpoints" / "change_siamese.pt"
LANDCOVER_CHECKPOINT = ROOT / "models" / "checkpoints" / "landcover_unet.pt"

LANDCOVER_CLASSES = ("tree", "shrub_grass", "crop", "built", "bare", "water", "wetland", "mine")
# Class index reported for invalid pixels: the satellite did not see them, so no class is claimed.
LANDCOVER_NODATA = 255

WINDOW = 256
OVERLAP = 64
BATCH_SIZE = 4
# Three pooling stages need a multiple of 8. Below 64 px the receptive field has little to see,
# so smaller inputs are reflect-padded to 64 and cropped back afterwards.
MIN_SIDE = 64
MULTIPLE = 8

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

# A bad checkpoint must leave the service running with available=False, never crash it.
_LOAD_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    KeyError,
    ValueError,
    TypeError,
    AttributeError,
    EOFError,
    pickle.UnpicklingError,
)


def select_device() -> str:
    """Return the configured device if usable, otherwise the best one present."""

    import torch

    requested = os.getenv("SENTINELWATCH_ML_DEVICE", "").strip().lower()
    if requested == "cpu":
        return "cpu"
    if requested == "cuda" and torch.cuda.is_available():
        return "cuda"
    if requested == "mps" and torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _blend_weights(height: int, width: int) -> np.ndarray:
    """Tent weights so overlapping windows fade into each other without visible seams."""

    def ramp(length: int) -> np.ndarray:
        centre = (length - 1) / 2
        return 1.0 - np.abs(np.arange(length) - centre) / (centre + 1)

    return np.minimum.outer(ramp(height), ramp(width)).astype(np.float32)


def _positions(length: int, window: int, overlap: int) -> list[int]:
    if length <= window:
        return [0]
    stride = window - overlap
    positions = list(range(0, length - window, stride))
    positions.append(length - window)
    return positions


def _pad_amounts(height: int, width: int) -> tuple[int, int]:
    def target(side: int) -> int:
        side = max(side, MIN_SIDE)
        return side + (-side % MULTIPLE)

    return target(height) - height, target(width) - width


def sliding_window_logits(
    forward,
    inputs: tuple[np.ndarray, ...],
    out_channels: int,
    *,
    window: int = WINDOW,
    overlap: int = OVERLAP,
    batch_size: int = BATCH_SIZE,
    device: str = "cpu",
) -> np.ndarray:
    """Run `forward(*tensors) -> [B,C,h,w]` over windows, returning blended [C,H,W] outputs.

    The same function drives validation, test evaluation and live inference, so a reported metric
    describes the code that serves predictions.
    """

    import torch

    height, width = inputs[0].shape[-2:]
    pad_h, pad_w = _pad_amounts(height, width)
    if pad_h or pad_w:
        pad = ((0, 0), (0, pad_h), (0, pad_w))
        inputs = tuple(np.pad(array, pad, mode="reflect") for array in inputs)
    full_h, full_w = inputs[0].shape[-2:]
    win_h, win_w = min(window, full_h), min(window, full_w)
    win_h -= win_h % MULTIPLE
    win_w -= win_w % MULTIPLE
    weights = _blend_weights(win_h, win_w)
    total = np.zeros((out_channels, full_h, full_w), dtype=np.float32)
    weight_sum = np.zeros((full_h, full_w), dtype=np.float32)
    corners = [
        (top, left)
        for top in _positions(full_h, win_h, overlap)
        for left in _positions(full_w, win_w, overlap)
    ]
    with torch.inference_mode():
        for start in range(0, len(corners), batch_size):
            chunk = corners[start : start + batch_size]
            tensors = [
                torch.from_numpy(
                    np.stack([a[:, t : t + win_h, x : x + win_w] for t, x in chunk])
                ).to(device)
                for a in inputs
            ]
            outputs = forward(*tensors).detach().to("cpu").numpy()
            for (top, left), output in zip(chunk, outputs, strict=True):
                total[:, top : top + win_h, left : left + win_w] += output * weights
                weight_sum[top : top + win_h, left : left + win_w] += weights
    blended = total / np.maximum(weight_sum, 1e-6)
    return blended[:, :height, :width]


def _check_scene(name: str, array: np.ndarray) -> None:
    if array.ndim != 3 or array.shape[0] != len(BAND_ORDER) or min(array.shape[1:]) < 1:
        raise ValueError(f"{name} must be [6,H,W] with non-empty H and W, got {array.shape}")


def _check_valid(valid: np.ndarray, shape: tuple[int, int]) -> None:
    if np.shape(valid) != shape:
        raise ValueError(f"valid must be H x W {shape}, got {np.shape(valid)}")


class ChangeDetector:
    """Siamese U-Net change detector."""

    def __init__(self, checkpoint: Path | None = None):
        self.checkpoint = Path(checkpoint) if checkpoint is not None else CHANGE_CHECKPOINT
        self.available = False
        self.device = "cpu"
        self.threshold = 0.5
        self.normalisation = NORMALISE_POOLED
        self.temperature = 1.0
        self.align_dates = False
        self.version: str | None = None
        self.unavailable_reason: str | None = None
        self._model = None
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.checkpoint.is_file():
            self.unavailable_reason = f"No checkpoint at {self.checkpoint}"
            return
        try:
            import torch

            from app.ml.siamese_unet import SiameseUNet

            device = select_device()
            payload = torch.load(self.checkpoint, map_location="cpu", weights_only=True)
            config = payload["config"]
            model = SiameseUNet(
                in_channels=int(config["in_channels"]),
                base_channels=int(config["base_channels"]),
                dropout=float(config.get("dropout", 0.0)),
                norm=str(config.get("norm", "batch")),
            )
            model.load_state_dict(payload["model_state"])
            model.to(device).eval()
            normalisation = str(payload.get("normalisation", NORMALISE_POOLED))
            if normalisation not in NORMALISATION_MODES:
                raise ValueError(f"Unknown normalisation {normalisation!r}")
            self.normalisation = normalisation
            self.threshold = float(payload["threshold"])
            self.temperature = float(payload.get("temperature", 1.0))
            self.align_dates = bool(payload.get("align_dates", False))
            self.version = str(payload.get("version", "unknown"))
            self.device = device
            self._model = model
            self.available = True
        except _LOAD_ERRORS as error:
            self.unavailable_reason = f"Checkpoint could not be loaded: {error}"
            logger.warning("Change model unavailable: %s", error)

    def predict(self, before: np.ndarray, after: np.ndarray, valid: np.ndarray) -> np.ndarray:
        """[6,H,W] x 2 -> float32 H x W change probability in [0,1]. Sliding window, any size.

        Invalid pixels (valid False, or non-finite in either date) return probability 0.
        """

        if not self.available or self._model is None:
            raise RuntimeError(self.unavailable_reason or "Change model is unavailable.")
        _check_scene("before", before)
        _check_scene("after", after)
        if before.shape != after.shape:
            raise ValueError(f"before {before.shape} and after {after.shape} differ")
        _check_valid(valid, before.shape[1:])
        import torch

        mask = finite_valid(valid, before, after)
        if not mask.any():
            return np.zeros(before.shape[1:], dtype=np.float32)
        clean_before = np.nan_to_num(before.astype(np.float32))
        clean_after = np.nan_to_num(after.astype(np.float32))
        if self.align_dates:
            # Output stays on the before grid. Edge pixels the shift uncovers report no change.
            clean_after, mask = apply_date_shift(
                clean_after, mask, estimate_date_shift(clean_before, clean_after, mask)
            )
        norm_before, norm_after = normalise_pair(clean_before, clean_after, mask, self.normalisation)
        # MPS and BatchNorm eval are read-only but not documented as thread-safe; one forward at a
        # time keeps concurrent scheduler workers from contending for the same device memory.
        with self._lock:
            logits = sliding_window_logits(
                self._model, (norm_before, norm_after), 1, device=self.device
            )[0]
        # The temperature, fitted on validation data, makes the probability mean what it says.
        probability = (
            torch.sigmoid(torch.from_numpy(logits) / self.temperature).numpy().astype(np.float32)
        )
        probability = np.clip(probability, 0.0, 1.0)
        probability[~mask] = 0.0
        return probability


class LandCoverClassifier:
    """Per-pixel land-cover U-Net. Invalid pixels get LANDCOVER_NODATA and all-zero probabilities."""

    def __init__(self, checkpoint: Path | None = None):
        self.checkpoint = Path(checkpoint) if checkpoint is not None else LANDCOVER_CHECKPOINT
        self.available = False
        self.device = "cpu"
        self.version: str | None = None
        self.unavailable_reason: str | None = None
        self._model = None
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.checkpoint.is_file():
            self.unavailable_reason = f"No checkpoint at {self.checkpoint}"
            return
        try:
            import torch

            from app.ml.landcover_unet import LandCoverUNet

            device = select_device()
            payload = torch.load(self.checkpoint, map_location="cpu", weights_only=True)
            config = payload["config"]
            if tuple(payload["classes"]) != LANDCOVER_CLASSES:
                raise ValueError(f"Checkpoint classes differ: {payload['classes']}")
            model = LandCoverUNet(
                in_channels=int(config["in_channels"]),
                classes=len(LANDCOVER_CLASSES),
                base_channels=int(config["base_channels"]),
                dropout=float(config.get("dropout", 0.0)),
            )
            model.load_state_dict(payload["model_state"])
            model.to(device).eval()
            self._mean = np.asarray(payload["band_mean"], dtype=np.float32)
            self._std = np.asarray(payload["band_std"], dtype=np.float32)
            self.version = str(payload.get("version", "unknown"))
            self.device = device
            self._model = model
            self.available = True
        except _LOAD_ERRORS as error:
            self.unavailable_reason = f"Checkpoint could not be loaded: {error}"
            logger.warning("Land-cover model unavailable: %s", error)

    def predict(self, scene: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """[6,H,W] -> (uint8 H x W class index, float32 [8,H,W] probabilities)."""

        if not self.available or self._model is None:
            raise RuntimeError(self.unavailable_reason or "Land-cover model is unavailable.")
        _check_scene("scene", scene)
        _check_valid(valid, scene.shape[1:])
        import torch

        classes = len(LANDCOVER_CLASSES)
        mask = finite_valid(valid, scene)
        if not mask.any():
            return (
                np.full(scene.shape[1:], LANDCOVER_NODATA, dtype=np.uint8),
                np.zeros((classes, *scene.shape[1:]), dtype=np.float32),
            )
        normalised = normalise_scene(
            np.nan_to_num(scene.astype(np.float32)), self._mean, self._std, mask
        )
        with self._lock:
            logits = sliding_window_logits(self._model, (normalised,), classes, device=self.device)
        probabilities = torch.softmax(torch.from_numpy(logits), dim=0).numpy().astype(np.float32)
        labels = probabilities.argmax(axis=0).astype(np.uint8)
        labels[~mask] = LANDCOVER_NODATA
        probabilities[:, ~mask] = 0.0
        return labels, probabilities


_singleton_lock = threading.Lock()
_change_detector: ChangeDetector | None = None
_landcover_classifier: LandCoverClassifier | None = None


def get_change_detector() -> ChangeDetector:
    """Process-wide cached ChangeDetector; safe for concurrent threads."""

    global _change_detector
    if _change_detector is None:
        with _singleton_lock:
            if _change_detector is None:
                _change_detector = ChangeDetector()
    return _change_detector


def get_landcover_classifier() -> LandCoverClassifier:
    """Process-wide cached LandCoverClassifier; safe for concurrent threads."""

    global _landcover_classifier
    if _landcover_classifier is None:
        with _singleton_lock:
            if _landcover_classifier is None:
                _landcover_classifier = LandCoverClassifier()
    return _landcover_classifier


class SiameseInference:
    """Compatibility shim for app/services/{analysis,system}.py until the backend moves over.

    Those callers pass H x W x 3 RGB arrays. The installed model needs six bands, and SWIR cannot
    honestly be invented from RGB, so this reports available=False. analysis.py then uses its
    labelled spectral baseline and never attributes a result to a model it did not run. New code
    should call get_change_detector().
    """

    def __init__(self, checkpoint: Path, threshold: float = 0.50):
        self.checkpoint = checkpoint
        self.threshold = threshold
        self.available = False
        self.device = "cpu"
        self.model = None

    def predict(self, before: np.ndarray, after: np.ndarray) -> np.ndarray:
        raise RuntimeError(
            "The RGB interface cannot run the six-band model; use get_change_detector()."
        )
