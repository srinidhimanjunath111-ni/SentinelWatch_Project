"""Build models/registry.json from measured reports. No metric is written by hand here.

    python -m training.build_registry

Each model entry describes the checkpoint installed on disk. Test numbers are looked up by that
checkpoint's SHA-256 in reports/ml/*.json, so a report for any other checkpoint cannot be attached
to it. A checkpoint without a test report is listed with status UNEVALUATED rather than READY.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import torch

from app.ml.landcover_unet import LandCoverUNet
from app.ml.siamese_unet import SiameseUNet
from data_tools.fetch_oscd import LICENCE as OSCD_LICENCE
from data_tools.fetch_oscd import URL as OSCD_URL
from training.evaluate import file_sha256

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINTS = ROOT / "models" / "checkpoints"
REPORTS = ROOT / "reports" / "ml"
BANDS = ["B02", "B03", "B04", "B08", "B11", "B12"]

SOURCE_FACTS = {
    "oscd": {"name": "Onera Satellite Change Detection (OSCD)", "url": OSCD_URL, "licence": OSCD_LICENCE},
    "india_forest": {
        "name": "India forest change: Sentinel-2 L2A pairs labelled from Hansen Global Forest Change",
        "url": "https://glad.earthengine.app/view/global-forest-change",
        "licence": "Labels CC BY 4.0 (Hansen/UMD/Google/USGS/NASA); imagery Copernicus Sentinel-2 open data",
    },
}
LANDCOVER_SOURCE = {
    "name": "India land cover: Sentinel-2 L2A 2021, ESA WorldCover 2021 v200, Maus et al. 2022 mining polygons",
    "url": "https://esa-worldcover.org/ ; https://doi.org/10.1594/PANGAEA.942325",
    "licence": "WorldCover CC BY 4.0; mining polygons CC BY-SA 4.0; imagery Copernicus Sentinel-2 open data",
}
CHANGE_LIMITATIONS = [
    "OSCD is urban and mostly outside India (one Indian city, Mumbai), so urban results do not transfer automatically to Indian forest, mining or rural change.",
    "OSCD imagery is Sentinel-2 L1C while live imagery is L2A. Per-pair per-band normalisation reduces this shift but does not remove it, and no OSCD-style pixel labels exist on L2A to measure the residual.",
    "OSCD has 24 pairs. The test set is 10 cities and validation is only 2, so metrics and the tuned threshold carry real sampling uncertainty.",
    "The model reports physical change only. It cannot determine cause or legality.",
]
FOREST_LIMITATIONS = [
    "India forest labels come from Hansen Global Forest Change at 30 m and are annual, so they are coarse relative to 10 m pixels, and the loss edge is uncertain by several pixels.",
    "Hansen records loss of tree cover only. Pixels outside forest (treecover2000 under 30) are ignored, so India forest metrics say nothing about false alarms on non-forest land.",
]
LANDCOVER_LIMITATIONS = [
    "Reference labels are ESA WorldCover 2021, itself a model output with its own errors (around 75 percent reported global accuracy), so scores measure agreement with WorldCover rather than ground truth.",
    "The mine class comes from Maus et al. 2022 polygons digitised on a 2019 mosaic. They include tailings, ponds and plant, and omit mines opened or unmapped since, so unmapped mines are labelled as other classes during training and scoring.",
    "A mine class from polygons cannot determine legality or current activity. It marks a mining-related footprint.",
    "Trained on 2021 India L2A scenes from dry and post-monsoon dates. Monsoon-season scenes and other years are outside the training distribution.",
    "Held-out tiles are different places, but only a few mining belts exist, so mine metrics rest on few sites.",
]


def _reports() -> list[dict]:
    reports = []
    for path in sorted(REPORTS.glob("*.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(report, dict):
            report["_path"] = path
            reports.append(report)
    return reports


def _relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _artifact(path: Path) -> str | None:
    return _relative(path) if path.is_file() else None


def _parameters(model: torch.nn.Module) -> int:
    return int(sum(parameter.numel() for parameter in model.parameters()))


def _binary_summary(metrics: dict) -> dict:
    return {key: metrics[key] for key in ("f1", "iou", "precision", "recall", "accuracy")}


def change_entry(reports: list[dict]) -> dict | None:
    checkpoint = CHECKPOINTS / "change_siamese.pt"
    if not checkpoint.is_file():
        return None
    sha = file_sha256(checkpoint)
    payload = torch.load(checkpoint, map_location="cpu", weights_only=True)
    version = str(payload["version"])
    tests = [r for r in reports if r.get("key") == "change" and r.get("split") == "test"]
    installed = {r["dataset"]: r for r in tests if r.get("checkpoint_sha256") == sha}
    training = next(
        (r for r in reports if r.get("key") == "change" and "history" in r and r.get("version") == version),
        None,
    )
    selection = next((r for r in reports if r.get("key") == "change_selection"), None)
    primary = installed.get("oscd_test")
    per_dataset = {name: _binary_summary(r["metrics"]) for name, r in installed.items()}
    # Every candidate on the same test data, so a v1 versus v2 comparison stays visible.
    for report in tests:
        per_dataset.setdefault(f"{report['dataset']}__{report['version']}", _binary_summary(report["metrics"]))
    datasets = []
    if training:
        for source, count in training["train_samples"].items():
            datasets.append({**SOURCE_FACTS[source], "role": "train", "samples": count})
        for source, count in training["val_samples"].items():
            datasets.append({**SOURCE_FACTS[source], "role": "val", "samples": count})
    for name, report in installed.items():
        source = name.removesuffix("_test")
        datasets.append({**SOURCE_FACTS[source], "role": "test", "samples": report["samples"]})
    limitations = list(CHANGE_LIMITATIONS)
    if training and "india_forest" in training["datasets"] or "india_forest_test" in installed:
        limitations += FOREST_LIMITATIONS
    if selection:
        limitations.append(selection["decision"])
    prefix = primary["_path"].with_suffix("") if primary else None
    entry = {
        "key": "change",
        "name": "SentinelWatch change detector",
        "version": version,
        "architecture": f"Siamese U-Net, shared encoder, absolute-difference fusion, base {payload['config']['base_channels']} channels",
        "parameters": _parameters(
            SiameseUNet(
                in_channels=int(payload["config"]["in_channels"]),
                base_channels=int(payload["config"]["base_channels"]),
            )
        ),
        "status": "READY" if primary else "UNEVALUATED",
        "checkpoint": _relative(checkpoint),
        "checkpoint_sha256": sha,
        "checkpoint_bytes": checkpoint.stat().st_size,
        "input_bands": BANDS,
        "normalisation": str(payload["normalisation"]),
        "trained_at": str(payload["trained_at"]),
        "device": str(payload["device"]),
        "epochs": training["epochs_equivalent"] if training else 0,
        "steps": int(payload["step"]),
        "training_seconds": training["training_seconds"] if training else float(payload["training_seconds"]),
        "threshold": float(payload["threshold"]),
        "datasets": datasets,
        "metrics": {
            "split": "test",
            **(_binary_summary(primary["metrics"]) if primary else {}),
            "per_dataset": per_dataset,
        },
        "artifacts": {
            "learning_curve": _artifact(training["_path"].with_name(training["_path"].stem + "_curve.png")) if training else None,
            "confusion_matrix": _artifact(Path(f"{prefix}_confusion.png")) if prefix else None,
            "samples": _artifact(Path(f"{prefix}_samples.png")) if prefix else None,
        },
        "limitations": limitations,
    }
    if selection:
        entry["selection"] = {key: selection[key] for key in ("selected", "criteria", "decision")}
    return entry


def landcover_entry(reports: list[dict]) -> dict | None:
    checkpoint = CHECKPOINTS / "landcover_unet.pt"
    if not checkpoint.is_file():
        return None
    sha = file_sha256(checkpoint)
    payload = torch.load(checkpoint, map_location="cpu", weights_only=True)
    version = str(payload["version"])
    test = next(
        (r for r in reports if r.get("key") == "landcover" and r.get("split") == "test" and r.get("checkpoint_sha256") == sha),
        None,
    )
    training = next((r for r in reports if r.get("key") == "landcover" and "history" in r and r.get("version") == version), None)
    prefix = test["_path"].with_suffix("") if test else None
    datasets = []
    if training:
        datasets.append({**LANDCOVER_SOURCE, "role": "train", "samples": training["train_samples"]})
        datasets.append({**LANDCOVER_SOURCE, "role": "val", "samples": training["val_samples"]})
    if test:
        datasets.append({**LANDCOVER_SOURCE, "role": "test", "samples": test["samples"]})
    metrics: dict = {"split": "test"}
    if test:
        result = test["metrics"]
        metrics.update(
            {
                "f1": result["macro_f1"],
                "iou": result["macro_iou"],
                "accuracy": result["accuracy"],
                "macro_f1": result["macro_f1"],
                "macro_iou": result["macro_iou"],
                "per_class": result["per_class"],
                "per_dataset": {"india_landcover_test": {"f1": result["macro_f1"], "iou": result["macro_iou"], "accuracy": result["accuracy"]}},
            }
        )
    return {
        "key": "landcover",
        "name": "SentinelWatch land-cover classifier",
        "version": version,
        "architecture": f"U-Net, base {payload['config']['base_channels']} channels, 8 classes",
        "parameters": _parameters(
            LandCoverUNet(
                in_channels=int(payload["config"]["in_channels"]),
                classes=len(payload["classes"]),
                base_channels=int(payload["config"]["base_channels"]),
            )
        ),
        "status": "READY" if test else "UNEVALUATED",
        "checkpoint": _relative(checkpoint),
        "checkpoint_sha256": sha,
        "checkpoint_bytes": checkpoint.stat().st_size,
        "input_bands": BANDS,
        "normalisation": "fixed per-band standardisation from the training set",
        "classes": list(payload["classes"]),
        "trained_at": str(payload["trained_at"]),
        "device": str(payload["device"]),
        "epochs": training["epochs"] if training else 0,
        "training_seconds": training["training_seconds"] if training else float(payload["training_seconds"]),
        "threshold": None,
        "datasets": datasets,
        "metrics": metrics,
        "artifacts": {
            "learning_curve": _artifact(training["_path"].with_name(training["_path"].stem + "_curve.png")) if training else None,
            "confusion_matrix": _artifact(Path(f"{prefix}_confusion.png")) if prefix else None,
            "samples": _artifact(Path(f"{prefix}_samples.png")) if prefix else None,
        },
        "limitations": LANDCOVER_LIMITATIONS,
    }


def main() -> None:
    reports = _reports()
    models = [entry for entry in (change_entry(reports), landcover_entry(reports)) if entry]
    registry = {"generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "models": models}
    destination = ROOT / "models" / "registry.json"
    destination.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    for model in models:
        print(model["key"], model["version"], model["status"], {k: v for k, v in model["metrics"].items() if k in ("f1", "iou", "accuracy")})


if __name__ == "__main__":
    main()
