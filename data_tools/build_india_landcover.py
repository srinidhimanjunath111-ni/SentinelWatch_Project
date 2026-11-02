"""Build the India-native L2A land-cover dataset.

    python -m data_tools.build_india_landcover --max-minutes 35

Samples 256 x 256 windows (2.56 km) across India: stratified across states, plus oversampling of
rare classes (built, water, wetland, mine) and the known mining belts. Each window takes one 2021
L2A scene under 20 percent cloud, alternating dry-season and post-monsoon dates so that the model
sees the seasonal range the labels must survive. Labels are ESA WorldCover 2021 v200, with mining
polygons (Maus et al. 2022) burned in as `mine`. Cloud, shadow and no-data pixels become IGNORE.

Splitting is by 3 degree WorldCover tile, so held-out chips are different places. Windows stay at
least `TILE_MARGIN` inside their tile so that chips in neighbouring tiles cannot overlap. Mining
tiles are pinned so that each split has real mines; the other pins cover the main biomes.

Each finished chip is written immediately and a run can resume, so a crash loses little.
"""

from __future__ import annotations

import argparse
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter

import numpy as np

from data_tools.s2_stac import apply_gdal_env, read_window, reflectance_offset, search

apply_gdal_env()

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "processed" / "india_landcover"
BOUNDARIES = ROOT / "data" / "raw" / "naturalearth" / "ne_50m_admin_1.geojson"
MINING_GPKG = ROOT / "data" / "raw" / "mining_maus2022" / "global_mining_polygons_v2.gpkg"
WORLDCOVER = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"

IGNORE = 255
CHIP = 256
TILE_MARGIN = 0.02  # degrees, about 2.2 km: more than half a chip
# WorldCover code -> LANDCOVER_CLASSES index. 70 (snow) and 100 (moss) are not in the class list.
WORLDCOVER_MAP = {10: 0, 20: 1, 30: 1, 40: 2, 50: 3, 60: 4, 80: 5, 90: 6, 95: 6}
MINE_CLASS = 7
SEASONS = {
    "dry": ("2021-01-15", "2021-04-30"),
    "post_monsoon": ("2021-10-05", "2021-12-31"),
}
# (name, west, south, east, north). Known Indian mining belts, oversampled.
MINING_BELTS = (
    ("jharia", 86.1, 23.5, 86.6, 23.9),
    ("korba", 82.4, 22.1, 82.9, 22.6),
    ("bellary", 76.2, 14.8, 77.2, 15.5),
    ("keonjhar", 85.0, 21.3, 85.9, 22.2),
    ("singrauli", 82.4, 23.9, 83.0, 24.4),
)
# Fixed before any chip was downloaded. Everything else is train.
TEST_TILES = {
    "N15E075": "Bellary iron ore, Deccan",
    "N24E081": "Singrauli coal, central India",
    "N24E090": "Meghalaya and Assam, north-east forest and wetland",
    "N27E072": "Thar desert",
    "N09E075": "Kerala, Western Ghats, backwaters",
}
VAL_TILES = {
    "N21E081": "Korba coal, Chhattisgarh",
    "N27E075": "Delhi and Jaipur, cropland and cities",
    "N12E075": "Mysuru and Bengaluru, southern plateau",
    "N21E087": "Odisha and Bengal coast, delta",
    "N30E075": "Himachal and Punjab, Himalayan foothills",
}


def tile_name(lat: float, lon: float) -> str:
    south, west = math.floor(lat / 3) * 3, math.floor(lon / 3) * 3
    ns = "N" if south >= 0 else "S"
    ew = "E" if west >= 0 else "W"
    return f"{ns}{abs(south):02d}{ew}{abs(west):03d}"


def split_of(tile: str) -> str:
    if tile in TEST_TILES:
        return "test"
    if tile in VAL_TILES:
        return "val"
    return "train"


def inside_tile(lat: float, lon: float) -> bool:
    return (
        TILE_MARGIN < lat - math.floor(lat / 3) * 3 < 3 - TILE_MARGIN
        and TILE_MARGIN < lon - math.floor(lon / 3) * 3 < 3 - TILE_MARGIN
    )


def load_states():
    from shapely.geometry import shape

    data = json.loads(BOUNDARIES.read_text(encoding="utf-8"))
    return {
        f["properties"]["name"]: shape(f["geometry"]).buffer(0)
        for f in data["features"]
        if f["properties"].get("admin") == "India"
    }


def load_mines():
    from pyogrio.raw import read

    meta, _, geometry, fields = read(MINING_GPKG, where="ISO3_CODE='IND'")
    import shapely

    polygons = shapely.from_wkb(geometry)
    areas = np.asarray(fields[list(meta["fields"]).index("AREA")], dtype=float)
    return polygons, areas


