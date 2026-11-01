from pathlib import Path

import numpy as np
from PIL import Image

from app.services.analysis import _connected_components, analyse_pair, write_visual_evidence


def _save_pair(folder: Path, before: np.ndarray, after: np.ndarray) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    before_path, after_path = folder / "before.png", folder / "after.png"
    Image.fromarray(before).save(before_path)
    Image.fromarray(after).save(after_path)
    return before_path, after_path


def test_connected_components_removes_small_noise():
    mask = np.zeros((20, 20), dtype=bool)
    mask[2:6, 2:6] = True
    mask[15, 15] = True
    components = _connected_components(mask, min_pixels=4)
    assert len(components) == 1
    assert components[0].sum() == 16


def test_deforestation_like_change_creates_evidence(tmp_path: Path):
    before = np.full((240, 240, 3), [55, 120, 65], dtype=np.uint8)
    after = before.copy()
    # A substantial green-canopy to exposed-earth conversion.
    after[60:190, 75:190] = [165, 112, 58]
    before_path, after_path = _save_pair(tmp_path, before, after)
    result = analyse_pair(before_path, after_path, pixel_resolution_m=2.0)
    assert result.changed_area_pixels > 4_000
    assert result.detections
    assert result.detections[0].activity == "deforestation"
    output = tmp_path / "evidence.jpg"
    write_visual_evidence(before_path, after_path, result, output)
    assert output.is_file() and output.stat().st_size > 1000
