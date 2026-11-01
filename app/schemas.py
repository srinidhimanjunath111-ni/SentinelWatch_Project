"""Request and response contracts for the monitoring and review API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.satellite.geometry import GeometryValidationError, validate_geometry


class MonitoringAreaCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    geometry: dict[str, Any]
    categories: list[str] = Field(
        default_factory=lambda: ["VEGETATION_LOSS", "CONSTRUCTION", "MINING_LAND_DISTURBANCE"]
    )
    max_cloud_cover: float = Field(default=20.0, ge=0, le=100)
    min_change_area_m2: float = Field(default=400.0, gt=0, le=100_000_000)
    sensitivity: str = Field(default="MEDIUM", pattern="^(LOW|MEDIUM|HIGH)$")
    monitoring_interval_minutes: int = Field(default=1440, ge=15, le=43_200)

    @field_validator("geometry")
    @classmethod
    def valid_geometry(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_geometry(value)
        except GeometryValidationError as error:
            raise ValueError(str(error)) from error

    @field_validator("categories")
    @classmethod
    def valid_categories(cls, values: list[str]) -> list[str]:
        allowed = {
            "VEGETATION_LOSS",
            "CONSTRUCTION",
            "MINING_LAND_DISTURBANCE",
            "OTHER_CHANGE",
        }
        normalised = sorted({value.strip().upper() for value in values if value.strip()})
        if not normalised:
            raise ValueError("Select at least one monitoring category.")
        unknown = set(normalised) - allowed
        if unknown:
            raise ValueError(f"Unsupported monitoring categories: {', '.join(sorted(unknown))}")
        return normalised


class MonitoringAreaUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    geometry: dict[str, Any] | None = None
    categories: list[str] | None = None
    max_cloud_cover: float | None = Field(default=None, ge=0, le=100)
    min_change_area_m2: float | None = Field(default=None, gt=0, le=100_000_000)
    sensitivity: str | None = Field(default=None, pattern="^(LOW|MEDIUM|HIGH)$")
    monitoring_interval_minutes: int | None = Field(default=None, ge=15, le=43_200)
    status: str | None = Field(default=None, pattern="^(ACTIVE|PAUSED)$")

    @field_validator("geometry")
    @classmethod
    def valid_optional_geometry(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        try:
            return validate_geometry(value)
        except GeometryValidationError as error:
            raise ValueError(str(error)) from error

    @field_validator("categories")
    @classmethod
    def valid_optional_categories(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return values
        return MonitoringAreaCreate.valid_categories(values)


class MonitoringAreaOut(BaseModel):
    id: str
    name: str
    geometry: dict[str, Any]
    bbox: list[float]
    center_lat: float
    center_lon: float
    categories: list[str]
    max_cloud_cover: float
    min_change_area_m2: float
    sensitivity: str
    monitoring_interval_minutes: int
    status: str
    created_at: datetime
    updated_at: datetime
    last_checked_at: datetime | None
    latest_run: MonitoringRunOut | None = None


class AcquisitionOut(BaseModel):
    id: str
    provider: str
    product_id: str
    collection: str
    satellite: str | None
    sensor: str | None
    processing_level: str | None
    acquisition_datetime: datetime
    cloud_cover: float | None
    geometry: dict[str, Any] | None
    bbox: list[float] | None
    source_url: str | None
    quality_status: str
    quality_note: str | None
    cached_raster: bool
    first_seen_at: datetime | None = None
    selection_role: str | None = None
    last_selected_at: datetime | None = None


class MonitoringRunOut(BaseModel):
    id: str
    aoi_id: str
    status: str
    stage: str
    progress_percent: int
    message: str
    error_code: str | None
    error_detail: str | None
    before_acquisition_id: str | None
    after_acquisition_id: str | None
    started_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class AnalysisOut(BaseModel):
    id: str
    aoi_id: str
    run_id: str | None
    status: str
    analysis_method: str
    pixel_area_m2: float
    quality: dict[str, Any]
    result: dict[str, Any]
    evidence_path: str | None
    created_at: datetime
    completed_at: datetime | None


class ModelVersionOut(BaseModel):
    id: str
    name: str
    architecture: str
    checkpoint_path: str
    status: str
    training_dataset: str | None
    metrics: dict[str, Any]
    notes: str | None
    created_at: datetime
    updated_at: datetime


class SystemStatusOut(BaseModel):
    service: str
    database: str
    provider: str
    catalogue_endpoint: str
    imagery_credentials_configured: bool
    scheduler_enabled: bool
    model_status: str
    model_version: str
    device: str
    rasterio_available: bool
    demo_mode: bool
    message: str


class FindingOut(BaseModel):
    id: str
    activity: str
    category: str | None
    confidence: float
    severity: str
    area_pixels: int
    area_m2: float
    centroid_x: float
    centroid_y: float
    bbox: str
    geometry_json: str | None
    ndvi_before: float | None
    ndvi_after: float | None
    ndvi_delta: float | None
    persistence_score: float | None
    requires_human_verification: bool
    evidence_json: str

    model_config = {"from_attributes": True}


class AlertOut(BaseModel):
    id: str
    case_id: str
    finding_id: str | None
    title: str
    message: str
    severity: str
    status: str
    acknowledgement_note: str | None
    created_at: datetime
    acknowledged_at: datetime | None
    department_id: str | None = None
    priority_score: float | None = None

    model_config = {"from_attributes": True}


class ReviewOut(BaseModel):
    id: str
    case_id: str
    action: str
    note: str | None
    reviewer: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CaseSummaryOut(BaseModel):
    id: str
    title: str
    location: str | None
    before_date: str | None
    after_date: str | None
    pixel_resolution_m: float
    source: str | None
    status: str
    analysis_method: str
    overall_confidence: float
    changed_area_px: int
    changed_area_m2: float
    highest_severity: str
    before_image: str
    after_image: str
    mask_image: str
    aoi_id: str | None
    analysis_id: str | None
    before_acquisition_id: str | None
    after_acquisition_id: str | None
    data_origin: str | None
    requires_human_verification: bool
    model_version_label: str | None
    quality_note: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CaseDetailOut(CaseSummaryOut):
    review_note: str | None
    findings: list[FindingOut]
    alerts: list[AlertOut]
    reviews: list[ReviewOut] = Field(default_factory=list)


class StatusUpdate(BaseModel):
    status: str = Field(pattern="^(new|reviewed|verified|escalated|dismissed)$")
    review_note: str | None = Field(default=None, max_length=2000)
    reviewer: str | None = Field(default=None, max_length=120)


class ReviewCreate(BaseModel):
    action: str = Field(pattern="^(reviewed|verified|escalated|dismissed)$")
    note: str | None = Field(default=None, max_length=2000)
    reviewer: str | None = Field(default=None, max_length=120)


class AlertAcknowledge(BaseModel):
    acknowledgement_note: str | None = Field(default=None, max_length=2000)


class PriorityDiscoveryRequest(BaseModel):
    region_name: str = Field(default="User-defined region", min_length=2, max_length=160)
    geometry: dict[str, Any]
    grid_size: int = Field(default=2, ge=1, le=3)
    max_cloud_cover: float = Field(default=20.0, ge=0, le=100)
    lookback_days: int = Field(default=30, ge=7, le=365)

    @field_validator("geometry")
    @classmethod
    def valid_region_geometry(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_geometry(value)
        except GeometryValidationError as error:
            raise ValueError(str(error)) from error


class PriorityCandidateOut(BaseModel):
    id: str
    geometry: dict[str, Any]
    bbox: list[float]
    region_name: str
    discovery_method: str
    score: float
    explanation: str
    evidence: dict[str, Any]
    status: str
    monitoring_area_id: str | None
    created_at: datetime
    updated_at: datetime


class CandidateApproval(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    categories: list[str] = Field(default_factory=lambda: ["VEGETATION_LOSS", "CONSTRUCTION"])
    max_cloud_cover: float = Field(default=20.0, ge=0, le=100)
    min_change_area_m2: float = Field(default=400.0, gt=0)
    sensitivity: str = Field(default="MEDIUM", pattern="^(LOW|MEDIUM|HIGH)$")


class DepartmentCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    category: str = Field(min_length=2, max_length=80)
    recipient_email: str | None = Field(default=None, max_length=255)
    active: bool = True


class DepartmentOut(DepartmentCreate):
    id: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AlertAssignment(BaseModel):
    department_id: str
    notify_email: bool = False


class NotificationOut(BaseModel):
    id: str
    alert_id: str
    department_id: str | None
    channel: str
    destination: str | None
    status: str
    detail: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DataStatusOut(BaseModel):
    database: str
    database_engine: str
    monitoring_areas: int
    observations: int
    analyses: int
    alerts: int
    latest_catalogue_query_at: datetime | None
    latest_catalogue_query_status: str | None
    newest_actual_acquisition_at: datetime | None
    latest_provider_error: str | None
    imagery_credentials_configured: bool


class MetricsOut(BaseModel):
    total_cases: int
    open_alerts: int
    high_priority_alerts: int
    changed_area_m2: float
    activity_counts: dict[str, int]
    method_counts: dict[str, int]
    monitored_areas: int = 0
    active_areas: int = 0
    completed_monitoring_runs: int = 0


# ``MonitoringAreaOut`` refers to the run response declared later in this file.
MonitoringAreaOut.model_rebuild()
