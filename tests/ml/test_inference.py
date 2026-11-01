"""Contract tests for app/ml/inference.py.

Checkpoints here are randomly initialised and written to a temporary directory: they exercise the
interface and never stand in for a trained model or a measured score.
"""

from __future__ import annotations

import threading
import zipfile
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from app.ml.inference import (  # noqa: E402
    LANDCOVER_CLASSES,
    LANDCOVER_NODATA,
    ChangeDetector,
    LandCoverClassifier,
    SiameseInference,
    get_change_detector,
    get_landcover_classifier,
)
from app.ml.landcover_unet import LandCoverUNet  # noqa: E402
from app.ml.preprocessing import (  # noqa: E402
    NORMALISE_PER_DATE,
    apply_date_shift,
    estimate_date_shift,
    normalise_pair,
)
from app.ml.siamese_unet import SiameseUNet  # noqa: E402
from data_tools.fetch_oscd import _safe_member_path  # noqa: E402
from data_tools.s2_stac import reflectance_offset, resolve_ambiguous_offset  # noqa: E402


class Smuggled:
    """A class weights_only loading must refuse to reconstruct."""


BASE = 4
SIZES = [(500, 500), (257, 301), (64, 64), (63, 65), (33, 17), (5, 200), (1, 1)]


@pytest.fixture(autouse=True)
def _cpu_only(monkeypatch):
    monkeypatch.setenv("SENTINELWATCH_ML_DEVICE", "cpu")


@pytest.fixture(scope="module")
def change_path(tmp_path_factory) -> Path:
    torch.manual_seed(0)
    path = tmp_path_factory.mktemp("ckpt") / "change.pt"
    torch.save(
        {
            "model_state": SiameseUNet(6, BASE, 0.1).state_dict(),
            "config": {"in_channels": 6, "base_channels": BASE, "dropout": 0.1},
            "threshold": 0.37,
            "version": "test",
        },
        path,
    )
    return path


@pytest.fixture(scope="module")
def landcover_path(tmp_path_factory) -> Path:
    torch.manual_seed(0)
    path = tmp_path_factory.mktemp("ckpt") / "landcover.pt"
    torch.save(
        {
            "model_state": LandCoverUNet(6, 8, BASE, 0.1).state_dict(),
            "config": {"in_channels": 6, "base_channels": BASE, "dropout": 0.1},
            "classes": list(LANDCOVER_CLASSES),
            "band_mean": [0.1] * 6,
            "band_std": [0.1] * 6,
            "version": "test",
        },
        path,
    )
    return path


def _scene(height: int, width: int, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).random((6, height, width), dtype=np.float32) * 0.4


@pytest.mark.parametrize("height,width", SIZES)
def test_change_shape_range_dtype(change_path, height, width):
    detector = ChangeDetector(change_path)
    output = detector.predict(
        _scene(height, width), _scene(height, width, 1), np.ones((height, width), dtype=bool)
    )
    assert output.shape == (height, width)
    assert output.dtype == np.float32
    assert np.isfinite(output).all()
    assert output.min() >= 0.0 and output.max() <= 1.0


@pytest.mark.parametrize("height,width", SIZES)
def test_landcover_shape_range_dtype(landcover_path, height, width):
    classifier = LandCoverClassifier(landcover_path)
    labels, probabilities = classifier.predict(_scene(height, width), np.ones((height, width), dtype=bool))
    assert labels.shape == (height, width) and labels.dtype == np.uint8
    assert probabilities.shape == (8, height, width) and probabilities.dtype == np.float32
    assert labels.max() < 8
    np.testing.assert_allclose(probabilities.sum(axis=0), 1.0, atol=1e-4)


def test_threshold_comes_from_checkpoint(change_path):
    assert ChangeDetector(change_path).threshold == pytest.approx(0.37)


def test_change_is_deterministic(change_path):
    before, after = _scene(300, 300), _scene(300, 300, 1)
    valid = np.ones((300, 300), dtype=bool)
    detector = ChangeDetector(change_path)
    np.testing.assert_array_equal(detector.predict(before, after, valid), detector.predict(before, after, valid))
    np.testing.assert_array_equal(detector.predict(before, after, valid), ChangeDetector(change_path).predict(before, after, valid))


def test_landcover_is_deterministic(landcover_path):
    scene, valid = _scene(300, 300), np.ones((300, 300), dtype=bool)
    classifier = LandCoverClassifier(landcover_path)
    first = classifier.predict(scene, valid)
    second = classifier.predict(scene, valid)
    np.testing.assert_array_equal(first[0], second[0])
    np.testing.assert_array_equal(first[1], second[1])


def test_change_invalid_pixels_are_zero(change_path):
    valid = np.ones((128, 128), dtype=bool)
    valid[10:40, 20:90] = False
    output = ChangeDetector(change_path).predict(_scene(128, 128), _scene(128, 128, 1), valid)
    assert (output[~valid] == 0.0).all()


