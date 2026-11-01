"""Build India forest-change pairs: Sentinel-2 L2A labelled from Hansen Global Forest Change.

    python -m data_tools.build_india_forest_change --max-minutes 35

Labels. Hansen `lossyear` gives the year of stand-replacement tree-cover loss at 30 m. For a pair
spanning year Y:
    1       loss in Y
    0       forest (treecover2000 >= 30) with no recorded loss in any year
    IGNORE  everything else, as follows
- Outside forest, other change is real and unlabelled, so it must not count as a false alarm.
- Loss in Y-1 or Y+1 is ignored instead of being labelled 1. Year Y-1 loss mostly happened before
  the before scene, so it is not a change in the pair at all, though the annual label cannot say
  which pixels. Ignoring them keeps Hansen's date uncertainty out of both classes. Measured spectral
  evidence for this choice is written to the dataset summary (see `spectral_evidence`).
- Loss in any other year is regrowth or already-cleared land: also unlabelled.
- Cloud, shadow and no-data in either date are ignored.

Dates. The before scene comes from Dec Y-1 to Feb Y, and the after scene from Jan to mid-Mar Y+1.
Both are dry season and close in phenology, from the same MGRS tile, and the window must be under
15 percent invalid by SCL in both.

Splits are by 2 degree block with blocks assigned per region from a fixed seed, and chips must stay
inside their block by a margin so that held-out chips are different places.
"""

from __future__ import annotations

import argparse
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import numpy as np

from data_tools.fetch_oscd import ChangeRecord
from data_tools.s2_stac import apply_gdal_env, read_window, reflectance_offset, search

apply_gdal_env()

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "hansen_gfc"
CACHE_DIR = ROOT / "data" / "processed" / "india_forest"
BOUNDARIES = ROOT / "data" / "raw" / "naturalearth" / "ne_50m_admin_1.geojson"
VERSION = "GFC-2025-v1.13"
BASE_URL = "https://storage.googleapis.com/earthenginepartners-hansen"
LICENCE = "CC BY 4.0, Hansen/UMD/Google/USGS/NASA; imagery Copernicus Sentinel-2 open data"

IGNORE = 255
CHIP = 256
CELL = 85  # Hansen pixels per scan cell, about 2.5 km, close to one chip
FOREST_COVER = 30
MIN_LOSS_PIXELS = 30  # about 2.7 ha at 30 m: smaller clearings are mostly edge noise at 10 m
YEARS = tuple(range(2018, 2024))
MAX_INVALID = 0.15
BLOCK = 2  # degrees
BLOCK_MARGIN = 0.03

# Region boxes (west, south, east, north) restricted to Indian territory by the state boundaries.
REGIONS = {
    "central_india": (78.0, 19.0, 86.0, 25.0),
    "northeast": (90.0, 22.0, 97.5, 29.5),
    "western_ghats": (73.0, 8.0, 78.0, 16.5),
    "andaman": (92.0, 6.5, 94.0, 14.0),
}
# Loss-pair quotas per region; no-loss control pairs are planned separately.
QUOTAS = {"central_india": 150, "northeast": 170, "western_ghats": 110, "andaman": 40}
CONTROL_SHARE = 0.25
TILES = ("30N_070E", "30N_080E", "30N_090E", "20N_070E", "20N_080E", "20N_090E")


def block_key(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / BLOCK), math.floor(lon / BLOCK)


def inside_block(lat: float, lon: float) -> bool:
    lat_part = lat % BLOCK
    lon_part = lon % BLOCK
    return (
        BLOCK_MARGIN < lat_part < BLOCK - BLOCK_MARGIN
        and BLOCK_MARGIN < lon_part < BLOCK - BLOCK_MARGIN
    )


def assign_blocks(cells: list[dict], seed: int) -> dict[tuple[int, int], str]:
    """Per region, shuffle occupied blocks and give roughly 20 percent to test, 15 to validation."""

    rng = np.random.default_rng(seed)
    assignment: dict[tuple[int, int], str] = {}
    for region in REGIONS:
        blocks = sorted({block_key(c["lat"], c["lon"]) for c in cells if c["region"] == region})
        blocks = [blocks[int(i)] for i in rng.permutation(len(blocks))]
        if not blocks:
            continue
        test_count = max(1, round(0.2 * len(blocks)))
        val_count = max(1, round(0.15 * len(blocks))) if len(blocks) > 2 else 0
        for index, block in enumerate(blocks):
            if index < test_count:
                assignment[block] = "test"
            elif index < test_count + val_count:
                assignment[block] = "val"
            else:
                assignment[block] = "train"
    return assignment


