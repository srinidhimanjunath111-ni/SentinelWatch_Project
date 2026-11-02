"""Truthful, downloadable investigation reports for preserved case evidence.

The report deliberately assembles only fields that are already persisted in the
case, related findings, alerts, human reviews, and audit trail.  It is a
decision-support record, not an assertion that an activity is illegal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Iterable
from xml.sax.saxutils import escape

from app.models import AuditLog, Case
from app.services.storage import resolve_public_path

try:  # Keep the core web/API install usable when PDF reports are not required.
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Image as PlatypusImage,
    )
    from reportlab.platypus import (
        KeepTogether,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    REPORTLAB_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by API fallback tests.
    REPORTLAB_AVAILABLE = False


class ReportDependencyUnavailable(RuntimeError):
    """Raised when the optional ReportLab feature has not been installed."""


_NOT_RECORDED = "Not recorded"
_MAX_DISPLAY_CHARS = 1_500
_MAX_AUDIT_ROWS = 60


def reportlab_available() -> bool:
    """Return whether this process can render PDF case reports."""

    return REPORTLAB_AVAILABLE


def report_filename(case: Case) -> str:
    """Generate a safe, stable download name without exposing user supplied titles."""

    return f"sentinelwatch-case-{case.id}-investigation-report.pdf"


def _as_text(value: Any) -> str:
    if value is None or value == "":
        return _NOT_RECORDED
    if isinstance(value, datetime):
        return _format_datetime(value)
    return str(value)


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return _NOT_RECORDED
    if value.tzinfo is None:
        return value.strftime("%Y-%m-%d %H:%M:%S UTC (stored without timezone)")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_number(value: float | int | None, suffix: str = "", digits: int = 2) -> str:
    if value is None:
        return _NOT_RECORDED
    if isinstance(value, int):
        return f"{value:,}{suffix}"
    return f"{value:,.{digits}f}{suffix}"


def _format_percent(value: float | None) -> str:
    if value is None:
        return _NOT_RECORDED
    return f"{value * 100:.1f}%"


def _safe_json(value: str | None) -> str:
    """Pretty-print stored JSON, retaining an explicit layout limit when needed."""

    if not value:
        return _NOT_RECORDED
    try:
        rendered = json.dumps(json.loads(value), indent=2, sort_keys=True, default=str)
    except (TypeError, json.JSONDecodeError):
        rendered = value
    if len(rendered) > _MAX_DISPLAY_CHARS:
        return (
            f"{rendered[:_MAX_DISPLAY_CHARS]}\n"
            "[PDF display truncated for layout; complete value remains in the application database.]"
        )
    return rendered


def _paragraph(value: Any, style: ParagraphStyle) -> Paragraph:
    text = escape(_as_text(value)).replace("\n", "<br/>")
    return Paragraph(text, style)


def _section_heading(text: str, styles: dict[str, ParagraphStyle]) -> list[Any]:
    return [Spacer(1, 4 * mm), Paragraph(escape(text), styles["Section"]), Spacer(1, 1.5 * mm)]


def _key_value_table(rows: Iterable[tuple[str, Any]], styles: dict[str, ParagraphStyle]) -> Table:
    data = [[_paragraph(key, styles["Label"]), _paragraph(value, styles["BodySmall"])] for key, value in rows]
    table = Table(data, colWidths=[48 * mm, 124 * mm], hAlign="LEFT", repeatRows=0)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EAF2EC")),
                ("BOX", (0, 0), (-1, -1), 0.35, colors.HexColor("#BACABD")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D6E0D8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _data_table(
    headers: list[str], rows: list[list[Any]], widths: list[float], styles: dict[str, ParagraphStyle]
) -> Table:
    data: list[list[Paragraph]] = [[_paragraph(header, styles["TableHeader"]) for header in headers]]
    data.extend([[_paragraph(value, styles["TableCell"]) for value in row] for row in rows])
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#174A35")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.35, colors.HexColor("#A9BBAE")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D6E0D8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _report_styles() -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "Title": ParagraphStyle(
            "SWTitle",
            parent=sample["Title"],
            fontName="Helvetica-Bold",
            fontSize=19,
            leading=23,
            textColor=colors.HexColor("#153E2D"),
            alignment=TA_CENTER,
            spaceAfter=2 * mm,
        ),
        "Subtitle": ParagraphStyle(
            "SWSubtitle",
            parent=sample["Normal"],
            fontSize=9.5,
            leading=13,
            textColor=colors.HexColor("#486252"),
            alignment=TA_CENTER,
            spaceAfter=5 * mm,
        ),
        "Section": ParagraphStyle(
            "SWSection",
            parent=sample["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=colors.HexColor("#174A35"),
            spaceBefore=0,
            spaceAfter=0,
        ),
        "Body": ParagraphStyle(
            "SWBody",
            parent=sample["BodyText"],
            fontSize=9.2,
            leading=13,
            textColor=colors.HexColor("#1E2E24"),
        ),
        "BodySmall": ParagraphStyle(
            "SWBodySmall",
            parent=sample["BodyText"],
            fontSize=8.2,
            leading=10.7,
            textColor=colors.HexColor("#1E2E24"),
        ),
        "Label": ParagraphStyle(
            "SWLabel",
            parent=sample["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8.2,
            leading=10.7,
            textColor=colors.HexColor("#1E2E24"),
        ),
        "TableHeader": ParagraphStyle(
            "SWTableHeader",
            parent=sample["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=7.3,
            leading=8.8,
            textColor=colors.white,
        ),
        "TableCell": ParagraphStyle(
            "SWTableCell",
            parent=sample["BodyText"],
            fontSize=7.2,
            leading=9.0,
            textColor=colors.HexColor("#1E2E24"),
        ),
        "Disclaimer": ParagraphStyle(
            "SWDisclaimer",
            parent=sample["BodyText"],
            fontSize=9,
            leading=12.5,
            textColor=colors.HexColor("#5B3D00"),
            borderColor=colors.HexColor("#D7B666"),
            borderWidth=0.7,
            borderPadding=7,
            backColor=colors.HexColor("#FFF7DB"),
        ),
        "Mono": ParagraphStyle(
            "SWMono",
            parent=sample["Code"],
            fontName="Courier",
            fontSize=6.6,
            leading=8.2,
            textColor=colors.HexColor("#263A2B"),
            wordWrap="CJK",
        ),
    }


def _draw_page_chrome(canvas: Any, doc: Any) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#A9BBAE"))
    canvas.setLineWidth(0.5)
    canvas.line(doc.leftMargin, A4[1] - 13 * mm, A4[0] - doc.rightMargin, A4[1] - 13 * mm)
    canvas.setFillColor(colors.HexColor("#174A35"))
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.drawString(doc.leftMargin, A4[1] - 10 * mm, "SENTINELWATCH  |  INVESTIGATION RECORD")
    canvas.setFillColor(colors.HexColor("#536B5A"))
    canvas.setFont("Helvetica", 7.5)
    canvas.drawRightString(A4[0] - doc.rightMargin, 10 * mm, f"Page {doc.page}")
    canvas.drawString(doc.leftMargin, 10 * mm, "Decision-support evidence; human verification required")
    canvas.restoreState()


def _analysis_rows(case: Case) -> list[tuple[str, Any]]:
    analysis = case.analysis
    if analysis is None:
        return [("Linked analysis record", _NOT_RECORDED)]
    return [
        ("Analysis record ID", analysis.id),
        ("Analysis status", analysis.status),
        ("Method", analysis.analysis_method),
        ("Pixel area", _format_number(analysis.pixel_area_m2, " m²", 4)),
        ("Created", _format_datetime(analysis.created_at)),
        ("Completed", _format_datetime(analysis.completed_at)),
        ("Quality metadata (stored)", _safe_json(analysis.quality_json)),
        ("Result metadata (stored)", _safe_json(analysis.result_json)),
    ]


def _loaded(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _picture(url: str | None, width: float) -> Any | None:
    """A flowable for a stored evidence image, scaled to width, or None if it is absent."""

    path = resolve_public_path(url)
    if path is None:
        return None
    from PIL import Image as PILImage

    with PILImage.open(path) as image:
        pixel_width, pixel_height = image.size
    return PlatypusImage(str(path), width=width, height=width * pixel_height / pixel_width)


def _evidence_story(case: Case, styles: dict[str, ParagraphStyle]) -> list[Any]:
    images = _loaded(case.images_json, {})
    panel = _picture(images.get("panel") or case.mask_image, 172 * mm)
    story: list[Any] = []
    if panel is None:
        story.append(_paragraph("Evidence images are not available on this server.", styles["Body"]))
        return story
    story.append(panel)
    story.append(Spacer(1, 2 * mm))
    story.append(
        _paragraph(
            f"Left to right: before ({case.before_date}), after ({case.after_date}), change regions "
            "coloured by verdict, NDVI change (red is vegetation lost, blue is gained). Before and "
            "after share one brightness stretch.",
            styles["BodySmall"],
        )
    )
    reference = _loaded(case.seasonal_reference_json, None)
    if reference:
        picture = _picture(reference.get("image"), 80 * mm)
        if picture is not None:
            story.extend(
                [
                    Spacer(1, 3 * mm),
                    KeepTogether(
                        [
                            picture,
                            _paragraph(
                                f"Same-season reference: {reference.get('date')} "
                                f"({reference.get('product_id')}).",
                                styles["BodySmall"],
                            ),
                        ]
                    ),
                ]
            )
    return story


def _context_rows(case: Case) -> list[tuple[str, Any]]:
    context = _loaded(case.context_json, {})
    rain = context.get("rainfall") or {}
    if rain.get("available"):
        rainfall = f"{rain.get('total_mm')} mm in the {rain.get('window_days')} days before the scene"
        if rain.get("normal_mm") is not None:
            rainfall += f" (same window in prior years averaged {rain.get('normal_mm')} mm)"
        if rain.get("peak_14d_mm") is not None:
            rainfall += f"; wettest 14 days since the baseline scene: {rain.get('peak_14d_mm')} mm"
    else:
        rainfall = "Unavailable"
    disasters = [
        f"{event.get('source')}: {event.get('title')} ({event.get('date') or 'undated'}, "
        f"{event.get('distance_km')} km away)"
        for event in context.get("disasters") or []
    ]
    names = lambda items: ", ".join(  # noqa: E731
        str(item.get("name") or "unnamed") for item in items
    )
    return [
        ("Rainfall", rainfall),
        ("Disaster feeds", "; ".join(disasters) or "No nearby events listed"),
        ("Protected areas (OpenStreetMap)", names(context.get("protected_areas") or []) or "None mapped"),
        ("Quarries (OpenStreetMap)", names(context.get("osm_features") or []) or "None mapped"),
        ("Authorised zones on record", names(context.get("authorised_zones") or []) or "None on record"),
        (
            "Sources unavailable",
            ", ".join(str(item.get("source")) for item in context.get("unavailable") or []) or "None",
        ),
    ]


def _finding_block(index: int, finding: Any, styles: dict[str, ParagraphStyle]) -> list[Any]:
    indices = _loaded(finding.indices_json, {})
    persists = {True: "Yes", False: "No", None: "Not tested"}[finding.seasonal_persists]
    rows = [
        ("Category and verdict", f"{finding.category} / {finding.verdict}"),
        ("Land cover", f"{finding.transition_from} to {finding.transition_to}"),
        ("Area", _format_number(finding.area_m2, " m²", 0)),
        ("Centre (lat, lon)", f"{finding.centroid_lat}, {finding.centroid_lon}"),
        ("Evidence score", _format_number(finding.confidence, digits=2)),
        ("Differs from same-season reference", persists),
        (
            "Index changes",
            ", ".join(f"{key.removesuffix('_delta').upper()} {value:+.2f}" for key, value in indices.items()),
        ),
    ]
    reasons = _loaded(finding.verdict_reasons_json, [])
    block: list[Any] = [Paragraph(escape(f"Finding {index}"), styles["Label"]), _key_value_table(rows, styles)]
    for reason in reasons:
        block.append(_paragraph(f"- {reason}", styles["BodySmall"]))
    block.append(Spacer(1, 3 * mm))
    return block


def _area_rows(case: Case) -> list[tuple[str, Any]]:
    area = case.area
    if area is None:
        return [("Monitoring area", _NOT_RECORDED)]
    return [
        ("Monitoring area ID", area.id),
        ("Monitoring area name", area.name),
        ("State and district", f"{area.state or _NOT_RECORDED}, {area.district or _NOT_RECORDED}"),
        ("Area status", area.status),
        ("Centre (latitude, longitude)", f"{area.center_lat:.6f}, {area.center_lon:.6f}"),
        ("Configured minimum change area", _format_number(area.min_change_area_m2, " m²")),
        ("Sensitivity", area.sensitivity),
        ("Monitoring interval", _format_number(area.monitoring_interval_minutes, " minutes", 0)),
        ("Configured categories (stored)", _safe_json(area.categories_json)),
        ("Bounding box (stored)", area.bbox_json),
    ]


def build_case_investigation_report(case: Case, audit_events: Iterable[AuditLog]) -> bytes:
    """Render a case report as PDF bytes from persisted evidence only.

    ``case`` must be attached to an active SQLAlchemy session so its optional
    ``analysis`` and ``area`` relationships can be read.  The caller supplies
    scoped audit events, which prevents a report from exposing unrelated history.
    """

    if not REPORTLAB_AVAILABLE:
        raise ReportDependencyUnavailable(
            "PDF reporting requires ReportLab. Install requirements-operational.txt first."
        )

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
        title=f"SentinelWatch investigation report {case.id}",
        author="SentinelWatch",
    )
    styles = _report_styles()
    story: list[Any] = [
        Paragraph("SentinelWatch Investigation Report", styles["Title"]),
        Paragraph(
            "A preserved monitoring lead and its available evidence trail", styles["Subtitle"]
        ),
        Paragraph(
            "<b>Important decision-support disclaimer.</b> This report records algorithmic change "
            "signals and stored operational evidence. It does not establish that an activity is "
            "illegal, identify a responsible party, or replace a qualified human review, lawful "
            "investigation, field verification, or agency decision.",
            styles["Disclaimer"],
        ),
    ]

    story.extend(_section_heading("1. Case summary", styles))
    story.append(
        _key_value_table(
            [
                ("Case ID", case.id),
                ("Title", case.title),
                ("Current status", case.status),
                (
                    "Origin",
                    "Archive replay of past imagery" if case.origin == "archive_replay" else "Live monitoring",
                ),
                ("Created", _format_datetime(case.created_at)),
                ("Location label", case.location),
                ("Highest recorded severity", case.highest_severity),
                ("Overall change confidence", _format_percent(case.overall_confidence)),
                ("Changed area", _format_number(case.changed_area_m2, " m²")),
                ("Changed pixels", _format_number(case.changed_area_px)),
                ("Human verification required", "Yes" if case.requires_human_verification else "No"),
                ("Latest review note", case.review_note),
                ("Latest review timestamp", _format_datetime(case.reviewed_at)),
            ],
            styles,
        )
    )

    story.extend(_section_heading("2. Data provenance and evidence references", styles))
    story.append(
        _key_value_table(
            [
                ("Data origin", case.data_origin),
                ("Declared source", case.source),
                ("Before acquisition date", case.before_date),
                ("After acquisition date", case.after_date),
                ("Analysis method", case.analysis_method),
                ("Model version label", case.model_version_label),
                ("Pixel resolution", _format_number(case.pixel_resolution_m, " m", 2)),
                ("Quality note", case.quality_note),
                ("Monitoring area ID", case.aoi_id),
                ("Analysis ID", case.analysis_id),
                ("Before acquisition ID", case.before_acquisition_id),
                ("After acquisition ID", case.after_acquisition_id),
            ],
            styles,
        )
    )

    story.extend(_section_heading("Evidence imagery", styles))
    story.extend(_evidence_story(case, styles))

    story.extend(_section_heading("Context at the time of the scene", styles))
    story.append(_key_value_table(_context_rows(case), styles))

    story.extend(_section_heading("3. Linked monitoring area", styles))
    story.append(_key_value_table(_area_rows(case), styles))

    story.extend(_section_heading("4. Linked analysis record", styles))
    story.append(_key_value_table(_analysis_rows(case), styles))

    story.extend(_section_heading("5. Detected change regions", styles))
    findings = list(case.findings)
    if findings:
        for index, finding in enumerate(findings, start=1):
            story.extend(_finding_block(index, finding, styles))
    else:
        story.append(_paragraph("No stored change-region findings are associated with this case.", styles["Body"]))

    story.extend(_section_heading("6. Alerts", styles))
    alerts = list(case.alerts)
    if alerts:
        alert_rows = [
            [
                alert.id,
                alert.title,
                alert.severity,
                alert.status,
                _format_datetime(alert.created_at),
                alert.message,
                alert.status_note or alert.acknowledgement_note,
            ]
            for alert in alerts
        ]
        story.append(
            _data_table(
                ["Alert ID", "Title", "Severity", "Status", "Created", "Message", "Status note"],
                alert_rows,
                [20 * mm, 26 * mm, 14 * mm, 15 * mm, 23 * mm, 35 * mm, 29 * mm],
                styles,
            )
        )
    else:
        story.append(_paragraph("No stored alerts are associated with this case.", styles["Body"]))

    story.extend(_section_heading("7. Human review history", styles))
    reviews = list(case.reviews)
    if reviews:
        review_rows = [
            [review.id, review.action, review.reviewer, _format_datetime(review.created_at), review.note]
            for review in reviews
        ]
        story.append(
            _data_table(
                ["Review ID", "Action", "Reviewer", "Recorded", "Note"],
                review_rows,
                [25 * mm, 23 * mm, 26 * mm, 32 * mm, 64 * mm],
                styles,
            )
        )
    else:
        story.append(_paragraph("No human review events have been stored for this case.", styles["Body"]))

    story.append(PageBreak())
    story.extend(_section_heading("8. Relevant audit events", styles))
    events = list(audit_events)[:_MAX_AUDIT_ROWS]
    if events:
        event_rows = [
            [
                event.id,
                event.entity_type,
                event.entity_id,
                event.event_type,
                _format_datetime(event.created_at),
                _safe_json(event.payload_json)[:300],
            ]
            for event in events
        ]
        story.append(
            _data_table(
                ["Event ID", "Entity type", "Entity ID", "Event", "Recorded", "Stored payload"],
                event_rows,
                [21 * mm, 19 * mm, 27 * mm, 27 * mm, 27 * mm, 53 * mm],
                styles,
            )
        )
    else:
        story.append(_paragraph("No scoped audit events have been stored for this case.", styles["Body"]))

    story.extend(_section_heading("9. Interpretation boundaries", styles))
    story.append(
        _paragraph(
            "Evidence scores and categories describe the system's recorded analytical output for the "
            "selected image pair. They are not legal conclusions or independently validated accuracy "
            "claims. Review source imagery, data quality, acquisition conditions, and the stored evidence "
            "before acting on this lead.",
            styles["Body"],
        )
    )
    story.append(Spacer(1, 3 * mm))
    story.append(
        _paragraph(
            f"Report generated at {_format_datetime(datetime.now(timezone.utc))}. "
            "Empty fields are shown as ‘Not recorded’; no values were inferred for this PDF.",
            styles["BodySmall"],
        )
    )

    document.build(story, onFirstPage=_draw_page_chrome, onLaterPages=_draw_page_chrome)
    return buffer.getvalue()