def test_change_nonfinite_pixels_become_invalid(change_path):
    before = _scene(96, 96)
    before[2, 5:9, 5:9] = np.nan
    output = ChangeDetector(change_path).predict(before, _scene(96, 96, 1), np.ones((96, 96), dtype=bool))
    assert np.isfinite(output).all()
    assert (output[5:9, 5:9] == 0.0).all()


def test_invalid_pixel_values_cannot_affect_valid_output(change_path):
    """Whatever an obscured pixel holds, the statistics and the model must not see it differently."""

    valid = np.ones((128, 128), dtype=bool)
    valid[:40] = False
    before, after = _scene(128, 128), _scene(128, 128, 1)
    bright = before.copy()
    bright[:, ~valid] = 1.0
    detector = ChangeDetector(change_path)
    np.testing.assert_array_equal(detector.predict(before, after, valid), detector.predict(bright, after, valid))


def test_all_invalid_returns_zeros(change_path, landcover_path):
    valid = np.zeros((70, 90), dtype=bool)
    assert (ChangeDetector(change_path).predict(_scene(70, 90), _scene(70, 90), valid) == 0).all()
    labels, probabilities = LandCoverClassifier(landcover_path).predict(_scene(70, 90), valid)
    assert (labels == LANDCOVER_NODATA).all()
    assert (probabilities == 0).all()


def test_landcover_invalid_pixels_are_nodata(landcover_path):
    valid = np.ones((96, 96), dtype=bool)
    valid[0:12, 0:12] = False
    labels, probabilities = LandCoverClassifier(landcover_path).predict(_scene(96, 96), valid)
    assert (labels[~valid] == LANDCOVER_NODATA).all()
    assert (probabilities[:, ~valid] == 0).all()
    assert (labels[valid] < 8).all()


def test_missing_checkpoint_is_graceful(tmp_path):
    detector = ChangeDetector(tmp_path / "absent.pt")
    assert detector.available is False
    assert detector.unavailable_reason
    with pytest.raises(RuntimeError):
        detector.predict(_scene(8, 8), _scene(8, 8), np.ones((8, 8), dtype=bool))
    classifier = LandCoverClassifier(tmp_path / "absent.pt")
    assert classifier.available is False
    with pytest.raises(RuntimeError):
        classifier.predict(_scene(8, 8), np.ones((8, 8), dtype=bool))


def test_corrupt_checkpoint_is_graceful(tmp_path):
    bad = tmp_path / "bad.pt"
    bad.write_bytes(b"not a checkpoint")
    assert ChangeDetector(bad).available is False
    assert LandCoverClassifier(bad).available is False


def test_pickled_code_is_refused(tmp_path):
    """weights_only=True must refuse a checkpoint that smuggles an arbitrary object."""

    path = tmp_path / "smuggle.pt"
    torch.save({"model_state": Smuggled(), "config": {}, "threshold": 0.5}, path)
    assert ChangeDetector(path).available is False


def test_wrong_shapes_raise(change_path):
    detector = ChangeDetector(change_path)
    good, valid = _scene(32, 32), np.ones((32, 32), dtype=bool)
    with pytest.raises(ValueError):
        detector.predict(good[:3], good[:3], valid)
    with pytest.raises(ValueError):
        detector.predict(good, _scene(32, 33), valid)
    with pytest.raises(ValueError):
        detector.predict(good, good, np.ones((31, 32), dtype=bool))


def test_concurrent_predictions_match_serial(change_path):
    detector = ChangeDetector(change_path)
    before, after = _scene(200, 200), _scene(200, 200, 1)
    valid = np.ones((200, 200), dtype=bool)
    expected = detector.predict(before, after, valid)
    results: list[np.ndarray] = []

    def run() -> None:
        results.append(detector.predict(before, after, valid))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4
    for result in results:
        np.testing.assert_array_equal(result, expected)


def test_singletons_are_cached():
    assert get_change_detector() is get_change_detector()
    assert get_landcover_classifier() is get_landcover_classifier()


def test_compatibility_alias_stays_on_labelled_baseline(change_path):
    """Legacy callers pass RGB; the six-band model must not claim to serve them."""

    alias = SiameseInference(change_path)
    assert alias.available is False
    with pytest.raises(RuntimeError):
        alias.predict(np.zeros((8, 8, 3)), np.zeros((8, 8, 3)))