def random_point_in(polygon, rng: np.random.Generator) -> tuple[float, float]:
    from shapely.geometry import Point

    west, south, east, north = polygon.bounds
    for _ in range(2000):
        lon, lat = rng.uniform(west, east), rng.uniform(south, north)
        if polygon.contains(Point(lon, lat)):
            return float(lat), float(lon)
    point = polygon.representative_point()
    return float(point.y), float(point.x)


def scan_rare_classes(tiles: list[str], india, workers: int) -> dict[int, dict[str, np.ndarray]]:
    """Read each tile's WorldCover overview to find where rare classes occur, returning lat/lon."""

    import rasterio
    import shapely

    def scan(tile: str):
        url = WORLDCOVER.format(tile=tile)
        with rasterio.open(url) as dataset:
            factor = 16
            out = (dataset.height // factor, dataset.width // factor)
            data = dataset.read(1, out_shape=out)
            transform = dataset.transform * dataset.transform.scale(
                dataset.width / out[1], dataset.height / out[0]
            )
        found = {}
        for code in (50, 80, 90, 95):
            rows, cols = np.nonzero(data == code)
            if rows.size == 0:
                continue
            lons = transform.c + (cols + 0.5) * transform.a
            lats = transform.f + (rows + 0.5) * transform.e
            keep = shapely.contains_xy(india, lons, lats)
            if keep.any():
                found[code] = np.stack((lats[keep], lons[keep]), axis=1).astype(np.float32)
        return tile, found

    result: dict[int, dict[str, np.ndarray]] = {50: {}, 80: {}, 90: {}, 95: {}}
    with ThreadPoolExecutor(workers) as pool:
        for future in as_completed([pool.submit(scan, tile) for tile in tiles]):
            try:
                tile, found = future.result()
            except Exception as error:  # noqa: BLE001 - a missing tile only costs candidates
                print(f"scan failed: {error}", flush=True)
                continue
            for code, points in found.items():
                result[code][tile] = points
    return result


def plan_candidates(args: argparse.Namespace) -> list[dict]:
    """Build the shuffled list of window centres, far more than we expect to use per stratum."""

    from shapely.ops import unary_union

    rng = np.random.default_rng(args.seed)
    states = load_states()
    india = unary_union(list(states.values()))
    polygons, areas = load_mines()
    candidates: list[dict] = []
    # State stratum: allocation proportional to sqrt(area) so small states are still represented.
    weights = {name: math.sqrt(geom.area) for name, geom in states.items()}
    total = sum(weights.values())
    for name, geom in states.items():
        count = max(4, round(args.state_windows * weights[name] / total))
        for _ in range(count):
            lat, lon = random_point_in(geom, rng)
            candidates.append({"lat": lat, "lon": lon, "stratum": "state", "state": name})
    # Mining stratum: named belts first, then polygons weighted by sqrt(area) across India.
    import shapely

    centres = shapely.point_on_surface(polygons)
    xs, ys = shapely.get_x(centres), shapely.get_y(centres)
    in_any_belt = np.zeros(len(polygons), dtype=bool)
    for name, west, south, east, north in MINING_BELTS:
        inside = (xs >= west) & (xs <= east) & (ys >= south) & (ys <= north)
        in_any_belt |= inside
        indices = np.nonzero(inside)[0]
        print(f"belt {name}: {indices.size} polygons", flush=True)
        if indices.size:
            for index in rng.choice(indices, args.belt_windows):
                candidates.append(_mine_candidate(ys[index], xs[index], rng, f"mine_{name}"))
    others = np.nonzero(~in_any_belt)[0]
    probability = np.sqrt(areas[others])
    probability = probability / probability.sum()
    for index in rng.choice(others, args.mine_windows, p=probability):
        candidates.append(_mine_candidate(ys[index], xs[index], rng, "mine_other"))
    # Rare-class stratum: pick a tile first so one megacity tile cannot take every window.
    tiles = sorted(
        {
            tile_name(lat + 0.5, lon + 0.5)
            for lat in range(6, 38)
            for lon in range(66, 98)
            if india.intersects(_box(lon, lat, lon + 1, lat + 1))
        }
    )
    print(f"{len(tiles)} tiles intersect India", flush=True)
    rare = scan_rare_classes(tiles, india, args.workers)
    for code, stratum, count in (
        (50, "built", args.rare_windows),
        (80, "water", args.rare_windows),
        (90, "wetland", args.rare_windows // 2),
        (95, "wetland", args.rare_windows // 2),
    ):
        names = sorted(rare[code])
        if not names:
            continue
        for _ in range(count):
            points = rare[code][names[int(rng.integers(len(names)))]]
            lat, lon = points[int(rng.integers(len(points)))]
            candidates.append({"lat": float(lat), "lon": float(lon), "stratum": stratum, "state": ""})
    order = rng.permutation(len(candidates))
    planned = [candidates[int(index)] for index in order]
    for number, candidate in enumerate(planned):
        candidate["id"] = f"c{number:05d}"
        candidate["season"] = "dry" if number % 2 == 0 else "post_monsoon"
        candidate["tile"] = tile_name(candidate["lat"], candidate["lon"])
        candidate["split"] = split_of(candidate["tile"])
    return [c for c in planned if inside_tile(c["lat"], c["lon"])]


def _box(west, south, east, north):
    from shapely.geometry import box

    return box(west, south, east, north)


def _mine_candidate(lat: float, lon: float, rng: np.random.Generator, stratum: str) -> dict:
    # Jitter of up to about 1 km so the mine is not always dead centre in the chip.
    return {
        "lat": float(lat + rng.uniform(-0.009, 0.009)),
        "lon": float(lon + rng.uniform(-0.009, 0.009)),
        "stratum": stratum,
        "state": "",
    }


def _grid_for(item: dict, lat: float, lon: float):
    """256 x 256 grid centred on the point, snapped to the item's 10 m UTM pixel grid."""

    from rasterio.crs import CRS
    from rasterio.transform import Affine
    from rasterio.warp import transform as warp_transform

    crs = CRS.from_epsg(int(item["properties"]["proj:epsg"]))
    xs, ys = warp_transform("EPSG:4326", crs, [lon], [lat])
    left = round((xs[0] - CHIP * 5) / 10) * 10
    top = round((ys[0] + CHIP * 5) / 10) * 10
    return crs, Affine(10.0, 0.0, left, 0.0, -10.0, top)


def _rasterise_mines(tree, polygons, crs, transform) -> np.ndarray:
    import shapely
    from pyproj import Transformer
    from rasterio.features import rasterize
    from rasterio.warp import transform_bounds

    left, top = transform.c, transform.f
    right, bottom = left + 10.0 * CHIP, top - 10.0 * CHIP
    query = shapely.box(*transform_bounds(crs, "EPSG:4326", left, bottom, right, top))
    hits = tree.query(query)
    if len(hits) == 0:
        return np.zeros((CHIP, CHIP), dtype=bool)
    project = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    def to_grid(coordinates: np.ndarray) -> np.ndarray:
        return np.column_stack(project.transform(coordinates[:, 0], coordinates[:, 1]))

    shapes = [(shapely.transform(polygons[int(i)], to_grid), 1) for i in hits]
    mask = rasterize(shapes, out_shape=(CHIP, CHIP), transform=transform, fill=0, dtype="uint8")
    return mask.astype(bool)


def _worldcover_labels(tile: str, crs, transform) -> np.ndarray:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    with rasterio.open(WORLDCOVER.format(tile=tile)) as source:
        with WarpedVRT(
            source,
            crs=crs,
            transform=transform,
            width=CHIP,
            height=CHIP,
            resampling=Resampling.nearest,
        ) as warped:
            codes = warped.read(1)
    labels = np.full(codes.shape, IGNORE, dtype=np.uint8)
    for code, index in WORLDCOVER_MAP.items():
        labels[codes == code] = index
    return labels


def build_chip(candidate: dict, tree, polygons, out_dir: Path, rng_seed: int, min_valid: float) -> dict | None:
    from rasterio.warp import transform_bounds  # noqa: F401 - keeps rasterio import in this thread

    half = 0.015
    bbox = (
        candidate["lon"] - half,
        candidate["lat"] - half,
        candidate["lon"] + half,
        candidate["lat"] + half,
    )
    start, end = SEASONS[candidate["season"]]
    items = search(bbox, start, end, max_cloud=20.0, limit=40)
    items = [i for i in items if reflectance_offset(i["properties"]) is not None]
    if not items:
        return None
    rng = np.random.default_rng(rng_seed)
    # Cleanest scenes first, shuffled slightly so one date does not dominate a region.
    items.sort(key=lambda i: i["properties"]["eo:cloud_cover"] + rng.uniform(0, 5))
    for item in items[:4]:
        crs, transform = _grid_for(item, candidate["lat"], candidate["lon"])
        result = read_window(item, transform, crs, CHIP, CHIP)
        if result is None:
            continue
        stack, valid = result
        if valid.mean() < min_valid:
            continue
        labels = _worldcover_labels(candidate["tile"], crs, transform)
        mines = _rasterise_mines(tree, polygons, crs, transform)
        labels[mines] = MINE_CLASS
        labels[~valid] = IGNORE
        if (labels != IGNORE).mean() < min_valid:
            return None
        np.savez_compressed(out_dir / f"{candidate['id']}.npz", image=stack, label=labels)
        counts = np.bincount(labels[labels != IGNORE], minlength=8)
        return {
            **candidate,
            "scene": item["id"],
            "date": item["properties"]["datetime"][:10],
            "baseline": item["properties"].get("s2:processing_baseline"),
            "valid_fraction": float(valid.mean()),
            "class_pixels": counts.tolist(),
        }
    return None


def run(args: argparse.Namespace) -> None:
    import shapely

    out_dir = args.cache_dir / "chips"
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_path = args.cache_dir / "plan.json"
    if plan_path.is_file():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    else:
        plan = plan_candidates(args)
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
    manifest_path = args.cache_dir / "manifest.jsonl"
    done_ids = set()
    if manifest_path.is_file():
        done_ids = {json.loads(line)["id"] for line in manifest_path.read_text().splitlines() if line}
    tried_path = args.cache_dir / "tried.txt"
    if tried_path.is_file():
        done_ids |= set(tried_path.read_text().split())
    polygons, _ = load_mines()
    tree = shapely.STRtree(polygons)
    pending = [c for c in plan if c["id"] not in done_ids]
    print(f"{len(plan)} planned, {len(done_ids)} already tried, {len(pending)} pending", flush=True)
    lock = threading.Lock()
    deadline = perf_counter() + args.max_minutes * 60
    stats = {"ok": 0, "skipped": 0, "error": 0}

    def work(candidate: dict) -> None:
        if perf_counter() > deadline:
            return
        try:
            chip = build_chip(candidate, tree, polygons, out_dir, args.seed + int(candidate["id"][1:]), args.min_valid)
        except Exception as error:  # noqa: BLE001 - one failed window must not stop the build
            with lock:
                stats["error"] += 1
                if stats["error"] <= 20:
                    print(f"{candidate['id']} failed: {type(error).__name__}: {str(error)[:160]}", flush=True)
            return
        with lock:
            with tried_path.open("a") as tried:
                tried.write(candidate["id"] + "\n")
            if chip is None:
                stats["skipped"] += 1
                return
            with manifest_path.open("a") as manifest:
                manifest.write(json.dumps(chip) + "\n")
            stats["ok"] += 1
            if stats["ok"] % 25 == 0:
                print(f"{stats} {(perf_counter() - deadline + args.max_minutes * 60) / 60:.1f} min", flush=True)

    with ThreadPoolExecutor(args.workers) as pool:
        for _ in pool.map(work, pending):
            pass
    print(f"Finished: {stats}", flush=True)
    summarise(args.cache_dir)


def load_manifest(cache_dir: Path = CACHE_DIR) -> list[dict]:
    lines = (cache_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


def summarise(cache_dir: Path = CACHE_DIR) -> dict:
    """Counts per split, measured from the manifest, for the committed dataset report."""

    entries = load_manifest(cache_dir)
    names = ("tree", "shrub_grass", "crop", "built", "bare", "water", "wetland", "mine")
    summary: dict = {"chips": len(entries), "by_split": {}, "test_tiles": TEST_TILES, "val_tiles": VAL_TILES}
    for split in ("train", "val", "test"):
        group = [e for e in entries if e["split"] == split]
        counts = np.sum([e["class_pixels"] for e in group], axis=0)
        summary["by_split"][split] = {
            "chips": len(group),
            "tiles": len({e["tile"] for e in group}),
            "class_pixels": dict(zip(names, counts.tolist(), strict=True)),
        }
    months: dict[str, int] = {}
    for entry in entries:
        months[entry["date"][:7]] = months.get(entry["date"][:7], 0) + 1
    summary["scene_months"] = dict(sorted(months.items()))
    (cache_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def load_chips(split: str, cache_dir: Path = CACHE_DIR) -> list[dict]:
    """Load one split into memory as dicts of image, label, name, tile, class_pixels."""

    chips = []
    for entry in load_manifest(cache_dir):
        if entry["split"] != split:
            continue
        with np.load(cache_dir / "chips" / f"{entry['id']}.npz", allow_pickle=False) as data:
            chips.append(
                {
                    "image": data["image"],
                    "label": data["label"],
                    "name": entry["id"],
                    "tile": entry["tile"],
                    "stratum": entry["stratum"],
                    "class_pixels": entry["class_pixels"],
                }
            )
    return chips


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument("--state-windows", type=int, default=330)
    parser.add_argument("--belt-windows", type=int, default=24)
    parser.add_argument("--mine-windows", type=int, default=120)
    parser.add_argument("--rare-windows", type=int, default=90)
    parser.add_argument("--max-minutes", type=float, default=35.0)
    parser.add_argument("--min-valid", type=float, default=0.7)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--seed", type=int, default=2021)
    args = parser.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    run(args)


if __name__ == "__main__":
    main()