def _region_of(lat: float, lon: float) -> str | None:
    for name, (west, south, east, north) in REGIONS.items():
        if west <= lon < east and south <= lat < north:
            return name
    return None


def scan_tile(tile: str) -> list[dict]:
    """Stream a Hansen tile as coarse cells, returning cells with loss or that suit as controls."""

    import rasterio
    from rasterio.windows import Window

    loss_path = RAW_DIR / f"Hansen_{VERSION}_lossyear_{tile}.tif"
    cover_path = RAW_DIR / f"Hansen_{VERSION}_treecover2000_{tile}.tif"
    cells: list[dict] = []
    with rasterio.open(loss_path) as loss_file, rasterio.open(cover_path) as cover_file:
        transform = loss_file.transform
        size = loss_file.width
        codes = [y - 2000 for y in YEARS]
        for row in range(0, size - CELL + 1, CELL):
            south = transform.f + (row + CELL) * transform.e
            north = transform.f + row * transform.e
            if not any(north > box[1] and south < box[3] for box in REGIONS.values()):
                continue
            window = Window(0, row, size - size % CELL, CELL)
            loss = loss_file.read(1, window=window)
            cover = cover_file.read(1, window=window)
            columns = loss.shape[1] // CELL
            loss = loss.reshape(CELL, columns, CELL)
            cover_mean = cover.reshape(CELL, columns, CELL).mean(axis=(0, 2))
            any_loss = (loss > 0).sum(axis=(0, 2))
            counts = {code: (loss == code).sum(axis=(0, 2)) for code in codes}
            for column in range(columns):
                if cover_mean[column] < FOREST_COVER:
                    continue
                lon = transform.c + (column + 0.5) * CELL * transform.a
                lat = transform.f + (row + 0.5 * CELL) * transform.e
                region = _region_of(lat, lon)
                if region is None:
                    continue
                by_year = {y: int(counts[y - 2000][column]) for y in YEARS}
                best_year = max(by_year, key=by_year.get)
                if by_year[best_year] >= MIN_LOSS_PIXELS:
                    # Each qualifying year is one candidate, so multi-year cells can recur.
                    for year, pixels in by_year.items():
                        if pixels >= MIN_LOSS_PIXELS:
                            cells.append(
                                {"lat": float(lat), "lon": float(lon), "region": region, "year": year, "loss_pixels": pixels, "kind": "loss"}
                            )
                elif any_loss[column] == 0 and cover_mean[column] >= 60:
                    cells.append(
                        {"lat": float(lat), "lon": float(lon), "region": region, "year": 0, "loss_pixels": 0, "kind": "control"}
                    )
    return cells


def plan(args: argparse.Namespace) -> list[dict]:
    import shapely
    from shapely.geometry import shape
    from shapely.ops import unary_union

    rng = np.random.default_rng(args.seed)
    data = json.loads(BOUNDARIES.read_text(encoding="utf-8"))
    india = unary_union(
        [shape(f["geometry"]).buffer(0) for f in data["features"] if f["properties"].get("admin") == "India"]
    )
    cells: list[dict] = []
    for tile in TILES:
        found = scan_tile(tile)
        print(f"{tile}: {len(found)} cells", flush=True)
        cells.extend(found)
    lats = np.array([c["lat"] for c in cells])
    lons = np.array([c["lon"] for c in cells])
    keep = shapely.contains_xy(india, lons, lats)
    cells = [c for c, k in zip(cells, keep, strict=True) if k and inside_block(c["lat"], c["lon"])]
    blocks = assign_blocks(cells, args.seed)
    for cell in cells:
        cell["split"] = blocks[block_key(cell["lat"], cell["lon"])]
    planned: list[dict] = []
    for region, quota in QUOTAS.items():
        losses = [c for c in cells if c["region"] == region and c["kind"] == "loss"]
        controls = [c for c in cells if c["region"] == region and c["kind"] == "control"]
        print(f"{region}: {len(losses)} loss candidates, {len(controls)} control candidates", flush=True)
        # Spread loss windows evenly across years, then over-plan since some scenes will fail.
        per_year = max(1, int(2.5 * quota / len(YEARS)))
        for year in YEARS:
            year_cells = [c for c in losses if c["year"] == year]
            if year_cells:
                picks = rng.choice(len(year_cells), min(per_year, len(year_cells)), replace=False)
                planned.extend(year_cells[int(i)] for i in picks)
        count = int(2.5 * quota * CONTROL_SHARE)
        if controls:
            picks = rng.choice(len(controls), min(count, len(controls)), replace=False)
            for index in picks:
                control = dict(controls[int(index)])
                control["year"] = int(rng.choice(YEARS))
                planned.append(control)
    order = rng.permutation(len(planned))
    output = []
    for number, index in enumerate(order):
        entry = dict(planned[int(index)])
        entry["id"] = f"f{number:05d}"
        output.append(entry)
    return output