def test_pair_normalisation_removes_band_offset_and_gain():
    """The L1C to L2A shift is first-order per band, so standardising should nearly cancel it."""

    before, after = _scene(128, 128), _scene(128, 128, 1)
    valid = np.ones((128, 128), dtype=bool)
    gain = np.array([0.8, 0.9, 1.1, 1.0, 1.2, 0.7], dtype=np.float32).reshape(-1, 1, 1)
    offset = np.array([0.1, 0.07, 0.05, 0.03, 0.01, 0.0], dtype=np.float32).reshape(-1, 1, 1)
    plain = normalise_pair(before, after, valid)
    shifted = normalise_pair(before * gain + offset, after * gain + offset, valid)
    np.testing.assert_allclose(plain[0], shifted[0], atol=1e-3)
    np.testing.assert_allclose(plain[1], shifted[1], atol=1e-3)


def test_pair_normalisation_keeps_scene_wide_change():
    before = _scene(64, 64)
    normalised_before, normalised_after = normalise_pair(before, before + 0.2, np.ones((64, 64), dtype=bool))
    assert (normalised_after - normalised_before).mean() > 0.5


def test_per_date_normalisation_cancels_date_radiometry():
    before = _scene(128, 128)
    valid = np.ones((128, 128), dtype=bool)
    gain = np.array([0.8, 0.9, 1.1, 1.0, 1.2, 0.7], dtype=np.float32).reshape(-1, 1, 1)
    first, second = normalise_pair(before, before * gain + 0.05, valid, NORMALISE_PER_DATE)
    np.testing.assert_allclose(first, second, atol=1e-3)


def test_detector_follows_checkpoint_normalisation(tmp_path):
    path = tmp_path / "per_date.pt"
    torch.save(
        {
            "model_state": SiameseUNet(6, BASE, 0.0, "group").state_dict(),
            "config": {"in_channels": 6, "base_channels": BASE, "dropout": 0.0, "norm": "group"},
            "threshold": 0.5,
            "normalisation": NORMALISE_PER_DATE,
        },
        path,
    )
    detector = ChangeDetector(path)
    assert detector.available and detector.normalisation == NORMALISE_PER_DATE
    output = detector.predict(_scene(70, 70), _scene(70, 70, 1), np.ones((70, 70), dtype=bool))
    assert output.shape == (70, 70)


def test_unknown_normalisation_is_refused(tmp_path):
    path = tmp_path / "unknown.pt"
    torch.save(
        {
            "model_state": SiameseUNet(6, BASE).state_dict(),
            "config": {"in_channels": 6, "base_channels": BASE},
            "threshold": 0.5,
            "normalisation": "something_else",
        },
        path,
    )
    assert ChangeDetector(path).available is False


def test_date_shift_recovers_a_known_offset():
    # A smooth random field, because estimate_date_shift needs structure to correlate on.
    rng = np.random.default_rng(3)
    field = rng.random((6, 200, 200), dtype=np.float32)
    for _ in range(2):
        field = (field + np.roll(field, 1, -1) + np.roll(field, 1, -2)) / 3
    moved = np.roll(field, (-2, 3), axis=(-2, -1))
    valid = np.ones((200, 200), dtype=bool)
    assert estimate_date_shift(field, moved, valid) == (2, -3)
    assert estimate_date_shift(field, field, valid) == (0, 0)


def test_date_shift_invalidates_uncovered_edges():
    stack, valid = np.ones((6, 10, 10), dtype=np.float32), np.ones((10, 10), dtype=bool)
    _, moved_valid = apply_date_shift(stack, valid, (2, -1))
    assert not moved_valid[:2].any() and not moved_valid[:, -1].any() and moved_valid[2:, :-1].all()


def test_small_aoi_is_never_shifted():
    assert estimate_date_shift(_scene(20, 20), _scene(20, 20, 1), np.ones((20, 20), dtype=bool)) == (0, 0)


def test_zip_traversal_is_rejected(tmp_path):
    for name in ("../evil.tif", "/abs/evil.tif", "a/../../evil.tif"):
        with pytest.raises(ValueError):
            _safe_member_path(tmp_path, zipfile.ZipInfo(name))
    symlink = zipfile.ZipInfo("link.tif")
    symlink.external_attr = 0o120777 << 16
    with pytest.raises(ValueError):
        _safe_member_path(tmp_path, symlink)
    assert _safe_member_path(tmp_path, zipfile.ZipInfo("ok/fine.tif")).is_relative_to(tmp_path.resolve())


def test_reflectance_offset_rules():
    assert reflectance_offset({"s2:processing_baseline": "03.01", "earthsearch:boa_offset_applied": False}) == 0.0
    assert reflectance_offset({"s2:processing_baseline": "05.09", "earthsearch:boa_offset_applied": True}) == 0.0
    assert reflectance_offset({"s2:processing_baseline": "04.00", "earthsearch:boa_offset_applied": False}) is None


def test_ambiguous_offset_decided_from_pixels():
    dark = np.full((6, 16, 16), 300, dtype=np.uint16)
    assert resolve_ambiguous_offset(dark) == 0.0
    assert resolve_ambiguous_offset(dark + 1000) == -0.1
