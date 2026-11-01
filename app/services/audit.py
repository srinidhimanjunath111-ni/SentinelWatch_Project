"""Append-only audit helpers used by both API actions and background runs."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def record_audit(
    db: Session, entity_type: str, entity_id: str, event_type: str, payload: dict[str, Any] | None = None
) -> AuditLog:
    event = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        payload_json=json.dumps(payload or {}, default=str, sort_keys=True),
    )
    db.add(event)
    return event
