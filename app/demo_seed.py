"""Create presentation-ready, deterministic demonstration cases.

The generated images are intentionally marked as synthetic and are useful only for
testing the interface and pipeline. They must not be used to report model accuracy.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
from PIL import Image, ImageDraw

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import Alert, Case, Finding
from app.services.alerting import create_alerts_for_case
from app.services.analysis import analyse_pair, write_visual_evidence
from app.services.storage import public_path


def _scene(activity: str, seed: int) -> tuple[Image.Image, Image.Image]:
    """Create a textured aerial-style image pair with a controlled intervention."""
    rng = np.random.default_rng(seed)
    height = width = 420
    y, x = np.mgrid[:height, :width]
    texture = rng.normal(0, 12, (height, width, 1))
    gradients = 7 * np.sin(x[:, :, None] / 26) + 4 * np.cos(y[:, :, None] / 31)
    base = np.array([62, 111, 66], dtype=np.float32) + texture + gradients
    # Broad patches make the base look like a landscape rather than a flat colour.
    base[(x < 95) & (y > 235)] = np.array([128, 130, 76]) + texture[(x < 95) & (y > 235)]
    base[(x > 315) & (y < 160)] = np.array([77, 100, 58]) + texture[(x > 315) & (y < 160)]
    before = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    before_draw = ImageDraw.Draw(before)
    before_draw.line([(0, 308), (420, 270)], fill=(138, 129, 108), width=8)
    before_draw.line([(187, 0), (216, 420)], fill=(95, 98, 83), width=5)
    after = before.copy()
    draw = ImageDraw.Draw(after)

    if activity == "deforestation":
        # Dense green canopy before, bare earth + clearing track after.
        canopy = [(119, 88), (278, 75), (326, 142), (302, 254), (188, 274), (100, 196)]
        before_draw.polygon(canopy, fill=(38, 122, 54))
        for cx, cy, radius in [
            (145, 120, 18),
            (183, 101, 15),
            (243, 132, 25),
            (276, 193, 18),
            (164, 210, 25),
        ]:
            before_draw.ellipse(
                (cx - radius, cy - radius, cx + radius, cy + radius), fill=(31, 107, 44)
            )
        after = before.copy()
        draw = ImageDraw.Draw(after)
        draw.polygon(canopy, fill=(154, 116, 65))
        draw.line([(117, 113), (306, 232)], fill=(186, 150, 86), width=15)
        for cx, cy, radius in [(150, 150, 13), (223, 176, 10), (270, 121, 12)]:
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(134, 99, 52))
    elif activity == "mining":
        # An irregular exposed pit, stockpile, and access road.
        pit = [(132, 101), (269, 82), (320, 149), (292, 267), (182, 296), (110, 224)]
        draw.polygon(pit, fill=(159, 116, 57))
        draw.ellipse((156, 120, 290, 262), fill=(116, 83, 48))
        draw.ellipse((177, 141, 268, 239), fill=(82, 74, 58))
        draw.line([(286, 209), (399, 280)], fill=(181, 145, 82), width=14)
        draw.rectangle((310, 161, 357, 205), fill=(185, 147, 80))
    elif activity == "construction":
        # New bright, rectilinear building footprints and connecting access drive.
        draw.rectangle((121, 112, 225, 213), fill=(186, 189, 182))
        draw.rectangle((242, 151, 332, 255), fill=(209, 210, 201))
        draw.rectangle((132, 220, 205, 286), fill=(172, 176, 171))
        draw.rectangle((115, 212, 340, 229), fill=(150, 150, 140))
        draw.line([(337, 225), (400, 263)], fill=(154, 151, 137), width=13)
        draw.line([(126, 216), (220, 287)], fill=(93, 95, 87), width=3)
    else:
        raise ValueError(f"Unsupported demo activity: {activity}")
    return before, after


def seed_demo_cases(refresh: bool = False) -> int:
    settings.ensure_directories()
    Base.metadata.create_all(bind=engine)
    # Keep demonstration source imagery inside the same evidence store used by uploads,
    # so `/files/...` access follows the normal path-confinement rule.
    demo_dir = settings.upload_dir / "demo"
    demo_dir.mkdir(parents=True, exist_ok=True)
    db = SessionLocal()
    created = 0
    cases = [
        (
            "deforestation",
            "Forest buffer — sector 14",
            "Synthetic demo / forest reserve",
            "2024-02-18",
            "2025-01-21",
            4,
        ),
        (
            "mining",
            "Watershed watch — sector 08",
            "Synthetic demo / watershed edge",
            "2024-05-07",
            "2025-02-14",
            5,
        ),
        (
            "construction",
            "Urban fringe — sector 03",
            "Synthetic demo / municipal boundary",
            "2023-10-11",
            "2025-03-26",
            6,
        ),
    ]
    try:
        for activity, title, location, before_date, after_date, seed in cases:
            case = db.query(Case).filter(Case.title == title).first()
            if case and not refresh:
                continue
            before_image, after_image = _scene(activity, seed)
            before_path = demo_dir / f"{activity}-before.png"
            after_path = demo_dir / f"{activity}-after.png"
            before_image.save(before_path)
            after_image.save(after_path)
            result = analyse_pair(before_path, after_path, pixel_resolution_m=2.0)
            mask_path = settings.output_dir / f"demo-{activity}-change-evidence.jpg"
            write_visual_evidence(before_path, after_path, result, mask_path)
            severity_order = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
            highest_severity = max(
                (item.severity for item in result.detections),
                key=severity_order.get,
                default="none",
            )
            values = {
                "title": title,
                "location": location,
                "before_date": before_date,
                "after_date": after_date,
                "source": "Synthetic satellite-textured demonstration (not model validation)",
                "pixel_resolution_m": 2.0,
                "analysis_method": result.method,
                "overall_confidence": result.confidence,
                "changed_area_px": result.changed_area_pixels,
                "changed_area_m2": result.changed_area_m2,
                "highest_severity": highest_severity,
                "before_image": public_path(before_path),
                "after_image": public_path(after_path),
                "mask_image": public_path(mask_path),
                "data_origin": "synthetic_test",
                "requires_human_verification": True,
                "quality_note": "Synthetic developer-only fixture. Not a real satellite acquisition or model evaluation result.",
            }
            if case:
                # Refresh affects only clearly labelled generated demo records.
                db.query(Alert).filter(Alert.case_id == case.id).delete(synchronize_session=False)
                db.query(Finding).filter(Finding.case_id == case.id).delete(
                    synchronize_session=False
                )
                for key, value in values.items():
                    setattr(case, key, value)
            else:
                case = Case(**values)
                db.add(case)
            db.flush()
            for item in result.detections:
                db.add(
                    Finding(
                        case_id=case.id,
                        activity=item.activity,
                        category=item.category,
                        confidence=item.confidence,
                        severity=item.severity,
                        area_pixels=item.area_pixels,
                        area_m2=item.area_m2,
                        centroid_x=item.centroid_x,
                        centroid_y=item.centroid_y,
                        bbox=",".join(map(str, item.bbox)),
                        ndvi_before=item.ndvi_before,
                        ndvi_after=item.ndvi_after,
                        ndvi_delta=item.ndvi_delta,
                        requires_human_verification=True,
                        evidence_json=json.dumps(item.evidence),
                    )
                )
            db.flush()
            create_alerts_for_case(db, case)
            created += 1
        db.commit()
        return created
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed SentinelWatch synthetic demonstration cases")
    parser.add_argument(
        "--refresh", action="store_true", help="Reanalyse only existing generated demo cases"
    )
    options = parser.parse_args()
    count = seed_demo_cases(refresh=options.refresh)
    adjective = "created or refreshed" if options.refresh else "new"
    print(f"SentinelWatch demo seed complete: {count} {adjective} case(s).")
