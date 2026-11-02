"""Compare change models on identical test data, record the decision and install the winner.

    python -m training.select_change

The rule was written down before v2's test numbers existed: keep v2 only if its OSCD test F1 does
not fall more than MATERIAL_DROP below v1's, and its India forest test F1 is higher than v1's.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports" / "ml"
INSTALLED = ROOT / "models" / "checkpoints" / "change_siamese.pt"
MATERIAL_DROP = 0.02
CRITERIA = (
    f"Keep v2 only if OSCD test F1 does not fall more than {MATERIAL_DROP} below v1 and India forest "
    "test F1 is higher than v1."
)
DISCLOSURE = (
    "v1's OSCD test result was seen before v2 was designed, so v2 is not a fully blind result. "
    "v2's configuration, threshold and temperature were chosen on validation data only, and v2 was "
    "evaluated once on each test set. This comparison then uses test results to choose between "
    "the two models, which makes the winner's score slightly optimistic."
)


def _report(name: str) -> dict:
    return json.loads((REPORTS / name).read_text(encoding="utf-8"))


def main() -> None:
    v1_oscd = _report("change_v1_oscd_test.json")
    v1_forest = _report("change_v1_india_forest_test.json")
    v2_oscd = _report("change_v2_oscd_test.json")
    v2_forest = _report("change_v2_india_forest_test.json")
    oscd_change = v2_oscd["metrics"]["f1"] - v1_oscd["metrics"]["f1"]
    forest_change = v2_forest["metrics"]["f1"] - v1_forest["metrics"]["f1"]
    keep_v2 = oscd_change >= -MATERIAL_DROP and forest_change > 0
    selected = "v2" if keep_v2 else "v1"
    decision = (
        f"Selected {selected}. Against v1, v2 changed OSCD test F1 by {oscd_change:+.3f} "
        f"({v1_oscd['metrics']['f1']:.3f} to {v2_oscd['metrics']['f1']:.3f}) and India forest test F1 "
        f"by {forest_change:+.3f} ({v1_forest['metrics']['f1']:.3f} to {v2_forest['metrics']['f1']:.3f}). "
        + DISCLOSURE
    )
    summary = {
        "status": "COMPLETED",
        "key": "change_selection",
        "created_at": datetime.now(UTC).isoformat(),
        "selected": selected,
        "criteria": CRITERIA,
        "material_drop": MATERIAL_DROP,
        "decision": decision,
        "v1": {"oscd_test": v1_oscd["metrics"], "india_forest_test": v1_forest["metrics"]},
        "v2": {"oscd_test": v2_oscd["metrics"], "india_forest_test": v2_forest["metrics"]},
    }
    (REPORTS / "change_selection.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if selected == "v2":
        shutil.copyfile(ROOT / v2_oscd["checkpoint"], INSTALLED)
    print(decision)


if __name__ == "__main__":
    main()
