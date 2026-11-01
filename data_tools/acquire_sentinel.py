"""Search real Sentinel-2 L2A metadata in Copernicus Data Space Ecosystem.

This is a transparent discovery helper.  It writes actual CDSE STAC metadata but
does not pretend to download imagery without the OAuth credentials required by the
CDSE Process API.  Use the AOI dashboard or monitoring service for the cropped
multi-band retrieval/preprocessing workflow.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.satellite.copernicus import CopernicusDataSpaceProvider
from app.satellite.geometry import bbox_polygon


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search official Copernicus Data Space Sentinel-2 L2A STAC metadata"
    )
    parser.add_argument("--bbox", required=True, help="west,south,east,north in WGS84")
    parser.add_argument(
        "--datetime", required=True, help="ISO interval, e.g. 2025-01-01/2025-03-31"
    )
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--max-cloud", type=float, default=20.0)
    parser.add_argument("--output", type=Path, default=Path("data/raw/copernicus-search"))
    return parser.parse_args()


def parse_bbox(value: str) -> list[float]:
    try:
        bbox = [float(item.strip()) for item in value.split(",")]
    except ValueError as error:
        raise ValueError("--bbox must contain four numeric comma-separated values") from error
    if len(bbox) != 4:
        raise ValueError("--bbox must contain west,south,east,north")
    west, south, east, north = bbox
    if west >= east or south >= north or not (-180 <= west <= 180 and -180 <= east <= 180):
        raise ValueError("--bbox must be an increasing WGS84 longitude extent")
    if south >= north or not (-90 <= south <= 90 and -90 <= north <= 90):
        raise ValueError("--bbox must be an increasing WGS84 latitude extent")
    return bbox


def main() -> None:
    args = parse_args()
    if not 0 <= args.max_cloud <= 100:
        raise ValueError("--max-cloud must be within 0 to 100")
    bbox = parse_bbox(args.bbox)
    provider = CopernicusDataSpaceProvider()
    candidates = provider.search_acquisitions(
        bbox_polygon(bbox), args.datetime, args.max_cloud, limit=args.limit
    )
    payload = {
        "provider": provider.name,
        "catalogue_endpoint": provider.stac_url,
        "collection": provider.collection,
        "bbox": bbox,
        "datetime": args.datetime,
        "max_cloud_cover": args.max_cloud,
        "items": [
            {
                "product_id": item.product_id,
                "acquisition_datetime": item.acquisition_datetime.isoformat() + "Z",
                "cloud_cover": item.cloud_cover,
                "satellite": item.satellite,
                "sensor": item.sensor,
                "processing_level": item.processing_level,
                "source_url": item.source_url,
                "geometry": item.geometry,
                "bbox": item.bbox,
                "assets": item.assets,
            }
            for item in candidates
        ],
        "notice": (
            "These are real discovery records. AOI-cropped bands require CDSE OAuth credentials "
            "and are retrieved through the SentinelWatch monitoring Process API path."
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    destination = args.output / "search.json"
    destination.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"Found {len(candidates)} real Sentinel-2 L2A item(s). Metadata: {destination}")
    if not provider.has_credentials:
        print("No imagery download attempted: configure CDSE OAuth credentials in .env for Process API access.")


if __name__ == "__main__":
    main()
