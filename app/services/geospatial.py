"""Geospatially aware Sentinel-2 preprocessing for operational AOI analyses."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


class GeospatialDependencyError(RuntimeError):
    """Raised when real GeoTIFF preprocessing is requested without Rasterio."""


class SceneQualityError(RuntimeError):
    """Raised when a real acquisition cannot support a defensible comparison."""


def _rasterio():
    try:
        import rasterio
        from rasterio.features import shapes
        from rasterio.warp import Resampling, reproject, transform_geom
    except ImportError as error:
        raise GeospatialDependencyError(
            "Rasterio is required for real Sentinel-2 GeoTIFF preprocessing. "
            "Install requirements-operational.txt before running live imagery analysis."
        ) from error
    return rasterio, shapes, Resampling, reproject, transform_geom


@dataclass
class RasterScene:
    reflectance: np.ndarray  # B02, B03, B04, B08 in [0, 1]
    valid_mask: np.ndarray
    transform: Any
    crs: Any
    bounds: Any
    scl: np.ndarray | None
    clm: np.ndarray | None
    cloud_fraction: float | None
    source_path: Path


@dataclass
class PreparedPair:
    before_rgb: np.ndarray
    after_rgb: np.ndarray
    before_nir: np.ndarray
    after_nir: np.ndarray
    before_ndvi: np.ndarray
    after_ndvi: np.ndarray
    valid_mask: np.ndarray
    transform: Any
    crs: Any
    pixel_area_m2: float
    pixel_resolution_m: float
    quality: dict[str, float | str]


def _normalise_reflectance(values: np.ndarray) -> np.ndarray:
    values = values.astype(np.float32, copy=False)
    valid = values[np.isfinite(values)]
    if valid.size and float(np.percentile(valid, 99)) > 2.0:
        values = values / 10_000.0
    return np.clip(values, 0.0, 1.0)


def _quality_mask(scl: np.ndarray | None, clm: np.ndarray | None, base: np.ndarray) -> np.ndarray:
    valid = base.copy()
    if scl is not None:
        # Sentinel-2 L2A Scene Classification Layer: no data, defective, cloud
        # shadow, cloud, cirrus, and snow/ice are unsuitable for direct comparison.
        invalid_scl = np.isin(np.rint(scl).astype(np.int16), [0, 1, 3, 8, 9, 10, 11])
        valid &= ~invalid_scl
    if clm is not None:
        valid &= np.nan_to_num(clm, nan=1.0) < 0.5
    return valid


def read_sentinel_scene(path: Path) -> RasterScene:
    """Load one Process API GeoTIFF and preserve its grid/quality information."""

    rasterio, _, _, _, _ = _rasterio()
    try:
        with rasterio.open(path) as dataset:
            if dataset.count < 4:
                raise SceneQualityError(
                    f"{path.name} has {dataset.count} band(s); B02/B03/B04/B08 are required."
                )
            raw = dataset.read().astype(np.float32)
            reflectance = _normalise_reflectance(raw[:4])
            finite = np.all(np.isfinite(reflectance), axis=0)
            raster_mask = np.all(dataset.read_masks(indexes=[1, 2, 3, 4]) > 0, axis=0)
            scl = raw[4] if dataset.count >= 5 else None
            clm = raw[5] if dataset.count >= 6 else None
            valid = _quality_mask(scl, clm, finite & raster_mask)
            cloud_fraction = float(1.0 - valid.mean())
            return RasterScene(
                reflectance=reflectance,
                valid_mask=valid,
                transform=dataset.transform,
                crs=dataset.crs,
                bounds=dataset.bounds,
                scl=scl,
                clm=clm,
                cloud_fraction=cloud_fraction,
                source_path=path,
            )
    except SceneQualityError:
        raise
    except Exception as error:  # Rasterio exposes several driver-specific errors.
        raise SceneQualityError(f"Could not read GeoTIFF {path.name}: {error}") from error


def _same_grid(before: RasterScene, after: RasterScene) -> bool:
    return (
        before.reflectance.shape == after.reflectance.shape
        and before.crs == after.crs
        and before.transform.almost_equals(after.transform)
    )


def _align_to_reference(scene: RasterScene, reference: RasterScene) -> RasterScene:
    if _same_grid(reference, scene):
        return scene
    _, _, Resampling, reproject, _ = _rasterio()
    if reference.crs is None or scene.crs is None:
        raise SceneQualityError("Both acquisitions must contain a CRS for alignment.")
    aligned = np.zeros_like(reference.reflectance, dtype=np.float32)
    for index in range(4):
        reproject(
            source=scene.reflectance[index],
            destination=aligned[index],
            src_transform=scene.transform,
            src_crs=scene.crs,
            dst_transform=reference.transform,
            dst_crs=reference.crs,
            resampling=Resampling.bilinear,
        )
    aligned_valid = np.zeros(reference.valid_mask.shape, dtype=np.uint8)
    reproject(
        source=scene.valid_mask.astype(np.uint8),
        destination=aligned_valid,
        src_transform=scene.transform,
        src_crs=scene.crs,
        dst_transform=reference.transform,
        dst_crs=reference.crs,
        resampling=Resampling.nearest,
    )
    return RasterScene(
        reflectance=aligned,
        valid_mask=aligned_valid.astype(bool),
        transform=reference.transform,
        crs=reference.crs,
        bounds=reference.bounds,
        scl=None,
        clm=None,
        cloud_fraction=float(1.0 - aligned_valid.mean()),
        source_path=scene.source_path,
    )


def _pixel_area_m2(scene: RasterScene) -> float:
    """Derive physical pixel area from raster grid metadata, not a hard-coded value."""

    transform = scene.transform
    if scene.crs is not None and not getattr(scene.crs, "is_geographic", False):
        area = abs(transform.a * transform.e - transform.b * transform.d)
        if area > 0:
            return float(area)
    width = scene.reflectance.shape[2]
    height = scene.reflectance.shape[1]
    center_lat = (scene.bounds.bottom + scene.bounds.top) / 2
    width_m = abs(scene.bounds.right - scene.bounds.left) * 111_320 * max(
        0.05, math.cos(math.radians(center_lat))
    )
    height_m = abs(scene.bounds.top - scene.bounds.bottom) * 110_540
    return max(0.0001, (width_m / width) * (height_m / height))


def _ndvi(reflectance: np.ndarray) -> np.ndarray:
    red, nir = reflectance[2], reflectance[3]
    return np.divide(nir - red, nir + red, out=np.zeros_like(nir), where=(nir + red) > 1e-6)


def prepare_pair(before_path: Path, after_path: Path) -> PreparedPair:
    """Read, validate, align, mask, and transform a real Sentinel-2 pair."""

    before = read_sentinel_scene(before_path)
    raw_after = read_sentinel_scene(after_path)
    needed_alignment = not _same_grid(before, raw_after)
    after = _align_to_reference(raw_after, before)
    valid = before.valid_mask & after.valid_mask
    valid_fraction = float(valid.mean())
    if valid_fraction < 0.05:
        raise SceneQualityError(
            "Less than 5% of the pair is usable after no-data/cloud/shadow masking."
        )
    pixel_area = _pixel_area_m2(before)
    before_rgb = np.moveaxis(before.reflectance[[2, 1, 0]], 0, -1)
    after_rgb = np.moveaxis(after.reflectance[[2, 1, 0]], 0, -1)
    before_rgb = before_rgb.copy()
    after_rgb = after_rgb.copy()
    before_rgb[~valid] = 0.0
    after_rgb[~valid] = 0.0
    return PreparedPair(
        before_rgb=before_rgb,
        after_rgb=after_rgb,
        before_nir=before.reflectance[3],
        after_nir=after.reflectance[3],
        before_ndvi=_ndvi(before.reflectance),
        after_ndvi=_ndvi(after.reflectance),
        valid_mask=valid,
        transform=before.transform,
        crs=before.crs,
        pixel_area_m2=pixel_area,
        pixel_resolution_m=math.sqrt(pixel_area),
        quality={
            "valid_fraction": round(valid_fraction, 5),
            "before_unusable_fraction": round(float(before.cloud_fraction or 0.0), 5),
            "after_unusable_fraction": round(float(after.cloud_fraction or 0.0), 5),
            "alignment": "reprojected-to-before-grid" if needed_alignment else "same-grid",
            "pixel_area_m2": round(pixel_area, 5),
        },
    )


def _stretch_rgb(rgb: np.ndarray, valid: np.ndarray) -> np.ndarray:
    output = rgb.copy()
    for channel in range(3):
        values = output[:, :, channel][valid]
        if values.size:
            low, high = np.percentile(values, [2, 98])
            output[:, :, channel] = (output[:, :, channel] - low) / max(high - low, 1e-5)
    return np.clip(output * 255, 0, 255).astype(np.uint8)


def write_rgb_preview(rgb: np.ndarray, valid_mask: np.ndarray, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(_stretch_rgb(rgb, valid_mask), mode="RGB").save(destination, quality=92)


def component_geojson(component: np.ndarray, transform: Any, crs: Any) -> dict[str, Any] | None:
    """Vectorise a detected raster component to WGS84 GeoJSON for review/map display."""

    _, shapes, _, _, transform_geom = _rasterio()
    if not component.any():
        return None
    features = list(shapes(component.astype(np.uint8), mask=component, transform=transform))
    if not features:
        return None
    geometry = max(features, key=lambda item: item[1])[0]
    if crs is not None and str(crs).upper() not in {"EPSG:4326", "OGC:CRS84"}:
        geometry = transform_geom(crs, "EPSG:4326", geometry, precision=7)
    return geometry
