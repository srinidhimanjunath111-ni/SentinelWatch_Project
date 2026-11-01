// Incident detail: the comparison, the verdict and everything an officer needs to act on it.

import { h, debounce, morph, setChildren } from "../dom.js";
import { api, asList } from "../api.js";
import { icon } from "../icons.js";
import {
  categoryLabel, channelLabel, dispatchStatusLabel, errorText, formatArea, formatDate, formatDateTime, formatLatency, formatPercent, formatSigned,
  isNum, landcoverLabel, statusLabel, verdictHeadline, zoneTypeLabel,
} from "../format.js";
import { on, severityRank, store } from "../state.js";
import { createMap, fitTo, geometryBounds, polygonStyle, HEALTH_COLOURS } from "../map.js";
import {
  emptyState, errorState, imageBox, illustrativeTag, kv, noteModal, relTime, replayBadge, severityTag, skeletonRows, statusTag, toast,
} from "../ui.js";
import { rainfallChart } from "../charts.js";
import { createCompare } from "./compare.js";

const L = () => window.L;

const TRANSITIONS = {
  open: ["acknowledged", "resolved", "false_positive"],
  dispatched: ["acknowledged", "field_verification", "resolved", "false_positive"],
  acknowledged: ["field_verification", "resolved", "false_positive"],
  field_verification: ["resolved", "false_positive"],
  resolved: [],
  false_positive: [],
};

const ACTIONS = {
  acknowledged: { label: "Acknowledge", iconName: "check", primary: true, title: "Acknowledge alert", message: "Records that the department has taken this alert." },
  field_verification: { label: "Send to field verification", iconName: "flag", title: "Send to field verification", message: "Records that a field team has been asked to verify this on the ground.", placeholder: "Team, range office or instructions" },
  resolved: { label: "Resolve", iconName: "circleCheck", title: "Resolve alert", message: "Closes the alert. Note what was found.", placeholder: "Outcome of verification", required: true },
  false_positive: { label: "Mark false positive", iconName: "x", title: "Mark as false positive", message: "Closes the alert and records that no real change was found.", placeholder: "Why this is not a real change", required: true, danger: true },
};

function parseRainfall(rainfall) {
  if (!rainfall || typeof rainfall !== "object") return [];
  const series = rainfall.daily ?? rainfall.series ?? rainfall.days ?? rainfall.values;
  if (Array.isArray(series)) {
    return series
      .map((row) => ({
        date: row.date ?? row.day ?? row.time,
        value: Number(row.mm ?? row.value ?? row.rain_mm ?? row.precipitation_mm ?? row.precipitation),
      }))
      .filter((row) => row.date && Number.isFinite(row.value));
  }
  if (Array.isArray(rainfall.dates) && Array.isArray(rainfall.mm)) {
    return rainfall.dates.map((date, index) => ({ date, value: Number(rainfall.mm[index]) })).filter((row) => Number.isFinite(row.value));
  }
  if (series && Array.isArray(series.time)) {
    const values = series.precipitation_sum || series.rain_sum || series.mm || [];
    return series.time.map((date, index) => ({ date, value: Number(values[index]) })).filter((row) => Number.isFinite(row.value));
  }
  return [];
}

function normaliseDispatches(alert) {
  return asList(alert && (alert.dispatches || alert.notifications)).slice();
}

function findingGeometry(finding) {
  if (finding.geometry && finding.geometry.type) return finding.geometry;
  if (finding.geometry_json) {
    try {
      return JSON.parse(finding.geometry_json);
    } catch {
      return null;
    }
  }
  return null;
}

