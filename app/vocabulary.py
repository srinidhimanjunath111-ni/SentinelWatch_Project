"""Classification vocabulary shared by the pipeline and API (contract section 4)."""

from __future__ import annotations

CATEGORIES = (
    "DEFORESTATION",
    "MINING",
    "CONSTRUCTION",
    "URBANISATION",
    "FLOOD",
    "LANDSLIDE",
    "BURN_SCAR",
    "SEASONAL_VEGETATION",
    "CROP_CYCLE",
    "WATER_CHANGE",
    "OTHER_CHANGE",
)
HUMAN_CAUSED = frozenset({"DEFORESTATION", "MINING", "CONSTRUCTION", "URBANISATION"})
NATURAL_CAUSED = frozenset({"FLOOD", "LANDSLIDE", "BURN_SCAR"})
BENIGN_CATEGORIES = frozenset({"SEASONAL_VEGETATION", "CROP_CYCLE"})

# Names used by the earlier prototype, still accepted from older clients and databases.
LEGACY_CATEGORY_MAP = {
    "VEGETATION_LOSS": "DEFORESTATION",
    "MINING_LAND_DISTURBANCE": "MINING",
}

SUSPECTED_UNAUTHORISED = "SUSPECTED_UNAUTHORISED"
AUTHORISED_ACTIVITY = "AUTHORISED_ACTIVITY"
NATURAL_EVENT = "NATURAL_EVENT"
SEASONAL_BENIGN = "SEASONAL_BENIGN"
NEEDS_REVIEW = "NEEDS_REVIEW"
VERDICTS = (
    SUSPECTED_UNAUTHORISED,
    AUTHORISED_ACTIVITY,
    NATURAL_EVENT,
    SEASONAL_BENIGN,
    NEEDS_REVIEW,
)

ALERT_STATUSES = ("open", "dispatched", "acknowledged", "field_verification", "resolved", "false_positive")
# Alerts still awaiting an outcome. A repeat detection merges only into these.
ACTIVE_ALERT_STATUSES = ("open", "dispatched", "acknowledged", "field_verification")
ALERT_TRANSITIONS = {
    "open": {"dispatched", "acknowledged", "resolved", "false_positive"},
    "dispatched": {"acknowledged", "field_verification", "resolved", "false_positive"},
    "acknowledged": {"field_verification", "resolved", "false_positive"},
    "field_verification": {"resolved", "false_positive"},
    "resolved": set(),
    "false_positive": set(),
}

ORIGIN_LIVE = "live"
ORIGIN_REPLAY = "archive_replay"

ZONE_TYPES = ("MINING", "CONSTRUCTION", "FORESTRY", "OTHER")
# Which registered zone type can authorise which detected category.
ZONE_AUTHORISES = {
    # Clearing inside a registered mine footprint is part of working the mine.
    "MINING": frozenset({"MINING", "DEFORESTATION"}),
    "CONSTRUCTION": frozenset({"CONSTRUCTION", "URBANISATION"}),
    "FORESTRY": frozenset({"DEFORESTATION"}),
    "OTHER": frozenset(HUMAN_CAUSED),
}

# Routing key -> (department name template, mailbox prefix) for on-demand departments.
ROUTING = {
    "DEFORESTATION": "FOREST",
    "BURN_SCAR": "FOREST",
    "MINING": "MINES",
    "CONSTRUCTION": "PLANNING",
    "URBANISATION": "PLANNING",
    "FLOOD": "DISASTER",
    "LANDSLIDE": "DISASTER",
}
DEFAULT_ROUTE = "ENVIRONMENT"
DEPARTMENT_TEMPLATES = {
    "FOREST": ("{state} Forest Department", "forest"),
    "MINES": ("{state} Mines & Geology Department", "mines"),
    "PLANNING": ("{state} Town & Country Planning Department", "planning"),
    "DISASTER": ("{state} State Disaster Management Authority", "sdma"),
    "ENVIRONMENT": ("{state} Pollution Control Board", "environment"),
}
