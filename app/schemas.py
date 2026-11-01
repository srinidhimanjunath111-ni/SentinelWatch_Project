"""Request contracts for the API. Response shapes live in app/services/payloads.py."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.satellite.geometry import GeometryValidationError, validate_geometry
from app.satellite.grid import AOITooLargeError, build_grid
from app.vocabulary import ALERT_STATUSES, CATEGORIES, LEGACY_CATEGORY_MAP, ZONE_TYPES

DEFAULT_AREA_CATEGORIES = ["DEFORESTATION", "MINING", "CONSTRUCTION", "URBANISATION", "OTHER_CHANGE"]


def _calendar_date(value: str | None) -> str | None:
    if value is None:
        return value
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{value} is not a real calendar date; use YYYY-MM-DD.") from error
    return value


def _checked_aoi(value: dict[str, Any]) -> dict[str, Any]:
    try:
        geometry = validate_geometry(value)
        build_grid(geometry)
    except (GeometryValidationError, AOITooLargeError) as error:
        raise ValueError(str(error)) from error
    return geometry


def _normalised_categories(values: list[str]) -> list[str]:
    normalised = sorted(
        {
            LEGACY_CATEGORY_MAP.get(value.strip().upper(), value.strip().upper())
            for value in values
            if value.strip()
        }
    )
    if not normalised:
        raise ValueError("Select at least one monitoring category.")
    unknown = set(normalised) - set(CATEGORIES)
    if unknown:
        raise ValueError(f"Unsupported monitoring categories: {', '.join(sorted(unknown))}")
    return normalised


class MonitoringAreaCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    geometry: dict[str, Any]
    categories: list[str] = Field(default_factory=lambda: list(DEFAULT_AREA_CATEGORIES))
    max_cloud_cover: float = Field(default=40.0, ge=0, le=100)
    min_change_area_m2: float = Field(default=5000.0, gt=0, le=100_000_000)
    sensitivity: str = Field(default="MEDIUM", pattern="^(LOW|MEDIUM|HIGH)$")
    monitoring_interval_minutes: int = Field(default=1440, ge=15, le=43_200)
    state: str | None = Field(default=None, max_length=80)
    district: str | None = Field(default=None, max_length=120)
    site_type: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    sources: list[Any] | None = None

    @field_validator("geometry")
    @classmethod
    def valid_geometry(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _checked_aoi(value)

    @field_validator("categories")
    @classmethod
    def valid_categories(cls, values: list[str]) -> list[str]:
        return _normalised_categories(values)


class MonitoringAreaUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    geometry: dict[str, Any] | None = None
    categories: list[str] | None = None
    max_cloud_cover: float | None = Field(default=None, ge=0, le=100)
    min_change_area_m2: float | None = Field(default=None, gt=0, le=100_000_000)
    sensitivity: str | None = Field(default=None, pattern="^(LOW|MEDIUM|HIGH)$")
    monitoring_interval_minutes: int | None = Field(default=None, ge=15, le=43_200)
    status: str | None = Field(default=None, pattern="^(ACTIVE|PAUSED)$")
    state: str | None = Field(default=None, max_length=80)
    district: str | None = Field(default=None, max_length=120)
    site_type: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("geometry")
    @classmethod
    def valid_optional_geometry(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else _checked_aoi(value)

    @field_validator("categories")
    @classmethod
    def valid_optional_categories(cls, values: list[str] | None) -> list[str] | None:
        return None if values is None else _normalised_categories(values)


class ReplayRequest(BaseModel):
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    max_scenes: int = Field(default=6, ge=1, le=40)
    force: bool = False

    @field_validator("start_date", "end_date")
    @classmethod
    def real_dates(cls, value: str) -> str:
        return _calendar_date(value)


class StatusUpdate(BaseModel):
    status: str = Field(pattern="^(new|reviewed|verified|escalated|dismissed)$")
    review_note: str | None = Field(default=None, max_length=2000)
    reviewer: str | None = Field(default=None, max_length=120)


class ReviewCreate(BaseModel):
    action: str = Field(pattern="^(reviewed|verified|escalated|dismissed)$")
    note: str | None = Field(default=None, max_length=2000)
    reviewer: str | None = Field(default=None, max_length=120)


class AlertStatusUpdate(BaseModel):
    status: str
    note: str | None = Field(default=None, max_length=2000)
    actor: str | None = Field(default=None, max_length=120)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in ALERT_STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(ALERT_STATUSES)}")
        return value


class AlertDispatchRequest(BaseModel):
    department_id: str | None = None


class AlertAcknowledge(BaseModel):
    acknowledgement_note: str | None = Field(default=None, max_length=2000)


class PriorityDiscoveryRequest(BaseModel):
    region_name: str = Field(default="User-defined region", min_length=2, max_length=160)
    geometry: dict[str, Any]
    grid_size: int = Field(default=2, ge=1, le=3)
    lookback_days: int = Field(default=30, ge=7, le=365)

    @field_validator("geometry")
    @classmethod
    def valid_region_geometry(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_geometry(value)
        except GeometryValidationError as error:
            raise ValueError(str(error)) from error


class CandidateApproval(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    categories: list[str] = Field(default_factory=lambda: list(DEFAULT_AREA_CATEGORIES))
    min_change_area_m2: float = Field(default=5000.0, gt=0)
    sensitivity: str = Field(default="MEDIUM", pattern="^(LOW|MEDIUM|HIGH)$")
    state: str | None = Field(default=None, max_length=80)

    @field_validator("categories")
    @classmethod
    def valid_categories(cls, values: list[str]) -> list[str]:
        return _normalised_categories(values)


class DepartmentCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    category: str = Field(min_length=2, max_length=80)
    state: str | None = Field(default=None, max_length=80)
    recipient_email: str | None = Field(default=None, max_length=255)
    active: bool = True


class AlertAssignment(BaseModel):
    department_id: str
    notify_email: bool = False


class AuthorisedZoneCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    zone_type: str
    geometry: dict[str, Any]
    authority: str | None = Field(default=None, max_length=200)
    reference: str | None = Field(default=None, max_length=255)
    valid_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    valid_to: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    source: str | None = Field(default=None, max_length=255)
    is_illustrative: bool = False

    @field_validator("valid_from", "valid_to")
    @classmethod
    def real_dates(cls, value: str | None) -> str | None:
        return _calendar_date(value)

    @model_validator(mode="after")
    def dates_in_order(self) -> AuthorisedZoneCreate:
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from must not be after valid_to.")
        return self

    @field_validator("zone_type")
    @classmethod
    def valid_zone_type(cls, value: str) -> str:
        normalised = value.strip().upper()
        if normalised not in ZONE_TYPES:
            raise ValueError(f"zone_type must be one of: {', '.join(ZONE_TYPES)}")
        return normalised

    @field_validator("geometry")
    @classmethod
    def valid_zone_geometry(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_geometry(value)
        except GeometryValidationError as error:
            raise ValueError(str(error)) from error