export async function mountIncident(root, params) {
  let disposed = false;
  let detail = null;
  let selectedFindingId = null;
  let alertDetails = new Map();
  let compare = null;
  let smallMap = null;
  const cleanups = [];

  root.replaceChildren(h("div.page.page-wide", skeletonRows(2), h("div.skeleton.skeleton-hero")));

  async function load() {
    try {
      detail = await api.case(params.id);
    } catch (error) {
      if (disposed) return;
      root.replaceChildren(h("div.page", error.status === 404
        ? emptyState("This incident no longer exists", "It may have been removed.", h("a.btn", { href: "#/alerts" }, "All alerts"))
        : errorState(error, load, "Could not load this incident")));
      return;
    }
    if (disposed) return;
    const findings = sortFindings(asList(detail.findings));
    detail.findings = findings;
    selectedFindingId = findings[0] ? findings[0].id : null;
    await loadAlerts();
    if (disposed) return;
    renderAll();
  }

  async function loadAlerts() {
    const alerts = asList(detail.alerts);
    const loaded = await Promise.all(alerts.map((alert) => api.alert(alert.id).catch(() => alert)));
    alertDetails = new Map(loaded.map((alert) => [alert.id, alert]));
  }

  function sortFindings(findings) {
    return [...findings].sort((a, b) => severityRank(b.severity) - severityRank(a.severity) || (Number(b.area_m2) || 0) - (Number(a.area_m2) || 0));
  }

  const currentFinding = () => asList(detail.findings).find((f) => f.id === selectedFindingId) || asList(detail.findings)[0] || null;

  /** The alert belonging to the selected finding; else the most severe alert on the case. */
  function currentAlert() {
    const finding = currentFinding();
    const all = [...alertDetails.values()];
    if (finding) {
      const match = all.find((alert) => alert.finding_id === finding.id);
      if (match) return match;
    }
    return all.sort((a, b) => severityRank(b.severity) - severityRank(a.severity))[0] || null;
  }

  // ------------------------------------------------------ sections

  const headerHost = h("div");
  const verdictHost = h("div");
  const actionsHost = h("div");
  const dispatchHost = h("div");
  const toolbarHost = h("div.cmp-toolbar");

  function renderHeader() {
    const area = detail.area || {};
    const alert = currentAlert();
    const place = [area.district, area.state].filter(Boolean).join(", ") || detail.location;
    const next = h("header.page-head",
      h("div.page-head-main",
        h("nav.crumbs", { "aria-label": "Breadcrumb" },
          h("a", { href: area.id ? `#/site/${encodeURIComponent(area.id)}` : "#/alerts" }, icon("chevronLeft", { size: 12 }), area.name || "Alerts")),
        h("h1.page-title", area.name || detail.title || "Incident"),
        h("div.page-meta",
          place ? h("span", place) : null,
          alert && alert.acquisition_date ? h("span", `Scene ${formatDate(alert.acquisition_date)}`) : null,
          alert ? relTime(alert.detected_at || alert.created_at) : relTime(detail.created_at),
          replayBadge(detail) || replayBadge(alert))),
      h("div.page-actions",
        h("a.btn", { href: api.reportUrl(detail.id), download: true }, icon("download"), "Download PDF report")));
    if (headerHost.firstElementChild) morph(headerHost.firstElementChild, next);
    else headerHost.appendChild(next);
  }

  function evidenceList(finding) {
    const reasons = asList(finding && finding.verdict_reasons);
    if (!reasons.length) return null;
    return h("ul.evidence", reasons.map((text) => h("li", icon("chevronRight", { size: 12 }), h("span", text))));
  }

  function renderVerdict() {
    const finding = currentFinding();
    const alert = currentAlert();
    const severity = (finding && finding.severity) || (alert && alert.severity) || detail.highest_severity;
    const category = (finding && finding.category) || (alert && alert.category);
    const verdict = (finding && finding.verdict) || (alert && alert.verdict);
    const confidence = finding && isNum(finding.confidence) ? finding.confidence : alert ? alert.confidence : detail.overall_confidence;
    const areaM2 = finding && isNum(finding.area_m2) ? finding.area_m2 : alert ? alert.area_m2 : detail.changed_area_m2;
    const transition = finding && finding.transition;
    const hasTransition = transition && (transition.from || transition.to);
    const indices = (finding && finding.indices) || {};
    const findings = asList(detail.findings);

    const next = h("section.verdict", { dataset: { severity: String(severity || "none").toLowerCase() }, "aria-label": "Verdict" },
      h("div.verdict-top", severityTag(severity), alert ? statusTag(alert.status) : null),
      h("h2.verdict-title", verdictHeadline(verdict, category)),
      h("dl.verdict-facts",
        h("div.verdict-fact", h("dt", "Changed area"), h("dd", formatArea(areaM2) || "–")),
        h("div.verdict-fact", h("dt", "Confidence"), h("dd", formatPercent(confidence) || "–")),
        finding && isNum(finding.change_probability)
          ? h("div.verdict-fact", h("dt", "Change probability"), h("dd", formatPercent(finding.change_probability)))
          : null,
        alert && Number(alert.persistence_count) > 1
          ? h("div.verdict-fact", h("dt", "Seen in"), h("dd", `${alert.persistence_count} scenes`))
          : null),
      hasTransition
        ? h("div.transition", { "aria-label": `Land cover changed from ${landcoverLabel(transition.from)} to ${landcoverLabel(transition.to)}` },
          h("span.transition-class", landcoverLabel(transition.from) || "Unknown"),
          icon("arrowRight", { size: 14 }),
          h("span.transition-class", landcoverLabel(transition.to) || "Unknown"))
        : null,
      findings.length > 1
        ? h("div.finding-switch", { role: "group", "aria-label": "Findings in this incident" },
          findings.map((item, index) => h("button.seg", {
            type: "button",
            "aria-pressed": item.id === selectedFindingId ? "true" : "false",
            onclick: () => {
              selectedFindingId = item.id;
              renderVerdict();
              renderActions();
              renderDispatch();
            },
          }, `${index + 1}. ${categoryLabel(item.category) || "Finding"}`)))
        : null,
      h("h3.section-label", "Why this verdict"),
      evidenceList(finding) || h("p.muted", "No reasons recorded for this finding."),
      indicesRow(indices),
      h("p.screening-note", "Screening result for department verification. A satellite cannot determine legality."));
    if (verdictHost.firstElementChild) morph(verdictHost.firstElementChild, next);
    else verdictHost.appendChild(next);
    // Finding buttons have per-item handlers, so they need rebinding after a morph.
    verdictHost.querySelectorAll(".finding-switch .seg").forEach((button, index) => {
      button.onclick = () => {
        selectedFindingId = findings[index].id;
        renderVerdict();
        renderActions();
        renderDispatch();
      };
    });
  }

  function indicesRow(indices) {
    const rows = [
      ["NDVI", indices.ndvi_delta],
      ["NBR", indices.nbr_delta],
      ["NDWI", indices.ndwi_delta],
      ["BSI", indices.bsi_delta],
      ["NDBI", indices.ndbi_delta],
    ].filter(([, value]) => isNum(value));
    if (!rows.length) return null;
    return h("dl.index-row", { title: "Change in spectral index, after minus before" },
      rows.map(([label, value]) => h("div", h("dt", `Δ${label}`), h("dd", formatSigned(value)))));
  }

  function renderActions() {
    actionsHost.replaceChildren();
    const alert = currentAlert();
    if (!alert) {
      actionsHost.append(h("p.muted", "No alert was raised for this change, so there is nothing to act on."));
      return;
    }
    const available = TRANSITIONS[alert.status] || [];
    if (!available.length) {
      actionsHost.append(h("p.lifecycle-done", icon("circleCheck"), `${statusLabel(alert.status)}.`,
        alert.status_note || alert.acknowledgement_note ? ` ${alert.status_note || alert.acknowledgement_note}` : ""));
      return;
    }
    actionsHost.append(h("div.action-row", available.map((status) => {
      const spec = ACTIONS[status];
      return h("button.btn", {
        type: "button",
        class: spec.primary ? "btn-primary" : spec.danger ? "btn-ghost-danger" : null,
        onclick: () => runAction(alert.id, status),
      }, icon(spec.iconName), spec.label);
    })));
  }

  async function runAction(alertId, status) {
    const spec = ACTIONS[status];
    const result = await noteModal({
      title: spec.title,
      message: spec.message,
      confirmLabel: spec.label,
      placeholder: spec.placeholder || "",
      required: Boolean(spec.required),
      danger: Boolean(spec.danger),
      submit: async ({ note, actor }) => {
        const updated = await api.setAlertStatus(alertId, { status, note, actor });
        alertDetails.set(alertId, { ...alertDetails.get(alertId), ...updated });
      },
    });
    if (result === undefined || disposed) return;
    toast(`Alert marked ${statusLabel(status).toLowerCase()}.`, "ok");
    renderHeader();
    renderVerdict();
    renderActions();
    renderDispatch();
    refreshAlerts();
  }

  function renderDispatch() {
    const alert = currentAlert();
    const rows = normaliseDispatches(alert).sort((a, b) => (Date.parse(b.at || b.created_at || "") || 0) - (Date.parse(a.at || a.created_at || "") || 0));
    const next = h("section.panel", { "aria-label": "Dispatch log" },
      h("header.panel-head",
        h("h2.panel-title", "Dispatch log"),
        alert && alert.department ? h("span.panel-sub", typeof alert.department === "string" ? alert.department : alert.department.name) : null,
        alert ? h("button.btn.btn-sm.btn-ghost", {
          type: "button",
          onclick: async () => {
            try {
              await api.redispatch(currentAlert().id);
              toast("Dispatch sent again.", "ok");
              refreshAlerts();
            } catch (error) {
              toast(errorText(error, "Dispatch could not be sent."), "error");
            }
          },
        }, icon("send", { size: 14 }), "Dispatch again") : null),
      rows.length
        ? h("div.table-scroll", h("table.data-table.data-table-compact",
          h("thead", h("tr",
            h("th", { scope: "col" }, "Department"),
            h("th", { scope: "col" }, "Channel"),
            h("th", { scope: "col" }, "Status"),
            h("th", { scope: "col" }, "Sent"),
            h("th", { scope: "col", class: "num" }, "Latency"))),
          h("tbody", rows.map((row) => h("tr",
            h("td", deptName(row, alert), row.destination ? h("span.cell-sub.mono", row.destination) : null),
            h("td", channelLabel(row.channel)),
            h("td", dispatchStatusLabel(row.status), row.detail ? h("span.cell-sub", row.detail) : null),
            h("td", formatDateTime(row.at || row.created_at)),
            h("td.num", formatLatency(row.latency_seconds ?? (alert && alert.latency_seconds))))))))
        : h("p.panel-empty", alert ? "Nothing has been dispatched for this alert." : "No alert was raised, so nothing was dispatched."));
    if (dispatchHost.firstElementChild) morph(dispatchHost.firstElementChild, next);
    else dispatchHost.appendChild(next);
  }

  function deptName(row, alert) {
    const dept = row.department;
    if (dept) return typeof dept === "string" ? dept : dept.name;
    if (row.department_name) return row.department_name;
    if (alert && alert.department) return typeof alert.department === "string" ? alert.department : alert.department.name;
    return "";
  }

  function renderCompare() {
    const images = detail.images || {
      before: detail.before_image,
      after: detail.after_image,
      overlay: detail.mask_image,
    };
    const polygons = asList(detail.findings).map(findingGeometry).filter(Boolean);
    const dates = sceneDates();
    compare = createCompare({
      images,
      beforeDate: dates.before,
      afterDate: dates.after,
      bounds: detail.image_bounds,
      polygons,
    });
    const toggle = (label, layer, initial, available) => {
      if (!available) return null;
      const input = h("input", { type: "checkbox", onchange: () => compare.setLayer(layer, input.checked) });
      input.checked = initial;
      return h("label.switch", input, h("span.switch-track", { "aria-hidden": "true" }), h("span", label));
    };
    setChildren(toolbarHost,
      h("span.cmp-hint", "Drag, or use arrow keys on the handle"),
      h("span.cmp-toggles",
        toggle("Change overlay", "overlay", true, compare.has.overlay),
        toggle("Outline", "outline", true, compare.has.outline),
        toggle("NDVI change", "ndvi", false, compare.has.ndvi)));
    return compare.element;
  }

  function sceneDates() {
    return {
      before: detail.before_date || (detail.before_acquisition && detail.before_acquisition.date),
      after: detail.after_date || (detail.after_acquisition && detail.after_acquisition.date),
    };
  }

  function renderSeasonal() {
    const reference = detail.seasonal_reference;
    if (!reference || !reference.image) return null;
    const persists = asList(detail.findings).map((finding) => finding.seasonal_persists).find((value) => value === true || value === false);
    return h("section.panel", { "aria-label": "Same-season comparison" },
      h("header.panel-head",
        h("h2.panel-title", "Same-season reference"),
        reference.date ? h("span.panel-sub", formatDate(reference.date)) : null),
      h("div.seasonal",
        imageBox(reference.image, { alt: `Same-season reference scene, ${formatDate(reference.date)}`, ratio: compareRatio(), className: "seasonal-image" }),
        h("div.seasonal-text",
          persists === true ? h("p", "The change is not present in the same season a year earlier, so it is not a seasonal pattern.") : null,
          persists === false ? h("p", "The same season a year earlier looks similar, which points to a seasonal pattern.") : null,
          persists === null || persists === undefined ? h("p.muted", "Reference scene from about a year earlier, used to separate seasonal change from lasting change.") : null,
          reference.product_id ? h("p.mono.small-break", reference.product_id) : null)));
  }

  function compareRatio() {
    const bounds = detail.image_bounds;
    if (Array.isArray(bounds) && bounds.length === 2) {
      const [[s, w], [n, e]] = bounds.map((pair) => pair.map(Number));
      const width = (e - w) * Math.cos(((s + n) / 2) * (Math.PI / 180));
      const height = n - s;
      if (width > 0 && height > 0) return `${width.toFixed(4)} / ${height.toFixed(4)}`;
    }
    return "4 / 3";
  }

  function renderContext() {
    const context = detail.context || {};
    const rainfall = context.rainfall || null;
    const rain = parseRainfall(rainfall);
    const disasters = asList(context.disasters);
    const protectedAreas = asList(context.protected_areas);
    const zones = asList(context.authorised_zones);
    const osm = asList(context.osm_features);
    const dates = sceneDates();
    const markers = [];
    if (dates.before) markers.push({ date: dates.before, label: "Before" });
    if (dates.after) markers.push({ date: dates.after, label: "After" });
    const totalMm = rainfall && isNum(rainfall.total_mm) ? Number(rainfall.total_mm) : rain.reduce((sum, row) => sum + row.value, 0);

    const blocks = [];
    if (rain.length) {
      blocks.push(h("div.context-block.context-rain",
        h("h3.section-label", "Rainfall at site", h("span.section-sub", `${totalMm.toFixed(0)} mm across ${rain.length} days`)),
        rainfallChart(rain, { markers })));
    }
    blocks.push(h("div.context-block",
      h("h3.section-label", "Disaster events"),
      disasters.length
        ? h("ul.context-list", disasters.map((event) => contextItem(event.title || event.name || event.type, [event.date || event.at ? formatDate(event.date || event.at) : null, event.source], event.url)))
        : h("p.muted", "No reported event near this site in the period.")));
    blocks.push(h("div.context-block",
      h("h3.section-label", "Protected-area overlap"),
      protectedAreas.length
        ? h("ul.context-list", protectedAreas.map((area) => contextItem(area.name || "Protected area", [area.designation || area.type, isNum(area.overlap_m2) ? `${formatArea(area.overlap_m2)} overlap` : null, isNum(area.distance_m) ? `${(area.distance_m / 1000).toFixed(1)} km away` : null], area.url)))
        : h("p.muted", "Does not overlap a mapped protected area.")));
    blocks.push(h("div.context-block",
      h("h3.section-label", "Authorised zones"),
      zones.length
        ? h("ul.context-list", zones.map((zone) => {
          const item = contextItem(zone.name || "Authorised zone", [zoneTypeLabel(zone.zone_type), zone.authority, zone.reference]);
          if (zone.is_illustrative) item.appendChild(illustrativeTag());
          return item;
        }))
        : h("p.muted", "No authorisation on record at this location.")));
    if (osm.length) {
      blocks.push(h("div.context-block",
        h("h3.section-label", "Mapped features nearby"),
        h("ul.context-list", osm.slice(0, 8).map((feature) => contextItem(feature.name || feature.type || feature.kind || "Feature", [feature.tag || feature.category])))));
    }
    return h("section.panel", { "aria-label": "Context" },
      h("header.panel-head", h("h2.panel-title", "Context")),
      h("div.context-grid", blocks));
  }

  function contextItem(title, details, url) {
    const titleNode = url && /^https?:/i.test(url)
      ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, title, icon("external", { size: 12 }))
      : h("span", title);
    return h("li", h("span.context-title", titleNode), h("span.context-detail", details.filter(Boolean).join(" · ")));
  }

  function renderProvenance() {
    const before = detail.before_acquisition || null;
    const after = detail.after_acquisition || null;
    const row = (label, acquisition, productId) => {
      const product = (acquisition && acquisition.product_id) || productId;
      if (!product) return null;
      const url = acquisition && acquisition.source_url;
      return kv(label, null, {
        node: h("span.prov-value",
          url && /^https?:/i.test(url)
            ? h("a.mono", { href: url, target: "_blank", rel: "noopener noreferrer" }, product, icon("external", { size: 12 }))
            : h("span.mono", product)),
      });
    };
    const rows = [
      row("Before scene", before, detail.before_product_id),
      row("After scene", after, detail.after_product_id),
      detail.seasonal_reference && detail.seasonal_reference.product_id ? kv("Same-season scene", detail.seasonal_reference.product_id, { mono: true }) : null,
      kv("Imagery source", detail.source),
      kv("Pixel resolution", isNum(detail.pixel_resolution_m) ? `${detail.pixel_resolution_m} m` : null),
      kv("Analysis method", detail.analysis_method),
      kv("Model", detail.model_version_label),
      kv("Incident ID", detail.id, { mono: true }),
      kv("Recorded", formatDateTime(detail.created_at)),
    ].filter(Boolean);
    return h("details.panel.details-panel",
      h("summary.panel-head", h("h2.panel-title", "Scene IDs and provenance"), icon("chevronRight", { size: 14, className: "details-caret" })),
      h("dl.kv-list", rows));
  }

  function mountSmallMap(host) {
    if (!L()) return;
    try {
      smallMap = createMap(host, { labels: true, zoomControl: true, scrollWheelZoom: false });
    } catch {
      return;
    }
    const findings = asList(detail.findings).map(findingGeometry).filter(Boolean);
    const group = L().featureGroup();
    const areaGeometry = store.areas.get((detail.area || {}).id)?.geometry;
    if (areaGeometry) L().geoJSON(areaGeometry, { style: () => polygonStyle("#c4cdd6", { fillOpacity: 0, weight: 1.5, dashArray: null }) }).addTo(group);
    for (const geometry of findings) L().geoJSON(geometry, { style: () => polygonStyle(HEALTH_COLOURS.ALERT, { fillOpacity: 0.2 }) }).addTo(group);
    group.addTo(smallMap);
    const bounds = findings.length
      ? findings.map(geometryBounds).filter(Boolean).reduce((acc, item) => acc.extend(item), L().latLngBounds([]))
      : group.getBounds();
    const area = detail.area;
    const fallback = area && Array.isArray(area.bbox) ? L().latLngBounds([area.bbox[1], area.bbox[0]], [area.bbox[3], area.bbox[2]]) : null;
    const target = bounds && bounds.isValid() ? bounds : fallback;
    if (target) requestAnimationFrame(() => {
      if (!disposed) fitTo(smallMap, target, { padding: [28, 28], maxZoom: 15 });
    });
    else smallMap.setView([22, 80], 5);
  }

  function renderAll() {
    const mapHost = h("div.incident-map", { role: "img", "aria-label": "Changed area on satellite base map" });
    headerHost.replaceChildren();
    verdictHost.replaceChildren();
    dispatchHost.replaceChildren();
    renderHeader();
    const compareEl = renderCompare();
    renderVerdict();
    renderActions();
    renderDispatch();
    root.replaceChildren(h("div.page.page-wide.incident",
      headerHost,
      h("div.incident-top",
        h("div.incident-left",
          h("div.cmp-wrap", compareEl),
          toolbarHost),
        h("div.incident-right",
          verdictHost,
          h("div.action-host", actionsHost))),
      h("div.incident-lower",
        h("div.incident-lower-main",
          renderSeasonal(),
          renderContext(),
          dispatchHost,
          renderProvenance()),
        h("div.incident-lower-side",
          h("section.panel", { "aria-label": "Location" },
            h("header.panel-head", h("h2.panel-title", "Location")),
            mapHost)))));
    mountSmallMap(mapHost);
  }

  const refreshAlerts = debounce(async () => {
    if (disposed || !detail) return;
    await loadAlerts();
    if (disposed) return;
    renderHeader();
    renderVerdict();
    renderActions();
    renderDispatch();
  }, 250);

  cleanups.push(on("dispatch-event", (event) => {
    if (detail && alertDetails.has(event.alert_id)) refreshAlerts();
  }));
  cleanups.push(on("alerts", () => {
    if (!detail) return;
    for (const [id, local] of alertDetails) {
      const fresh = store.alerts.get(id);
      if (fresh && fresh.status !== local.status) {
        refreshAlerts();
        break;
      }
    }
  }));

  load();

  return () => {
    disposed = true;
    refreshAlerts.cancel();
    cleanups.forEach((off) => off());
    if (smallMap) smallMap.remove();
  };
}

