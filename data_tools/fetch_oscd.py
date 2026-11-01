"""Fetch, safely extract and cache the Onera Satellite Change Detection dataset (OSCD).

Daudt et al., "Urban Change Detection for Multispectral Earth Observation Using Convolutional
Neural Networks", IGARSS 2018. 24 Sentinel-2 L1C pairs with pixel-level urban change maps.
Change maps are CC BY-NC-SA 4.0 (academic use only); imagery is Copernicus Sentinel data.

    python -m data_tools.fetch_oscd              # downloads to data/raw/oscd, caches npz

Downloads are treated as untrusted: nothing from them is imported or executed, zip members are
path-checked before extraction and only the GeoTIFF/text members we need are written. Cached
arrays use uint16 digital numbers (reflectance x 10000) and are stored with allow_pickle=False.

The official split comes from train.txt/test.txt in the archive. Two official *train* cities
are held out for validation (`VALIDATION_CITIES`); the choice was fixed before any training.
"""

from __future__ import annotations

import argparse
import json
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "oscd"
CACHE_DIR = ROOT / "data" / "processed" / "oscd"

BASE_URL = (
    "https://hf.co/datasets/hkristen/oscd/resolve/4958d786c1389ede1511d91a6ecf1a75c4074933/"
    "Onera%20Satellite%20Change%20Detection%20dataset%20-%20"
)
ARCHIVES = {
    "images.zip": BASE_URL + "Images.zip",
    "train_labels.zip": BASE_URL + "Train%20Labels.zip",
    "test_labels.zip": BASE_URL + "Test%20Labels.zip",
}
LICENCE = "Change maps CC BY-NC-SA 4.0 (academic use); imagery Copernicus Sentinel-2 open data"
URL = "https://rcdaudt.github.io/oscd/"

# Contract section 2 band order. imgs_*_rect holds every band already on the 10 m grid.
BANDS = ("B02", "B03", "B04", "B08", "B11", "B12")
# Fixed before training. One humid-Asian coastal city and one temperate-European city, so that
# validation is not all one climate. Mumbai stays in train: it is the only Indian city in OSCD.
VALIDATION_CITIES = ("hongkong", "bordeaux")
IGNORE = 255
# OSCD ships no extraction limit of its own; this bound protects the disk from a zip bomb.
MAX_EXTRACTED_BYTES = 8 * 1024**3


@dataclass
class ChangeRecord:
    """One co-registered pair. before/after are uint16 [6,H,W]; mask uint8 0/1/255 (ignore)."""

    name: str
    source: str
    before: np.ndarray
    after: np.ndarray
    mask: np.ndarray


def _download(url: str, destination: Path) -> None:
    if destination.is_file() and destination.stat().st_size > 0:
        return
    partial = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(response, out, length=1024 * 1024)
    partial.rename(destination)


def _safe_member_path(destination: Path, member: zipfile.ZipInfo) -> Path:
    """Resolve a zip member under destination or raise; rejects traversal and symlinks."""

    name = PurePosixPath(member.filename)
    if name.is_absolute() or ".." in name.parts or "\\" in member.filename:
        raise ValueError(f"Unsafe path in archive: {member.filename!r}")
    # The upper four bits of external_attr hold the Unix file type; 0o12 is a symlink.
    if (member.external_attr >> 28) == 0o12:
        raise ValueError(f"Symlink in archive: {member.filename!r}")
    target = (destination / Path(*name.parts)).resolve()
    if not target.is_relative_to(destination.resolve()):
        raise ValueError(f"Archive member escapes destination: {member.filename!r}")
    return target


def _wanted(filename: str) -> bool:
    parts = filename.split("/")
    if filename.endswith((".txt",)) and len(parts) == 2:
        return parts[1] in {"train.txt", "test.txt"}
    if filename.endswith("-cm.tif"):
        return True
    return (
        len(parts) >= 3
        and parts[-2] in {"imgs_1_rect", "imgs_2_rect"}
        and parts[-1] in {f"{band}.tif" for band in BANDS}
    )


def safe_extract(archive: Path, destination: Path) -> int:
    """Extract only the members the pipeline needs, with traversal and size guards."""

    written = 0
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if member.is_dir() or not _wanted(member.filename):
                continue
            written += member.file_size
            if written > MAX_EXTRACTED_BYTES:
                raise ValueError(f"{archive.name} expands beyond the extraction limit")
            target = _safe_member_path(destination, member)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("wb") as out:
                shutil.copyfileobj(source, out, length=1024 * 1024)
    return written


