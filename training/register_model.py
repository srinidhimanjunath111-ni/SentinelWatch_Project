"""Register an evaluated checkpoint in SentinelWatch's model-version table.

Registration is intentionally an explicit human action after a held-out test run.
It prevents a local checkpoint, a training-loss value, or a hypothetical metric
from silently appearing as an operationally validated model in the dashboard.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import settings
from app.database import SessionLocal
from app.migrations import migrate_schema
from app.models import ModelVersion
from app.services.system import current_model_state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register an evaluated SentinelWatch checkpoint")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--evaluation-report", type=Path, required=True)
    parser.add_argument("--dataset", required=True, help="Dataset/version label recorded in the thesis")
    parser.add_argument("--name", default=settings.model_version_name)
    parser.add_argument("--notes", default="")
    return parser.parse_args()


def _load_report(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Evaluation report does not exist: {path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("Evaluation report is not valid JSON.") from error
    if report.get("status") != "COMPLETED" or report.get("split") != "test":
        raise ValueError("Only a completed held-out test report can be registered.")
    if not isinstance(report.get("metrics"), dict) or not report["metrics"]:
        raise ValueError("Evaluation report has no measured metrics.")
    return report


def main() -> None:
    args = parse_args()
    if not args.checkpoint.is_file():
        raise FileNotFoundError(f"Checkpoint does not exist: {args.checkpoint}")
    report = _load_report(args.evaluation_report)
    report_checkpoint = Path(str(report.get("checkpoint", ""))).resolve()
    if report_checkpoint != args.checkpoint.resolve():
        raise ValueError("The evaluation report refers to a different checkpoint.")
    # The operational service reads MODEL_PATH. Make an accidental mismatch visible
    # instead of registering a report for one file while the API loads another.
    if args.checkpoint.resolve() != settings.model_path.resolve():
        raise ValueError(
            "Checkpoint must match MODEL_PATH before registration. Update .env MODEL_PATH, "
            "restart the app, then repeat this command."
        )
    state, _, message = current_model_state()
    if state != "READY":
        raise RuntimeError(f"The configured checkpoint cannot be loaded: {message}")
    migrate_schema()
    db = SessionLocal()
    try:
        version = db.query(ModelVersion).filter(ModelVersion.name == args.name).first()
        if version is None:
            version = ModelVersion(
                name=args.name,
                architecture=str(report.get("architecture", "Siamese U-Net shared encoder")),
                checkpoint_path=str(args.checkpoint),
                status="READY",
            )
            db.add(version)
        version.architecture = str(report.get("architecture", version.architecture))
        version.checkpoint_path = str(args.checkpoint)
        version.status = "READY"
        version.training_dataset = args.dataset
        version.metrics_json = json.dumps(
            {
                "evaluation_report": str(args.evaluation_report),
                "checkpoint_sha256": report.get("checkpoint_sha256"),
                "split": "test",
                **report["metrics"],
            },
            sort_keys=True,
        )
        version.notes = (args.notes.strip() + "\n" if args.notes.strip() else "") + (
            "Registered from a completed held-out evaluation report; metrics are binary "
            "change-segmentation measurements, not illegal-activity accuracy."
        )
        db.commit()
        print(f"Registered {version.name} with measured test metrics from {args.evaluation_report}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
