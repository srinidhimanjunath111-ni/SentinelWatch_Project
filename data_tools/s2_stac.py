"""Credential-free Sentinel-2 L2A access through Earth Search with windowed COG reads.

Reflectance across processing baselines
---------------------------------------
Earth Search's `raster:bands` offset of -0.1 cannot be applied blindly. Measured on deep water in
the Bay of Bengal (tile 44PMV, SWIR2 median digital number, Oct-Nov 2026):

    S2B_44PMV_20190327_0  baseline 02.11  boa_offset_applied False  offset  0    DN 207
    S2B_44PMV_20190327_1  baseline 05.00  boa_offset_applied True   offset -0.1  DN 202  (same pass)
    S2A_44PMV_20220224_0  baseline 04.00  boa_offset_applied False  offset -0.1  DN 1069 (+1000 present)
    S2B_44PMV_20220321_0  baseline 04.00  boa_offset_applied False  offset -0.1  DN 212  (+1000 absent)

So `boa_offset_applied: True` means the pixels are already corrected and the listed offset must
not be applied again: doing so would make deep water -0.08. For baseline >= 04.00 with the flag
False, the same metadata covers both kinds of pixels. `reflectance_offset` therefore returns None
for those items, the dataset builders skip them, and `resolve_ambiguous_offset` decides from the
pixels for anyone who cannot skip a scene.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import numpy as np

SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
ASSETS = ("blue", "green", "red", "nir", "swir16", "swir22")
TEN_METRE = {"blue", "green", "red", "nir"}
# Contract section 2: no-data, saturated, cloud shadow, cloud medium/high, cirrus.
INVALID_SCL = (0, 1, 3, 8, 9, 10)

GDAL_ENV = {
    "AWS_NO_SIGN_REQUEST": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "GDAL_HTTP_TIMEOUT": "60",
    "VSI_CACHE": "FALSE",
    "GDAL_CACHEMAX": "128",
}


def apply_gdal_env() -> None:
    for key, value in GDAL_ENV.items():
        os.environ[key] = value


def search(
    bbox: tuple[float, float, float, float],
    start: str,
    end: str,
    *,
    max_cloud: float,
    limit: int = 50,
    attempts: int = 5,
) -> list[dict]:
    """POST a STAC search with retries, returning item dicts. Dates are YYYY-MM-DD."""

    body = json.dumps(
        {
            "collections": ["sentinel-2-l2a"],
            "bbox": list(bbox),
            "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
            "limit": limit,
            "query": {"eo:cloud_cover": {"lt": max_cloud}},
        }
    ).encode()
    delay = 1.0
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(SEARCH_URL, body, {"content-type": "application/json"})
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                return json.load(response)["features"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2
    return []


def reflectance_offset(properties: dict) -> float | None:
    """Offset to add after scaling: 0.0 when known, None when metadata cannot settle it."""

    baseline = str(properties.get("s2:processing_baseline", "00.00"))
    if baseline < "04.00" or properties.get("earthsearch:boa_offset_applied") is True:
        return 0.0
    return None


def resolve_ambiguous_offset(digital_numbers: np.ndarray) -> float:
    """Decide the offset for an ambiguous item from its own [C,H,W] pixels (0 is no-data).

    With the +1000 still present, the darkest band's dark pixels sit at or above 1000. Without it,
    in almost any land or water scene the blue band's darkest percentile falls well below that.
    Very bright windows such as snow, or cloud that SCL has already removed, can look offset. A
    caller needing certainty should prefer a scene whose metadata settles the question.
    """

    lowest = []
    for band in digital_numbers:
        present = band[band > 0]
        if present.size:
            lowest.append(float(np.percentile(present, 1)))
    if lowest and min(lowest) >= 1000.0:
        return -0.1
    return 0.0


def read_window(
    item: dict,
    transform,
    crs,
    height: int,
    width: int,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Read 6 bands and SCL onto the given grid. Returns uint16 reflectance x 10000 and valid mask.

    The grid must share the item's CRS. B11, B12 and SCL are 20 m: bands are upsampled bilinearly
    per contract section 2 and SCL by nearest neighbour. Returns None if an offset is unresolved.
    """

    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds

    offset = reflectance_offset(item["properties"])
    if offset is None:
        return None
    left = transform.c
    top = transform.f
    right = left + transform.a * width
    bottom = top + transform.e * height
    layers = []
    for name in (*ASSETS, "scl"):
        with rasterio.open(item["assets"][name]["href"]) as dataset:
            if dataset.crs != crs:
                raise ValueError(f"{item['id']} is {dataset.crs}, grid is {crs}")
            source_window = from_bounds(left, bottom, right, top, dataset.transform)
            data = dataset.read(
                1,
                window=source_window,
                out_shape=(height, width),
                boundless=True,
                fill_value=0,
                resampling=Resampling.bilinear if name not in TEN_METRE and name != "scl" else Resampling.nearest,
            )
        layers.append(data)
    stack = np.stack(layers[:6]).astype(np.uint16)
    scl = layers[6]
    valid = ~np.isin(scl, INVALID_SCL) & (stack > 0).all(axis=0)
    return stack, valid
