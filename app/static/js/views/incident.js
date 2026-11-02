// Incident detail: the comparison, the verdict and everything an officer needs to act on it.

import { h, debounce, morph, setChildren } from "../dom.js";
import { api, asList } from "../api.js";
import { icon } from "../icons.js";
import {
  categoryLabel, channelLabel, cleanText, dispatchLine, errorText, formatArea, formatDate, formatDateTime, formatLatency, formatPercent, formatSigned,
  humanise, isNum, landcoverLabel, splitReference, statusLabel, verdictHeadline, verdictLabel, zoneTypeLabel,
} from "../format.js";
import { on, severityRank, store } from "../state.js";
import { createMap, fitTo, polygonStyle, HEALTH_COLOURS } from "../map.js";
import {
  emptyState, errorState, imageBox, illustrativeTag, kv, noteModal, relTime, replayBadge, severityTag, skeletonRows, statusTag, toast,
} from "../ui.js";
import { rainfallBars } from "../charts.js";
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

function rainfallBlock(rainfall, afterDate) {
  if (!rainfall || rainfall.available === false || !isNum(rainfall.total_mm)) {
    return h("div.context-block",
      h("h3.section-label", "Rainfall at site"),
      h("p.muted", "Rainfall record not available for this scene."));
  }
  const days = rainfall.window_days || rainfall.days_with_data;
  const endYear = (rainfall.end_date || afterDate || "").slice(0, 4);
  const rows = [
    { label: endYear || "This scene", value: Number(rainfall.total_mm), current: true },
    ...asList(rainfall.prior_years)
      .filter((row) => isNum(row.total_mm))
      .map((row) => ({ label: String(row.year), value: Number(row.total_mm), current: false })),
  ];
  const facts = [];
  if (isNum(rainfall.anomaly_percent)) {
    const pct = Math.round(Number(rainfall.anomaly_percent));
    facts.push(pct === 0 ? "In line with earlier years" : `${Math.abs(pct)}% ${pct > 0 ? "above" : "below"} the earlier-year average`);
  }
  if (isNum(rainfall.max_daily_mm)) facts.push(`Wettest day ${Number(rainfall.max_daily_mm).toFixed(0)} mm`);
  if (rainfall.source) facts.push(`Source: ${rainfall.source}`);
  const normal = isNum(rainfall.normal_mm) && rows.length > 1 ? Number(rainfall.normal_mm) : null;
  return h("div.context-block.context-rain",
    h("h3.section-label", days ? `Rainfall at site, ${days} days before the scene` : "Rainfall at site"),
    rainfallBars(rows, { normal }),
    normal !== null ? h("p.bars-key", h("span.bars-key-rule", { "aria-hidden": "true" }), `Earlier-year average ${normal.toFixed(0)} mm`) : null,
    facts.length ? h("p.context-detail", facts.join(" · ")) : null);
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
  let selectionLayer = null;
  let seasonalText = null;
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
    const findings = orderFindings(asList(detail.findings));
    detail.findings = findings;
    selectedFindingId = findings[0] ? findings[0].id : null;
    await loadAlerts();
    if (disposed) return;
    renderAll();
  }

  async function loadAlerts() {
    const alerts = asList(detail.alerts);
    // Repeat detections may link to an alert created in an earlier incident.
    const ids = new Set([...alerts.map((alert) => alert.id), ...asList(detail.findings).map((finding) => finding.alert_id).filter(Boolean)]);
    const loaded = (await Promise.all([...ids].map((id) => api.alert(id).catch(() => alerts.find((alert) => alert.id === id))))).filter(Boolean);
    alertDetails = new Map(loaded.map((alert) => [alert.id, alert]));
    // Region membership comes from the grouped alert; reasons and geometry come from its finding.
    const findings = new Map(asList(detail.findings).map((finding) => [finding.id, finding]));
    for (const alert of loaded) {
      for (const region of asList(alert.regions)) {
        const finding = findings.get(region.finding_id);
        if (finding) findings.set(finding.id, { ...finding, ...region, alert_id: alert.id });
      }
    }
    detail.findings = orderFindings([...findings.values()]);
  }

  function sortFindings(findings) {
    return [...findings].sort((a, b) => severityRank(b.severity) - severityRank(a.severity) || (Number(b.area_m2) || 0) - (Number(a.area_m2) || 0));
  }

  const currentFinding = () => asList(detail.findings).find((f) => f.id === selectedFindingId) || asList(detail.findings)[0] || null;

  const primaryAlert = () => [...alertDetails.values()].sort((a, b) => severityRank(b.severity) - severityRank(a.severity))[0] || null;

  /** The alert raised for the selected region. A seasonal region has none, and that is shown as such. */
  function currentAlert() {
    const finding = currentFinding();
    if (!finding) return primaryAlert();
    return alertForFinding(finding);
  }

  function alertForFinding(finding) {
    return alertDetails.get(finding.alert_id) || [...alertDetails.values()].find((alert) =>
      asList(alert.regions).some((region) => region.finding_id === finding.id) || alert.finding_id === finding.id) || null;
  }

  function selectFinding(id) {
    selectedFindingId = id;
    renderVerdict();
    renderActions();
    renderDispatch();
    renderRegions();
    syncSelection();
    const finding = currentFinding();
    if (smallMap && isNum(finding?.centroid_lat) && isNum(finding?.centroid_lon)) {
      smallMap.flyTo([Number(finding.centroid_lat), Number(finding.centroid_lon)], Math.max(15, smallMap.getZoom()), {
        animate: !window.matchMedia("(prefers-reduced-motion: reduce)").matches,
        duration: 0.6,
      });
    }
  }

  /** Regions with an alert lead; the rest sort by severity, then size. */
  function orderFindings(findings) {
    const alerts = alertDetails.size ? [...alertDetails.values()] : asList(detail.alerts);
    const alerted = new Set(alerts.flatMap((alert) => [alert.finding_id, ...asList(alert.regions).map((region) => region.finding_id)]));
    return sortFindings(findings).sort((a, b) => Number(Boolean(b.alert_id) || alerted.has(b.id)) - Number(Boolean(a.alert_id) || alerted.has(a.id)));
  }

  // ------------------------------------------------------ sections

  const headerHost = h("div");
  const verdictHost = h("div", {
    onclick: (event) => {
      const button = event.target.closest("[data-finding]");
      if (button) selectFinding(button.dataset.finding);
    },
  });
  const regionsHost = h("div", {
    onclick: (event) => {
      const row = event.target.closest("[data-finding]");
      if (row) selectFinding(row.dataset.finding);
    },
  });
  const actionsHost = h("div");
  const dispatchHost = h("div");
  const toolbarHost = h("div.cmp-toolbar");

  function renderHeader() {
    const area = detail.area || {};
    const alert = primaryAlert();
    const place = area.state || "";
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
        h("button.btn", { type: "button", id: "report-btn", onclick: (event) => downloadReport(event.currentTarget) }, icon("download"), "Download PDF report")));
    if (headerHost.firstElementChild) morph(headerHost.firstElementChild, next);
    else headerHost.appendChild(next);
  }

  /** Fetched first, so a report that cannot be rendered is an explained message, not a dead download. */
  async function downloadReport(button) {
    if (button.disabled) return;
    button.disabled = true;
    try {
      const { blob, filename } = await api.reportFile(detail.id);
      const url = URL.createObjectURL(blob);
      const link = h("a", { href: url, download: filename });
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (error) {
      toast(errorText(error, "The PDF report could not be prepared. Try again."), "error");
    } finally {
      button.disabled = false;
    }
  }

  function evidenceList(finding) {
    const reasons = asList(finding && finding.verdict_reasons);
    if (!reasons.length) return null;
    return h("ul.evidence", reasons.map((text) => h("li", icon("chevronRight", { size: 12 }), h("span", cleanText(text)))));
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
    const alertedFindings = asList(detail.findings).filter((item) => alertForFinding(item));

    const next = h("section.verdict", { dataset: { severity: String(severity || "none").toLowerCase() }, "aria-label": "Verdict" },
      h("div.verdict-top", severityTag(severity), alert ? statusTag(alert.status) : null),
      h("h2.verdict-title", verdictHeadline(verdict, category)),
      alert && Number(alert.region_count) > 1 ? h("p.muted", `${alert.region_count} regions · ${formatArea(alert.area_m2)} total`) : null,
      h("dl.verdict-facts",
        h("div.verdict-fact", h("dt", "Changed area"), h("dd", formatArea(areaM2) || "–")),
        h("div.verdict-fact", h("dt", "Confidence"), h("dd", formatPercent(confidence) || "–")),
        finding && isNum(finding.change_probability)
          ? h("div.verdict-fact", h("dt", "Change probability"), h("dd", formatPercent(finding.change_probability)))
          : null,
        alert && Number(alert.persistence_count) > 1
          ? h("div.verdict-fact", h("dt", "Seen in"), h("dd", `${alert.persistence_count} scenes`))
          : null),
      hasTransition ? transitionNode(transition) : null,
      alertedFindings.length > 1
        ? h("div.finding-switch", { role: "group", "aria-label": "Alerts in this incident" },
          alertedFindings.map((item) => h("button.seg", {
            type: "button",
            dataset: { finding: item.id },
            "aria-pressed": item.id === selectedFindingId ? "true" : "false",
          }, [categoryLabel(item.category) || "Region", formatArea(item.area_m2)].filter(Boolean).join(" · "))))
        : null,
      h("h3.section-label", "Why this verdict"),
      evidenceList(finding) || evidenceList(alert) || h("p.muted", "No reasons recorded for this finding."),
      indicesRow(indices),
      h("p.screening-note", "Screening result for department verification. A satellite cannot determine legality."));
    if (verdictHost.firstElementChild) morph(verdictHost.firstElementChild, next);
    else verdictHost.appendChild(next);
  }

  function transitionNode(transition) {
    const from = landcoverLabel(transition.from) || "Unknown";
    const to = landcoverLabel(transition.to) || "Unknown";
    if (from === to) {
      return h("div.transition", h("span.transition-class", to), h("span.muted", "Land cover class unchanged"));
    }
    return h("div.transition", { "aria-label": `Land cover changed from ${from} to ${to}` },
      h("span.transition-class", from),
      icon("arrowRight", { size: 14 }),
      h("span.transition-class", to));
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
      actionsHost.append(h("p.muted", "No alert is linked to this region, so there is no linked alert to act on."));
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
            h("td", deptName(row, alert), row.destination && row.destination !== deptName(row, alert) ? h("span.cell-sub.mono", row.destination) : null),
            h("td", channelLabel(row.channel)),
            h("td", dispatchLine(row), row.file ? h("a.cell-sub", { href: row.file, target: "_blank", rel: "noopener noreferrer" }, "Saved copy") : null),
            h("td", formatDateTime(row.at || row.created_at)),
            h("td.num", row.triggered_by === "manual" || String(row.channel).toUpperCase() === "IN_APP" ? "" : formatLatency(alert && alert.latency_seconds)))))))
        : h("p.panel-empty", alert ? "Nothing has been dispatched for this alert." : "No alert is linked to this region."));
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
    const dates = sceneDates();
    compare = createCompare({
      images,
      beforeDate: dates.before,
      afterDate: dates.after,
      bounds: detail.image_bounds,
    });
    const toggle = (label, layer, initial, available, after) => {
      if (!available) return null;
      const input = h("input", {
        type: "checkbox",
        onchange: () => {
          compare.setLayer(layer, input.checked);
          if (after) after(input.checked);
        },
      });
      input.checked = initial;
      return h("label.switch", input, h("span.switch-track", { "aria-hidden": "true" }), h("span", label));
    };
    const ndviLegend = h("span.ndvi-legend", { hidden: true, "aria-label": "NDVI change scale, from vegetation lost to vegetation gained" },
      h("span", "Vegetation lost"),
      h("span.ndvi-ramp", { "aria-hidden": "true" }),
      h("span", "Gained"));
    setChildren(toolbarHost,
      h("span.cmp-hint", "Drag, or use arrow keys on the handle"),
      ndviLegend,
      h("span.cmp-toggles",
        toggle("Change overlay", "overlay", true, compare.has.overlay),
        toggle("Selected region", "outline", true, compare.has.outline),
        toggle("NDVI change", "ndvi", false, compare.has.ndvi, (on) => {
          ndviLegend.hidden = !on;
        })));
    return compare.element;
  }

  function sceneDates() {
    return {
      before: detail.before_date || (detail.before_acquisition && detail.before_acquisition.date),
      after: detail.after_date || (detail.after_acquisition && detail.after_acquisition.date),
    };
  }

  function renderRegions() {
    const findings = asList(detail.findings);
    if (findings.length < 2) {
      regionsHost.replaceChildren();
      return;
    }
    const quiet = findings.filter((item) => !alertForFinding(item));
    const benign = quiet.filter((item) => item.verdict === "SEASONAL_BENIGN").length;
    const summary = benign
      ? `${benign} of ${findings.length} regions read as seasonal or crop cycle and raised no alert.`
      : `${findings.length} changed regions in this scene pair.`;
    const next = h("section.panel", { "aria-label": "Changed regions" },
      h("header.panel-head", h("h2.panel-title", "Changed regions"), h("span.panel-sub", summary)),
      h("div.table-scroll.regions-scroll", h("table.data-table.data-table-compact",
        h("thead", h("tr",
          h("th", { scope: "col" }, "Region"),
          h("th", { scope: "col" }, "Verdict"),
          h("th", { scope: "col" }, "Land cover"),
          h("th", { scope: "col", class: "num" }, "Area"),
          h("th", { scope: "col", class: "num" }, "Confidence"))),
        h("tbody", findings.map((item) => {
          const from = landcoverLabel(item.transition && item.transition.from);
          const to = landcoverLabel(item.transition && item.transition.to);
          return h("tr.row-link", { dataset: { finding: item.id }, class: item.id === selectedFindingId ? "is-selected" : null },
            h("td", h("button.link-btn", { type: "button", dataset: { finding: item.id }, "aria-pressed": item.id === selectedFindingId ? "true" : "false" },
              alertForFinding(item) ? h("span.region-alert", icon("bell", { size: 12 })) : null,
              categoryLabel(item.category) || "Region")),
            h("td", verdictLabel(item.verdict)),
            h("td", from && to ? (from === to ? to : `${from} to ${to}`) : ""),
            h("td.num", formatArea(item.area_m2)),
            h("td.num", formatPercent(item.confidence)));
        })))));
    if (regionsHost.firstElementChild) morph(regionsHost.firstElementChild, next);
    else regionsHost.appendChild(next);
  }

  function syncSelection() {
    const finding = currentFinding();
    if (seasonalText) seasonalText.textContent = seasonalSentence();
    if (compare) compare.setOutline(finding ? findingGeometry(finding) : null);
    if (smallMap && selectionLayer) {
      selectionLayer.eachLayer((layer) => {
        const selected = layer.findingId === selectedFindingId;
        layer.setStyle(selected
          ? polygonStyle(HEALTH_COLOURS.ALERT, { fillOpacity: 0.22, weight: 2 })
          : polygonStyle("#d7dde3", { fillOpacity: 0.04, weight: 1, opacity: 0.6 }));
        if (selected) layer.bringToFront();
      });
    }
  }

  function seasonalSentence() {
    const finding = currentFinding();
    const persists = finding ? finding.seasonal_persists : null;
    if (persists === true) return "Against this earlier same-season scene, this region still shows the change, so it does not read as a seasonal pattern.";
    if (persists === false) return "This region looks much the same as it did in this earlier same-season scene, which points to a seasonal pattern.";
    return "Earlier same-season reference scene, used to separate seasonal change from lasting change.";
  }

  function renderSeasonal() {
    const reference = detail.seasonal_reference;
    if (!reference || !reference.image) return null;
    seasonalText = h("p", seasonalSentence());
    return h("section.panel", { "aria-label": "Same-season comparison" },
      h("header.panel-head",
        h("h2.panel-title", "Same-season reference"),
        reference.date ? h("span.panel-sub", formatDate(reference.date)) : null),
      h("div.seasonal",
        imageBox(reference.image, { alt: `Same-season reference scene, ${formatDate(reference.date)}`, ratio: compareRatio(), className: "seasonal-image" }),
        h("div.seasonal-text",
          seasonalText,
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
    const disasters = asList(context.disasters);
    const protectedAreas = asList(context.protected_areas);
    const zones = asList(context.authorised_zones);
    const osm = asList(context.osm_features);
    const unavailable = new Set(asList(context.unavailable).map((item) => String((item && item.source) || item)));
    const disasterFeedsDown = [...unavailable].some((name) => /gdacs|eonet|disaster/i.test(name));

    const blocks = [];
    blocks.push(rainfallBlock(context.rainfall, sceneDates().after));
    blocks.push(h("div.context-block",
      h("h3.section-label", "Disaster events"),
      disasters.length
        ? h("ul.context-list", disasters.map((event) => contextItem(event.title || humanise(event.type) || "Reported event", [formatDate(event.date), isNum(event.distance_km) ? `${Number(event.distance_km).toFixed(0)} km away` : null], event.url)))
        : h("p.muted", disasterFeedsDown ? "Disaster feeds could not be reached for this scene." : "No reported event near this site in the period.")));
    blocks.push(h("div.context-block",
      h("h3.section-label", "Protected-area overlap"),
      protectedAreas.length
        ? h("ul.context-list", protectedAreas.map((area) => contextItem(area.name || "Unnamed protected area", ["OpenStreetMap"], osmUrl(area.osm_id))))
        : h("p.muted", unavailable.has("osm") ? "OpenStreetMap could not be reached for this scene." : "Does not overlap a mapped protected area.")));
    blocks.push(h("div.context-block",
      h("h3.section-label", "Authorised zones"),
      zones.length
        ? h("ul.context-list", zones.map((zone) => {
          const item = contextItem(zone.name || "Authorised zone", [zoneTypeLabel(zone.zone_type), zone.authority, splitReference(zone.reference).label], splitReference(zone.reference).url);
          if (zone.is_illustrative) item.appendChild(illustrativeTag());
          return item;
        }))
        : h("p.muted", "No authorisation on record at this location.")));
    if (osm.length) {
      blocks.push(h("div.context-block",
        h("h3.section-label", "Mapped features nearby"),
        h("ul.context-list", osm.slice(0, 8).map((feature) => contextItem(feature.name || humanise(feature.kind) || "Mapped feature", [feature.name ? humanise(feature.kind) : null], osmUrl(feature.osm_id))))));
    }
    return h("section.panel", { "aria-label": "Context" },
      h("header.panel-head", h("h2.panel-title", "Context")),
      h("div.context-grid", blocks));
  }

  function osmUrl(osmId) {
    return osmId && /^(way|relation|node)\/\d+$/.test(osmId) ? `https://www.openstreetmap.org/${osmId}` : null;
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
    const row = (label, acquisition) => {
      const product = acquisition && acquisition.product_id;
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
      row("Before scene", before),
      row("After scene", after),
      detail.seasonal_reference && detail.seasonal_reference.product_id ? kv("Same-season scene", detail.seasonal_reference.product_id, { mono: true }) : null,
      kv("Imagery source", detail.source),
      kv("Pixel resolution", isNum(detail.pixel_resolution_m) ? `${detail.pixel_resolution_m} m` : null),
      kv("Analysis method", String(detail.analysis_method || "").split("|").map((part) => humanise(part)).filter(Boolean).join(", ")),
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
      smallMap = createMap(host, { zoomControl: true, scrollWheelZoom: false });
    } catch {
      return;
    }
    const group = L().featureGroup();
    const drawBoundary = (geometry) => {
      if (geometry && smallMap) L().geoJSON(geometry, { interactive: false, style: () => polygonStyle("#c4cdd6", { fillOpacity: 0, weight: 1.5 }) }).addTo(group);
    };
    const areaId = (detail.area || {}).id;
    const known = store.areas.get(areaId);
    if (known) drawBoundary(known.geometry);
    else if (areaId) api.area(areaId).then((area) => { if (!disposed) drawBoundary(area.geometry); }).catch(() => {});
    selectionLayer = L().featureGroup();
    for (const finding of asList(detail.findings)) {
      const geometry = findingGeometry(finding);
      if (!geometry) continue;
      L().geoJSON(geometry).eachLayer((layer) => {
        layer.findingId = finding.id;
        layer.on("click", () => selectFinding(finding.id));
        selectionLayer.addLayer(layer);
      });
    }
    selectionLayer.addTo(group);
    group.addTo(smallMap);
    const bounds = group.getBounds();
    const area = detail.area;
    const fallback = area && Array.isArray(area.bbox) ? L().latLngBounds([area.bbox[1], area.bbox[0]], [area.bbox[3], area.bbox[2]]) : null;
    const target = bounds && bounds.isValid() ? bounds : fallback;
    if (target) requestAnimationFrame(() => {
      if (!disposed) fitTo(smallMap, target, { padding: [16, 16], maxZoom: 15 });
    });
    else smallMap.setView([22, 80], 5);
    syncSelection();
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
    renderRegions();
    const provenanceHost = h("div", renderProvenance());
    root.replaceChildren(h("div.page.page-wide.incident",
      headerHost,
      h("div.incident-top",
        h("div.incident-left",
          compareEl,
          toolbarHost),
        h("div.incident-right",
          verdictHost,
          h("div.action-host", actionsHost))),
      h("div.incident-lower",
        h("div.incident-lower-main",
          regionsHost,
          dispatchHost,
          renderSeasonal(),
          renderContext(),
          provenanceHost),
        h("div.incident-lower-side",
          h("section.panel", { "aria-label": "Location" },
            h("header.panel-head", h("h2.panel-title", "Location")),
            mapHost)))));
    mountSmallMap(mapHost);
    syncSelection();
    loadProvenance(provenanceHost);
  }

  /** Scene records come from the site's acquisition list, which carries the source links. */
  async function loadProvenance(host) {
    const areaId = (detail.area && detail.area.id) || detail.aoi_id;
    if (!areaId) return;
    try {
      const rows = asList(await api.acquisitions(areaId));
      if (disposed) return;
      const byId = new Map(rows.map((row) => [row.id, row]));
      detail.before_acquisition = byId.get(detail.before_acquisition_id) || null;
      detail.after_acquisition = byId.get(detail.after_acquisition_id) || null;
      const wasOpen = host.firstElementChild && host.firstElementChild.open;
      const next = renderProvenance();
      next.open = Boolean(wasOpen);
      host.replaceChildren(next);
    } catch {
      // Scene dates are still shown; only the deep links are missing.
    }
  }

  const refreshAlerts = debounce(async () => {
    if (disposed || !detail) return;
    await loadAlerts();
    if (disposed) return;
    renderHeader();
    renderVerdict();
    renderActions();
    renderDispatch();
    renderRegions();
  }, 250);

  cleanups.push(on("dispatch-event", (event) => {
    if (detail && alertDetails.has(event.alert_id)) refreshAlerts();
  }));
  cleanups.push(on("alerts", () => {
    if (!detail) return;
    for (const [id, local] of alertDetails) {
      const fresh = store.alerts.get(id);
      if (fresh && ["status", "region_count", "area_m2", "persistence_count", "last_seen_at"].some((key) => fresh[key] !== local[key])) {
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