def _read_stack(folder: Path) -> np.ndarray:
    import rasterio

    layers = []
    for band in BANDS:
        with rasterio.open(folder / f"{band}.tif") as dataset:
            layers.append(dataset.read(1))
    shapes = {layer.shape for layer in layers}
    if len(shapes) != 1:
        raise ValueError(f"Band shapes differ in {folder}: {shapes}")
    return np.stack(layers).astype(np.uint16)


def _read_mask(path: Path) -> np.ndarray:
    import rasterio

    with rasterio.open(path) as dataset:
        raw = dataset.read(1)
    values = set(np.unique(raw).tolist())
    # Change maps encode 1 = no change, 2 = change.
    if not values <= {1, 2}:
        raise ValueError(f"Unexpected change-map values {values} in {path}")
    return (raw == 2).astype(np.uint8)


def prepare(raw_dir: Path = RAW_DIR, cache_dir: Path = CACHE_DIR) -> dict:
    """Download, extract and cache every city, returning the split manifest."""

    raw_dir.mkdir(parents=True, exist_ok=True)
    for filename, url in ARCHIVES.items():
        _download(url, raw_dir / filename)
    extracted = raw_dir / "extracted"
    for filename in ARCHIVES:
        safe_extract(raw_dir / filename, extracted)
    images = extracted / "Onera Satellite Change Detection dataset - Images"
    label_roots = {
        "train": extracted / "Onera Satellite Change Detection dataset - Train Labels",
        "test": extracted / "Onera Satellite Change Detection dataset - Test Labels",
    }
    official = {
        split: [
            city.strip()
            for city in (images / f"{split}.txt").read_text(encoding="utf-8").split(",")
            if city.strip()
        ]
        for split in ("train", "test")
    }
    if set(official["train"]) & set(official["test"]):
        raise ValueError("Official train and test city lists overlap")
    if not set(VALIDATION_CITIES) <= set(official["train"]):
        raise ValueError("Validation cities must come from the official train list")
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "url": URL,
        "licence": LICENCE,
        "bands": list(BANDS),
        "splits": {
            "train": [c for c in official["train"] if c not in VALIDATION_CITIES],
            "val": list(VALIDATION_CITIES),
            "test": official["test"],
        },
        "cities": {},
    }
    for official_split, cities in official.items():
        for city in cities:
            before = _read_stack(images / city / "imgs_1_rect")
            after = _read_stack(images / city / "imgs_2_rect")
            mask = _read_mask(label_roots[official_split] / city / "cm" / f"{city}-cm.tif")
            if before.shape != after.shape or before.shape[1:] != mask.shape:
                raise ValueError(f"{city}: {before.shape} {after.shape} {mask.shape}")
            np.savez_compressed(cache_dir / f"{city}.npz", before=before, after=after, mask=mask)
            manifest["cities"][city] = {
                "height": int(mask.shape[0]),
                "width": int(mask.shape[1]),
                "change_fraction": float(mask.mean()),
            }
    (cache_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def load_manifest(cache_dir: Path = CACHE_DIR) -> dict:
    return json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))


def load_records(split: str, cache_dir: Path = CACHE_DIR) -> list[ChangeRecord]:
    """Load one split ('train', 'val', 'test') from the local cache."""

    manifest = load_manifest(cache_dir)
    records = []
    for city in manifest["splits"][split]:
        with np.load(cache_dir / f"{city}.npz", allow_pickle=False) as data:
            records.append(
                ChangeRecord(city, "oscd", data["before"], data["after"], data["mask"])
            )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    args = parser.parse_args()
    manifest = prepare(args.raw_dir, args.cache_dir)
    for split, cities in manifest["splits"].items():
        pixels = sum(manifest["cities"][c]["height"] * manifest["cities"][c]["width"] for c in cities)
        print(f"{split}: {len(cities)} cities, {pixels / 1e6:.2f} Mpx")
    for city, facts in manifest["cities"].items():
        print(f"  {city}: {facts['height']}x{facts['width']} change={facts['change_fraction']:.4f}")


if __name__ == "__main__":
    main()
