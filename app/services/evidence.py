"""Evidence images for a case: true colour, overlay, NDVI, panel and per-finding crops.

Every image shares one pixel grid and one display grid, warped from UTM to lat/lon so that
``image_bounds`` places it exactly on a web map. Before and after use one stretch computed
across the pair, so a brightness difference in the images is a difference on the ground.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from app.satellite.grid import AOIGrid

logger = logging.getLogger(__name__)

JPEG_QUALITY = 88
_DISPLAY_GAMMA = 1 / 1.25
_RGB = (2, 1, 0)  # B04, B03, B02 of the contract band order.
# RGB overlay colours per verdict; seasonal stays cool and quiet.
VERDICT_COLOURS = {
    "SUSPECTED_UNAUTHORISED": (255, 64, 64),
    "NATURAL_EVENT": (64, 164, 255),
    "NEEDS_REVIEW": (255, 176, 32),
    "AUTHORISED_ACTIVITY": (96, 220, 120),
    "SEASONAL_BENIGN": (180, 180, 200),
}
# Colorbrewer ramps as interpolation stops: red-yellow-green and red-white-blue.
_NDVI_STOPS = ((165, 0, 38), (244, 109, 67), (254, 224, 139), (166, 217, 106), (26, 152, 80), (0, 104, 55))
_CHANGE_STOPS = ((178, 24, 43), (239, 138, 98), (247, 247, 247), (103, 169, 207), (33, 102, 172))


@dataclass
class DisplayGrid:
    """A regular lat/lon raster covering the inner bounds of the UTM grid."""

    width: int
    height: int
    west: float
    south: float
    east: float
    north: float

    @property
    def transform(self):
        from rasterio.transform import from_bounds

        return from_bounds(self.west, self.south, self.east, self.north, self.width, self.height)

    @property
    def bounds_leaflet(self) -> list[list[float]]:
        return [[round(self.south, 7), round(self.west, 7)], [round(self.north, 7), round(self.east, 7)]]


def display_grid_for(grid: AOIGrid) -> DisplayGrid:
    west, south, east, north = grid.wgs84_bounds_inner()
    return DisplayGrid(grid.width, grid.height, west, south, east, north)


def warp_to_display(array: np.ndarray, grid: AOIGrid, display: DisplayGrid, *, nearest: bool) -> np.ndarray:
    """Warp a 2-D or [C,H,W] array from the UTM grid to the display grid."""

    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    source = array if array.ndim == 3 else array[np.newaxis]
    output = np.zeros((source.shape[0], display.height, display.width), dtype=source.dtype)
    reproject(
        source=source,
        destination=output,
        src_transform=grid.transform,
        src_crs=grid.crs,
        dst_transform=display.transform,
        dst_crs="EPSG:4326",
        resampling=Resampling.nearest if nearest else Resampling.bilinear,
    )
    return output if array.ndim == 3 else output[0]


def shared_stretch(
    scenes: list[np.ndarray], valid: np.ndarray, low: float = 2.0, high: float = 98.0
) -> tuple[np.ndarray, np.ndarray]:
    """Per-channel display limits from the pooled valid pixels of the whole set of scenes."""

    lows, highs = [], []
    for band in _RGB:
        pooled = np.concatenate([scene[band][valid] for scene in scenes]) if valid.any() else np.array([])
        if pooled.size == 0:
            lows.append(0.0)
            highs.append(0.3)
            continue
        low_value, high_value = np.percentile(pooled, [low, high])
        lows.append(float(low_value))
        highs.append(float(max(high_value, low_value + 1e-3)))
    # A single black and white point keeps colour balance natural across the pair.
    black, white = min(lows), max(highs)
    return np.full(3, black, dtype=np.float32), np.full(3, white, dtype=np.float32)


def true_colour(scene: np.ndarray, low: np.ndarray, high: np.ndarray) -> np.ndarray:
    """uint8 H x W x 3 from reflectance using supplied limits and a mild display gamma."""

    rgb = np.stack([scene[band] for band in _RGB], axis=-1)
    scaled = np.clip((rgb - low) / (high - low), 0.0, 1.0) ** _DISPLAY_GAMMA
    return (scaled * 255.0 + 0.5).astype(np.uint8)


def _colour_map(values: np.ndarray, stops, low: float, high: float, valid: np.ndarray) -> np.ndarray:
    scaled = np.clip((values - low) / (high - low), 0.0, 1.0)
    positions = np.linspace(0.0, 1.0, len(stops))
    table = np.asarray(stops, dtype=np.float32)
    rgb = np.stack([np.interp(scaled, positions, table[:, c]) for c in range(3)], axis=-1)
    rgb = (rgb + 0.5).astype(np.uint8)
    rgb[~valid] = (40, 40, 44)
    return rgb


def _warp_rgb(rgb: np.ndarray, grid: AOIGrid, display: DisplayGrid) -> np.ndarray:
    warped = warp_to_display(np.moveaxis(rgb, -1, 0), grid, display, nearest=True)
    return np.moveaxis(warped, 0, -1)


def _save(rgb: np.ndarray, path: Path) -> None:
    Image.fromarray(rgb, mode="RGB").save(path, format="JPEG", quality=JPEG_QUALITY, optimize=True)


def _overlay(
    after_rgb: np.ndarray, labels: np.ndarray, verdicts: list[str]
) -> np.ndarray:
    """Tint and outline each region in its verdict colour on the after image."""

    from scipy import ndimage

    output = after_rgb.astype(np.float32)
    for index, verdict in enumerate(verdicts, start=1):
        region = labels == index
        if not region.any():
            continue
        colour = np.array(VERDICT_COLOURS.get(verdict, (255, 176, 32)), dtype=np.float32)
        output[region] = 0.72 * output[region] + 0.28 * colour
        edge = region & ~ndimage.binary_erosion(region, iterations=1)
        output[edge] = colour
    return np.clip(output, 0, 255).astype(np.uint8)


def _panel(tiles: list[tuple[str, np.ndarray]]) -> np.ndarray:
    gap = 6
    label_height = 26
    height, width = tiles[0][1].shape[:2]
    canvas = Image.new(
        "RGB", (len(tiles) * width + (len(tiles) - 1) * gap, height + label_height), (14, 20, 24)
    )
    draw = ImageDraw.Draw(canvas)
    for position, (label, rgb) in enumerate(tiles):
        offset = position * (width + gap)
        canvas.paste(Image.fromarray(rgb, mode="RGB"), (offset, label_height))
        draw.text((offset + 8, 7), label, fill=(226, 232, 236))
    return np.asarray(canvas)


def _finding_crop(after_rgb: np.ndarray, region: np.ndarray, colour: tuple[int, int, int]) -> np.ndarray:
    """A square context crop around a region with its outline, sized for an alert card."""

    from scipy import ndimage

    ys, xs = np.where(region)
    centre_y, centre_x = int(ys.mean()), int(xs.mean())
    half = max(40, int(max(np.ptp(ys), np.ptp(xs)) * 0.75) + 12)
    height, width = region.shape
    top, left = max(0, centre_y - half), max(0, centre_x - half)
    bottom, right = min(height, centre_y + half), min(width, centre_x + half)
    crop = after_rgb[top:bottom, left:right].copy()
    region_crop = region[top:bottom, left:right]
    crop[region_crop & ~ndimage.binary_erosion(region_crop)] = colour
    image = Image.fromarray(crop, mode="RGB")
    image.thumbnail((256, 256), Image.Resampling.LANCZOS)
    if max(image.size) < 256:
        scale = 256 / max(image.size)
        image = image.resize(
            (int(image.width * scale), int(image.height * scale)), Image.Resampling.NEAREST
        )
    return np.asarray(image)


@dataclass
class EvidenceSet:
    bounds: list[list[float]]
    images: dict[str, Path]
    finding_thumbnails: list[Path]
    reference_image: Path | None


def write_evidence(
    folder: Path,
    *,
    grid: AOIGrid,
    before: np.ndarray,
    after: np.ndarray,
    valid: np.ndarray,
    ndvi_before: np.ndarray,
    ndvi_after: np.ndarray,
    labels: np.ndarray,
    verdicts: list[str],
    reference: np.ndarray | None = None,
) -> EvidenceSet:
    """Write all evidence for one case and return where it went."""

    folder.mkdir(parents=True, exist_ok=True)
    display = display_grid_for(grid)
    low, high = shared_stretch([before, after], valid)
    before_rgb = _warp_rgb(true_colour(before, low, high), grid, display)
    after_rgb = _warp_rgb(true_colour(after, low, high), grid, display)
    display_labels = warp_to_display(labels.astype(np.int32), grid, display, nearest=True)
    display_valid = warp_to_display(valid.astype(np.uint8), grid, display, nearest=True).astype(bool)
    ndvi_before_display = warp_to_display(ndvi_before, grid, display, nearest=False)
    ndvi_after_display = warp_to_display(ndvi_after, grid, display, nearest=False)
    overlay = _overlay(after_rgb, display_labels, verdicts)
    ndvi_before_rgb = _colour_map(ndvi_before_display, _NDVI_STOPS, -0.2, 0.9, display_valid)
    ndvi_after_rgb = _colour_map(ndvi_after_display, _NDVI_STOPS, -0.2, 0.9, display_valid)
    # Red for vegetation lost, blue for gained, pale for no change.
    ndvi_change_rgb = _colour_map(
        ndvi_after_display - ndvi_before_display, _CHANGE_STOPS, -0.6, 0.6, display_valid
    )
    panel = _panel(
        [
            ("BEFORE", before_rgb),
            ("AFTER", after_rgb),
            ("CHANGE", overlay),
            ("NDVI CHANGE", ndvi_change_rgb),
        ]
    )
    arrays = {
        "before": before_rgb,
        "after": after_rgb,
        "overlay": overlay,
        "ndvi_before": ndvi_before_rgb,
        "ndvi_after": ndvi_after_rgb,
        "ndvi_change": ndvi_change_rgb,
        "panel": panel,
    }
    paths: dict[str, Path] = {}
    for name, array in arrays.items():
        paths[name] = folder / f"{name}.jpg"
        _save(array, paths[name])
    reference_path: Path | None = None
    if reference is not None:
        reference_path = folder / "seasonal_reference.jpg"
        _save(_warp_rgb(true_colour(reference, low, high), grid, display), reference_path)
    thumbnails: list[Path] = []
    for index, verdict in enumerate(verdicts, start=1):
        region = display_labels == index
        path = folder / f"finding_{index}.jpg"
        source = region if region.any() else labels == index
        _save(
            _finding_crop(after_rgb, source, VERDICT_COLOURS.get(verdict, (255, 176, 32))),
            path,
        )
        thumbnails.append(path)
    return EvidenceSet(display.bounds_leaflet, paths, thumbnails, reference_path)


def write_scene_thumbnail(path: Path, scene: np.ndarray, valid: np.ndarray, grid: AOIGrid) -> None:
    """A small true-colour image for the timeline, stretched on its own valid pixels."""

    path.parent.mkdir(parents=True, exist_ok=True)
    low, high = shared_stretch([scene], valid)
    display = display_grid_for(grid)
    image = Image.fromarray(_warp_rgb(true_colour(scene, low, high), grid, display), mode="RGB")
    image.thumbnail((256, 256), Image.Resampling.LANCZOS)
    image.save(path, format="JPEG", quality=80, optimize=True)