def _labels(lat, lon, year, crs, transform) -> tuple[np.ndarray, dict]:
    """Hansen layers warped by nearest neighbour onto the 10 m grid, then mapped to labels."""

    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    top = math.ceil(lat / 10) * 10
    left = math.floor(lon / 10) * 10
    tile = f"{top}N_{left:03d}E"
    layers = {}
    for name in ("lossyear", "treecover2000"):
        with rasterio.open(RAW_DIR / f"Hansen_{VERSION}_{name}_{tile}.tif") as source:
            with WarpedVRT(source, crs=crs, transform=transform, width=CHIP, height=CHIP, resampling=Resampling.nearest) as warped:
                layers[name] = warped.read(1)
    code = year - 2000
    loss, cover = layers["lossyear"], layers["treecover2000"]
    labels = np.full((CHIP, CHIP), IGNORE, dtype=np.uint8)
    labels[(cover >= FOREST_COVER) & (loss == 0)] = 0
    labels[loss == code] = 1
    adjacent = (loss == code - 1) | (loss == code + 1)
    return labels, {"adjacent": adjacent, "forest_no_loss": labels == 0, "loss_y": labels == 1}


def _pick_scene(items: list[dict], lat: float, lon: float, tile: str | None):
    """Yield candidates with known radiometry, cleanest first, optionally one MGRS tile only."""

    usable = [i for i in items if reflectance_offset(i["properties"]) is not None]
    if tile is not None:
        usable = [i for i in usable if i["properties"].get("grid:code") == tile]
    return sorted(usable, key=lambda i: i["properties"]["eo:cloud_cover"])


def _grid(item: dict, lat: float, lon: float):
    from rasterio.crs import CRS
    from rasterio.transform import Affine
    from rasterio.warp import transform as warp_transform

    crs = CRS.from_epsg(int(item["properties"]["proj:epsg"]))
    xs, ys = warp_transform("EPSG:4326", crs, [lon], [lat])
    return crs, Affine(10.0, 0.0, round((xs[0] - CHIP * 5) / 10) * 10, 0.0, -10.0, round((ys[0] + CHIP * 5) / 10) * 10)


def build_pair(candidate: dict, out_dir: Path) -> dict | None:
    lat, lon, year = candidate["lat"], candidate["lon"], candidate["year"]
    half = 0.015
    bbox = (lon - half, lat - half, lon + half, lat + half)
    before_items = search(bbox, f"{year - 1}-12-01", f"{year}-02-28", max_cloud=40.0, limit=40)
    after_items = search(bbox, f"{year + 1}-01-01", f"{year + 1}-03-15", max_cloud=40.0, limit=40)
    # Try tiles in order of how clean their scenes are; both dates must share one MGRS tile.
    for before_item in _pick_scene(before_items, lat, lon, None)[:3]:
        tile = before_item["properties"].get("grid:code")
        crs, transform = _grid(before_item, lat, lon)
        before = read_window(before_item, transform, crs, CHIP, CHIP)
        if before is None or (1 - before[1].mean()) >= MAX_INVALID:
            continue
        for after_item in _pick_scene(after_items, lat, lon, tile)[:3]:
            after = read_window(after_item, transform, crs, CHIP, CHIP)
            if after is None or (1 - after[1].mean()) >= MAX_INVALID:
                continue
            valid = before[1] & after[1]
            labels, parts = _labels(lat, lon, year, crs, transform)
            labels[~valid] = IGNORE
            if (labels == 0).sum() + (labels == 1).sum() < 0.2 * labels.size:
                return None
            np.savez_compressed(out_dir / f"{candidate['id']}.npz", before=before[0], after=after[0], mask=labels)
            return {
                **candidate,
                "before_scene": before_item["id"],
                "after_scene": after_item["id"],
                "before_date": before_item["properties"]["datetime"][:10],
                "after_date": after_item["properties"]["datetime"][:10],
                "before_baseline": before_item["properties"].get("s2:processing_baseline"),
                "after_baseline": after_item["properties"].get("s2:processing_baseline"),
                "positive_pixels": int((labels == 1).sum()),
                "negative_pixels": int((labels == 0).sum()),
                "evidence": _spectral_evidence(before[0], after[0], valid, parts),
            }
    return None


