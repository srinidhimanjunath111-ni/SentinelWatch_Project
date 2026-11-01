"""Persistent evidence model for AOI monitoring and investigator review.

The application intentionally stores the catalogue records, processing provenance,
model state, and human decisions separately.  A case is a reviewable lead, not a
finding of illegality.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.timeutil import utcnow


def uuid_str() -> str:
    return str(uuid.uuid4())


class MonitoringArea(Base):
    """A user-defined geographic area monitored through the official catalogue."""

    __tablename__ = "monitoring_areas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(160))
    geometry_json: Mapped[str] = mapped_column(Text)
    bbox_json: Mapped[str] = mapped_column(String(160))
    center_lat: Mapped[float] = mapped_column(Float)
    center_lon: Mapped[float] = mapped_column(Float)
    categories_json: Mapped[str] = mapped_column(Text, default="[]")
    max_cloud_cover: Mapped[float] = mapped_column(Float, default=20.0)
    min_change_area_m2: Mapped[float] = mapped_column(Float, default=400.0)
    sensitivity: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    monitoring_interval_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    district: Mapped[str | None] = mapped_column(String(120), nullable=True)
    site_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sources_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # Set on areas that come from app/data/monitored_sites.json, which reseeding matches on.
    site_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    seed_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suggested_replay_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    acquisition_links: Mapped[list[AOIAcquisition]] = relationship(
        back_populates="area", cascade="all, delete-orphan"
    )
    runs: Mapped[list[MonitoringRun]] = relationship(
        back_populates="area", cascade="all, delete-orphan", order_by="MonitoringRun.started_at.desc()"
    )
    analyses: Mapped[list[AnalysisRecord]] = relationship(
        back_populates="area", cascade="all, delete-orphan", order_by="AnalysisRecord.created_at.desc()"
    )
    cases: Mapped[list[Case]] = relationship(back_populates="area")


class SatelliteAcquisition(Base):
    """One real product discovered from a satellite provider catalogue."""

    __tablename__ = "satellite_acquisitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    provider: Mapped[str] = mapped_column(String(64), default="copernicus-data-space")
    product_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # Provider-independent identity, so failover cannot analyse one overpass twice.
    scene_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tile: Mapped[str | None] = mapped_column(String(16), nullable=True)
    collection: Mapped[str] = mapped_column(String(96), default="sentinel-2-l2a")
    satellite: Mapped[str | None] = mapped_column(String(48), nullable=True)
    sensor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processing_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    acquisition_datetime: Mapped[datetime] = mapped_column(DateTime, index=True)
    cloud_cover: Mapped[float | None] = mapped_column(Float, nullable=True)
    geometry_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    bbox_json: Mapped[str | None] = mapped_column(String(160), nullable=True)
    asset_metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    quality_status: Mapped[str] = mapped_column(String(32), default="DISCOVERED")
    quality_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    cached_raster_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    band_manifest_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    aoi_links: Mapped[list[AOIAcquisition]] = relationship(
        back_populates="acquisition", cascade="all, delete-orphan"
    )


class AOIAcquisition(Base):
    """A de-duplicated relationship between an AOI and discovered product."""

    __tablename__ = "aoi_acquisitions"
    __table_args__ = (UniqueConstraint("aoi_id", "acquisition_id", name="uq_aoi_acquisition"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    aoi_id: Mapped[str] = mapped_column(ForeignKey("monitoring_areas.id"), index=True)
    acquisition_id: Mapped[str] = mapped_column(ForeignKey("satellite_acquisitions.id"), index=True)
    selection_role: Mapped[str] = mapped_column(String(32), default="CANDIDATE")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_selected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AOI-specific observation state, measured from SCL inside this AOI's grid.
    aoi_clear_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)
    scene_status: Mapped[str] = mapped_column(String(32), default="DISCOVERED")
    thumbnail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    area: Mapped[MonitoringArea] = relationship(back_populates="acquisition_links")
    acquisition: Mapped[SatelliteAcquisition] = relationship(back_populates="aoi_links")


class ModelVersion(Base):
    """Truthful state and provenance of an inference model version."""

    __tablename__ = "model_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    architecture: Mapped[str] = mapped_column(String(128), default="Siamese U-Net")
    checkpoint_path: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="NOT_TRAINED")
    training_dataset: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    analyses: Mapped[list[AnalysisRecord]] = relationship(back_populates="model_version")


class MonitoringRun(Base):
    """An auditable execution of a live AOI check, including genuine progress."""

    __tablename__ = "monitoring_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    aoi_id: Mapped[str] = mapped_column(ForeignKey("monitoring_areas.id"), index=True)
    status: Mapped[str] = mapped_column(String(48), default="QUEUED", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="QUEUED")
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="Waiting to start.")
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    catalogue_query_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_acquisition_id: Mapped[str | None] = mapped_column(
        ForeignKey("satellite_acquisitions.id"), nullable=True
    )
    after_acquisition_id: Mapped[str | None] = mapped_column(
        ForeignKey("satellite_acquisitions.id"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    origin: Mapped[str] = mapped_column(String(24), default="live")
    trigger: Mapped[str | None] = mapped_column(String(32), nullable=True)
    as_of_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    replay_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    replay_force: Mapped[bool] = mapped_column(Boolean, default=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)

    area: Mapped[MonitoringArea] = relationship(back_populates="runs")
    analysis: Mapped[AnalysisRecord | None] = relationship(back_populates="run", uselist=False)


class AnalysisRecord(Base):
    """Processing provenance and result summary for one selected image pair."""

    __tablename__ = "analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("monitoring_runs.id"), nullable=True)
    aoi_id: Mapped[str] = mapped_column(ForeignKey("monitoring_areas.id"), index=True)
    before_acquisition_id: Mapped[str] = mapped_column(ForeignKey("satellite_acquisitions.id"))
    after_acquisition_id: Mapped[str] = mapped_column(ForeignKey("satellite_acquisitions.id"))
    model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")
    analysis_method: Mapped[str] = mapped_column(String(64))
    pixel_area_m2: Mapped[float] = mapped_column(Float)
    quality_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    area: Mapped[MonitoringArea] = relationship(back_populates="analyses")
    run: Mapped[MonitoringRun | None] = relationship(back_populates="analysis")
    model_version: Mapped[ModelVersion | None] = relationship(back_populates="analyses")
    case: Mapped[Case | None] = relationship(back_populates="analysis", uselist=False)


class Case(Base):
    """A human-reviewable suspicious change lead and its preserved evidence."""

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    title: Mapped[str] = mapped_column(String(160), default="Untitled investigation")
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    before_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    after_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pixel_resolution_m: Mapped[float] = mapped_column(Float, default=10.0)
    source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="new")
    analysis_method: Mapped[str] = mapped_column(String(64), default="spectral-baseline")
    overall_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    changed_area_px: Mapped[int] = mapped_column(Integer, default=0)
    changed_area_m2: Mapped[float] = mapped_column(Float, default=0.0)
    highest_severity: Mapped[str] = mapped_column(String(20), default="none")
    before_image: Mapped[str] = mapped_column(String(512))
    after_image: Mapped[str] = mapped_column(String(512))
    mask_image: Mapped[str] = mapped_column(String(512))
    aoi_id: Mapped[str | None] = mapped_column(
        ForeignKey("monitoring_areas.id"), nullable=True, index=True
    )
    analysis_id: Mapped[str | None] = mapped_column(ForeignKey("analyses.id"), nullable=True)
    before_acquisition_id: Mapped[str | None] = mapped_column(
        ForeignKey("satellite_acquisitions.id"), nullable=True
    )
    after_acquisition_id: Mapped[str | None] = mapped_column(
        ForeignKey("satellite_acquisitions.id"), nullable=True
    )
    data_origin: Mapped[str | None] = mapped_column(String(48), nullable=True)
    requires_human_verification: Mapped[bool] = mapped_column(Boolean, default=True)
    model_version_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quality_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(String(24), default="live", index=True)
    replay_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    image_bounds_json: Mapped[str | None] = mapped_column(String(160), nullable=True)
    images_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    seasonal_reference_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    area: Mapped[MonitoringArea | None] = relationship(back_populates="cases")
    analysis: Mapped[AnalysisRecord | None] = relationship(back_populates="case")
    findings: Mapped[list[Finding]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="Finding.confidence.desc()"
    )
    # Alerts reference findings, so they must be flushed after them and deleted before them.
    alerts: Mapped[list[Alert]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="Alert.created_at.desc()"
    )
    reviews: Mapped[list[CaseReview]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="CaseReview.created_at.desc()"
    )


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    activity: Mapped[str] = mapped_column(String(40))
    category: Mapped[str | None] = mapped_column(String(48), default="OTHER_CHANGE")
    confidence: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(20))
    area_pixels: Mapped[int] = mapped_column(Integer)
    area_m2: Mapped[float] = mapped_column(Float)
    centroid_x: Mapped[float] = mapped_column(Float)
    centroid_y: Mapped[float] = mapped_column(Float)
    bbox: Mapped[str] = mapped_column(String(128))
    geometry_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ndvi_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    ndvi_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    ndvi_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    persistence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    requires_human_verification: Mapped[bool] = mapped_column(Boolean, default=True)
    evidence_json: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    verdict_reasons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    transition_from: Mapped[str | None] = mapped_column(String(24), nullable=True)
    transition_to: Mapped[str | None] = mapped_column(String(24), nullable=True)
    indices_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    change_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    # True: still differs from last year's same season. False: does not. None: no reference.
    seasonal_persists: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    centroid_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    centroid_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    thumbnail: Mapped[str | None] = mapped_column(String(512), nullable=True)

    case: Mapped[Case] = relationship(back_populates="findings")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    finding_id: Mapped[str | None] = mapped_column(ForeignKey("findings.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(180))
    message: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="open")
    acknowledgement_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    priority_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    category: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    origin: Mapped[str] = mapped_column(String(24), default="live")
    replay_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    aoi_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    acquisition_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    detected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When processing of the scene finished; latency_seconds runs from here to dispatch.
    scene_processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    latency_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    persistence_count: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    case: Mapped[Case] = relationship(back_populates="alerts")
    finding: Mapped[Finding | None] = relationship()
    department: Mapped[Department | None] = relationship(back_populates="alerts")
    notifications: Mapped[list[NotificationAttempt]] = relationship(
        back_populates="alert", cascade="all, delete-orphan", order_by="NotificationAttempt.created_at.desc()"
    )


class CaseReview(Base):
    """Every reviewer decision is retained instead of overwriting the audit trail."""

    __tablename__ = "case_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    action: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    case: Mapped[Case] = relationship(back_populates="reviews")


class AuditLog(Base):
    """Append-only, lightweight audit events for operational transparency."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(96), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PriorityCandidate(Base):
    """A transparent grid or evidence-led suggestion awaiting human approval."""

    __tablename__ = "priority_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    geometry_json: Mapped[str] = mapped_column(Text)
    bbox_json: Mapped[str] = mapped_column(String(160))
    region_name: Mapped[str] = mapped_column(String(160))
    discovery_method: Mapped[str] = mapped_column(String(80))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    explanation: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="PRELIMINARY", index=True)
    monitoring_area_id: Mapped[str | None] = mapped_column(ForeignKey("monitoring_areas.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Department(Base):
    """Locally configurable routing category; it never implies official jurisdiction."""

    __tablename__ = "departments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    category: Mapped[str] = mapped_column(String(80))
    state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    recipient_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    alerts: Mapped[list[Alert]] = relationship(back_populates="department")


class NotificationAttempt(Base):
    """Persisted delivery outcome for in-app or explicitly configured email routing."""

    __tablename__ = "notification_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    alert_id: Mapped[str] = mapped_column(ForeignKey("alerts.id"), index=True)
    department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    channel: Mapped[str] = mapped_column(String(32), default="IN_APP")
    destination: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="RECORDED")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(24), default="auto")

    alert: Mapped[Alert] = relationship(back_populates="notifications")
    department: Mapped[Department | None] = relationship()


class CatalogueRefresh(Base):
    """One persisted catalogue-refresh outcome for operational status transparency."""

    __tablename__ = "catalogue_refreshes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    provider: Mapped[str] = mapped_column(String(64), default="copernicus-data-space")
    aoi_id: Mapped[str | None] = mapped_column(ForeignKey("monitoring_areas.id"), nullable=True)
    query_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32))
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    newest_acquisition_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AuthorisedZone(Base):
    """A registered permission footprint. OSM data is context and never lands here."""

    __tablename__ = "authorised_zones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(200))
    zone_type: Mapped[str] = mapped_column(String(24), index=True)
    geometry_json: Mapped[str] = mapped_column(Text)
    authority: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    valid_from: Mapped[str | None] = mapped_column(String(10), nullable=True)
    valid_to: Mapped[str | None] = mapped_column(String(10), nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_illustrative: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