def _nbr(stack: np.ndarray) -> np.ndarray:
    # Normalised burn ratio from B08 and B12; a sharp drop marks loss of live vegetation.
    nir, swir = stack[3].astype(np.float32), stack[5].astype(np.float32)
    return (nir - swir) / np.maximum(nir + swir, 1.0)


def _spectral_evidence(before, after, valid, parts) -> dict:
    """Mean NBR change per Hansen label group: the basis for ignoring adjacent-year loss."""

    change = _nbr(after) - _nbr(before)
    out = {}
    for name, mask in parts.items():
        selected = mask & valid
        out[name] = {"pixels": int(selected.sum()), "nbr_sum": float(change[selected].sum()) if selected.any() else 0.0}
    return out


def run(args: argparse.Namespace) -> None:
    out_dir = args.cache_dir / "chips"
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_path = args.cache_dir / "plan.json"
    if plan_path.is_file():
        planned = json.loads(plan_path.read_text(encoding="utf-8"))
    else:
        planned = plan(args)
        plan_path.write_text(json.dumps(planned), encoding="utf-8")
    manifest_path = args.cache_dir / "manifest.jsonl"
    tried_path = args.cache_dir / "tried.txt"
    done = set(tried_path.read_text().split()) if tried_path.is_file() else set()
    pending = [c for c in planned if c["id"] not in done]
    print(f"{len(planned)} planned, {len(pending)} pending", flush=True)
    lock = threading.Lock()
    deadline = perf_counter() + args.max_minutes * 60
    started = perf_counter()
    stats = {"ok": 0, "skipped": 0, "error": 0}

    def work(candidate: dict) -> None:
        if perf_counter() > deadline:
            return
        try:
            pair = build_pair(candidate, out_dir)
        except Exception as error:  # noqa: BLE001 - one failed window must not stop the build
            with lock:
                stats["error"] += 1
                if stats["error"] <= 20:
                    print(f"{candidate['id']} failed: {type(error).__name__}: {str(error)[:160]}", flush=True)
            return
        with lock:
            with tried_path.open("a") as tried:
                tried.write(candidate["id"] + "\n")
            if pair is None:
                stats["skipped"] += 1
                return
            with manifest_path.open("a") as manifest:
                manifest.write(json.dumps(pair) + "\n")
            stats["ok"] += 1
            if stats["ok"] % 25 == 0:
                print(f"{stats} {(perf_counter() - started) / 60:.1f} min", flush=True)

    with ThreadPoolExecutor(args.workers) as pool:
        for _ in pool.map(work, pending):
            pass
    print(f"Finished: {stats}", flush=True)
    summarise(args.cache_dir)


def summarise(cache_dir: Path = CACHE_DIR) -> dict:
    entries = load_manifest(cache_dir)
    totals: dict[str, dict[str, float]] = {}
    summary: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "hansen_version": VERSION,
        "licence": LICENCE,
        "pairs": len(entries),
        "by_split": {},
    }
    for entry in entries:
        group = summary["by_split"].setdefault(
            entry["split"], {"pairs": 0, "loss_pairs": 0, "control_pairs": 0, "positive_pixels": 0, "negative_pixels": 0, "regions": {}}
        )
        group["pairs"] += 1
        group["loss_pairs" if entry["kind"] == "loss" else "control_pairs"] += 1
        group["positive_pixels"] += entry["positive_pixels"]
        group["negative_pixels"] += entry["negative_pixels"]
        group["regions"][entry["region"]] = group["regions"].get(entry["region"], 0) + 1
        for name, values in entry["evidence"].items():
            bucket = totals.setdefault(name, {"pixels": 0, "nbr_sum": 0.0})
            bucket["pixels"] += values["pixels"]
            bucket["nbr_sum"] += values["nbr_sum"]
    summary["spectral_evidence"] = {
        name: {"pixels": v["pixels"], "mean_nbr_change": v["nbr_sum"] / max(v["pixels"], 1)}
        for name, v in totals.items()
    }
    (cache_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return summary


def load_manifest(cache_dir: Path = CACHE_DIR) -> list[dict]:
    lines = (cache_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


def load_records(split: str, cache_dir: Path = CACHE_DIR) -> list[ChangeRecord]:
    records = []
    for entry in load_manifest(cache_dir):
        if entry["split"] != split:
            continue
        with np.load(cache_dir / "chips" / f"{entry['id']}.npz", allow_pickle=False) as data:
            records.append(
                ChangeRecord(entry["id"], "india_forest", data["before"], data["after"], data["mask"])
            )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument("--max-minutes", type=float, default=35.0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--seed", type=int, default=2024)
    args = parser.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    run(args)


if __name__ == "__main__":
    main()
