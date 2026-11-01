const $ = (selector) => document.querySelector(selector);
const areaDialog = $("#area-dialog");
const priorityDialog = $("#priority-dialog");
const detailDialog = $("#detail-dialog");
const FINAL_RUN_STATES = new Set([
  "COMPLETED", "COMPLETED_NO_SIGNIFICANT_CHANGE", "NO_NEW_ACQUISITION",
  "NO_SUITABLE_ACQUISITION", "SKIPPED_PAUSED", "BLOCKED_CREDENTIALS",
  "BLOCKED_DEPENDENCY", "UNSUITABLE_IMAGERY", "FAILED"
]);

let aoiMap;
let drawnItems;
let activeRunIds = new Set();
let configuredDepartments = [];

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}

function formatArea(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  if (numeric >= 1_000_000) return `${(numeric / 1_000_000).toFixed(2)} km²`;
  if (numeric >= 10_000) return `${(numeric / 10_000).toFixed(2)} ha`;
  return `${Math.round(numeric).toLocaleString()} m²`;
}

function formatDate(value) {
  if (!value) return "not checked yet";
  const date = new Date(value.endsWith("Z") ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function severityClass(value) { return `severity-${String(value || "none").toLowerCase()}`; }
function stateClass(value) { return `run-${String(value || "unknown").toLowerCase().replaceAll("_", "-")}`; }

function showToast(message, duration = 3800) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), duration);
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = "Request failed.";
    try { detail = (await response.json()).detail || detail; } catch (_) { /* no JSON */ }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

function areaGeometryFromLayer(layer) {
  const feature = layer.toGeoJSON();
  return feature.geometry;
}

function geometryToBbox(geometry) {
  const points = [];
  const visit = (value) => {
    if (!Array.isArray(value)) return;
    if (typeof value[0] === "number") points.push(value);
    else value.forEach(visit);
  };
  visit(geometry.coordinates);
  if (!points.length) throw new Error("The AOI does not contain valid coordinates.");
  const longs = points.map((point) => point[0]);
  const lats = points.map((point) => point[1]);
  return [Math.min(...longs), Math.min(...lats), Math.max(...longs), Math.max(...lats)];
}

function updateGeometryFields(geometry) {
  const bbox = geometryToBbox(geometry);
  $("#geometry-json").value = JSON.stringify(geometry);
  $("#bbox-input").value = bbox.map((value) => value.toFixed(6)).join(", ");
  $("#area-centre").value = `${((bbox[1] + bbox[3]) / 2).toFixed(6)}, ${((bbox[0] + bbox[2]) / 2).toFixed(6)}`;
  $("#map-help").textContent = "AOI ready. Its WGS84 GeoJSON and bounding box will be stored with every provider query.";
}

function resetMapLayers() {
  if (drawnItems) drawnItems.clearLayers();
}

function addGeometryToMap(geometry, zoom = true) {
  if (!aoiMap || !window.L) return;
  resetMapLayers();
  const layer = window.L.geoJSON(geometry);
  layer.eachLayer((item) => drawnItems.addLayer(item));
  if (zoom) {
    const bounds = layer.getBounds();
    if (bounds.isValid()) aoiMap.fitBounds(bounds.pad(0.18));
  }
}

function initialiseMap() {
  if (aoiMap || !window.L) {
    if (!window.L) $("#map-help").textContent = "Map library is unavailable. Paste GeoJSON or enter a WGS84 bounding box below.";
    return;
  }
  aoiMap = window.L.map("aoi-map", { zoomControl: true }).setView([20, 0], 2);
  window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap contributors"
  }).addTo(aoiMap);
  drawnItems = new window.L.FeatureGroup();
  aoiMap.addLayer(drawnItems);
  aoiMap.addControl(new window.L.Control.Draw({
    edit: { featureGroup: drawnItems, remove: true },
    draw: {
      polygon: { allowIntersection: false, showArea: true },
      rectangle: { showArea: true },
      polyline: false, circle: false, circlemarker: false, marker: false
    }
  }));
  const onLayerChange = (event) => {
    if (event.layer) {
      resetMapLayers();
      drawnItems.addLayer(event.layer);
    }
    const layers = drawnItems.getLayers();
    if (layers[0]) updateGeometryFields(areaGeometryFromLayer(layers[0]));
  };
  aoiMap.on(window.L.Draw.Event.CREATED, onLayerChange);
  aoiMap.on(window.L.Draw.Event.EDITED, onLayerChange);
  aoiMap.on(window.L.Draw.Event.DELETED, () => {
    $("#geometry-json").value = "";
    $("#bbox-input").value = "";
    $("#area-centre").value = "";
    $("#map-help").textContent = "Draw a new polygon/rectangle or use the GeoJSON/bbox fallback.";
  });
}

function bboxToGeometry(text) {
  const bbox = text.split(",").map((value) => Number(value.trim()));
  if (bbox.length !== 4 || bbox.some((value) => !Number.isFinite(value))) {
    throw new Error("Enter bbox as four numeric values: west, south, east, north.");
  }
  const [west, south, east, north] = bbox;
  if (west >= east || south >= north || west < -180 || east > 180 || south < -90 || north > 90) {
    throw new Error("The bounding box must be valid WGS84 west, south, east, north coordinates.");
  }
  return { type: "Polygon", coordinates: [[[west, south], [east, south], [east, north], [west, north], [west, south]]] };
}

function getFormGeometry() {
  const text = $("#geometry-json").value.trim();
  if (text) {
    try {
      const geojson = JSON.parse(text);
      return geojson.type === "Feature" ? geojson.geometry : geojson;
    } catch (_) {
      throw new Error("GeoJSON could not be parsed. Draw on the map or correct the JSON.");
    }
  }
  const bbox = $("#bbox-input").value.trim();
  if (bbox) return bboxToGeometry(bbox);
  throw new Error("Draw an AOI, paste GeoJSON, or enter a bounding box.");
}

function renderMetrics(metrics) {
  $("#metric-areas").textContent = metrics.monitored_areas;
  $("#metric-areas-sub").textContent = `${metrics.active_areas} active · ${metrics.completed_monitoring_runs} completed run(s)`;
  $("#metric-alerts").textContent = metrics.open_alerts;
  $("#metric-high").textContent = metrics.high_priority_alerts;
  $("#metric-area").textContent = formatArea(metrics.changed_area_m2);
}

function renderSystemStatus(status) {
  const holder = $("#system-status");
  const flags = [
    [status.imagery_credentials_configured, "CDSE imagery credentials"],
    [status.rasterio_available, "GeoTIFF processor"],
    [status.model_status === "READY", `Model ${status.model_status}`],
    [status.scheduler_enabled, "Background scheduler"]
  ];
  holder.className = `system-status ${status.imagery_credentials_configured && status.rasterio_available ? "ready" : "attention"}`;
  holder.innerHTML = `
    <div class="system-status-head"><span class="status-orb"></span><strong>${escapeHtml(status.provider)} · ${escapeHtml(status.model_status)}</strong><span class="system-device">${escapeHtml(status.device)}</span></div>
    <p>${escapeHtml(status.message)}</p>
    <div class="readiness-list">${flags.map(([enabled, label]) => `<span class="readiness ${enabled ? "on" : "off"}">${enabled ? "✓" : "!"} ${escapeHtml(label)}</span>`).join("")}</div>`;
  $("#demo-banner").hidden = !status.demo_mode;
}

function formatMetric(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${(numeric * 100).toFixed(1)}%` : "not recorded";
}

function renderModels(models) {
  const holder = $("#model-record");
  const model = models[0];
  if (!model) {
    holder.innerHTML = `<div class="empty-state">No model-version record exists yet. Start the service once to create an honest <strong>NOT TRAINED</strong> record.</div>`;
    return;
  }
  const metrics = model.metrics || {};
  const hasEvaluation = Number.isFinite(Number(metrics.f1)) && metrics.split === "test";
  const metricMarkup = hasEvaluation
    ? `<div class="model-metrics"><div><span>Held-out F1 / Dice</span><strong>${formatMetric(metrics.f1)}</strong></div><div><span>Held-out IoU</span><strong>${formatMetric(metrics.iou)}</strong></div><div><span>Precision</span><strong>${formatMetric(metrics.precision)}</strong></div><div><span>Recall</span><strong>${formatMetric(metrics.recall)}</strong></div></div>`
    : `<p class="model-empty">No held-out evaluation has been registered. This dashboard deliberately shows no accuracy claim until a lawful dataset, checkpoint, and completed test report have been supplied.</p>`;
  const report = metrics.evaluation_report ? `<small class="model-report">Evaluation artifact: ${escapeHtml(metrics.evaluation_report)}</small>` : "";
  holder.innerHTML = `<div class="model-main"><div><span class="status-tag ${model.status === "READY" ? "area-active" : "area-paused"}">${escapeHtml(model.status)}</span><h3>${escapeHtml(model.name)}</h3><p>${escapeHtml(model.architecture)}</p></div><div class="model-provenance"><span>Training data</span><strong>${escapeHtml(model.training_dataset || "not registered")}</strong><span>Checkpoint</span><small>${escapeHtml(model.checkpoint_path || "not configured")}</small></div></div>${metricMarkup}${report}<p class="model-note">${escapeHtml(model.notes || "No additional model note is available.")}</p>`;
}

function renderDataStatus(status) {
  const holder = $("#data-status-record");
  const date = status.newest_actual_acquisition_at ? formatDate(status.newest_actual_acquisition_at) : "none recorded";
  const refresh = status.latest_catalogue_query_at ? `${status.latest_catalogue_query_status} · ${formatDate(status.latest_catalogue_query_at)}` : "no catalogue query recorded";
  holder.innerHTML = `<div class="data-status-grid"><div><span>Database</span><strong>${escapeHtml(status.database)} (${escapeHtml(status.database_engine)})</strong></div><div><span>Stored observations</span><strong>${Number(status.observations).toLocaleString()}</strong></div><div><span>Newest acquisition</span><strong>${escapeHtml(date)}</strong></div><div><span>Latest catalogue refresh</span><strong>${escapeHtml(refresh)}</strong></div><div><span>Imagery download</span><strong>${status.imagery_credentials_configured ? "CDSE credentials configured" : "credentials not configured"}</strong></div></div>${status.latest_provider_error ? `<p class="data-error">Latest provider error: ${escapeHtml(status.latest_provider_error)}</p>` : ""}`;
}

function renderCandidates(candidates) {
  const holder = $("#candidate-list");
  if (!candidates.length) {
    holder.innerHTML = `<div class="empty-state">No priority candidates yet. Discovery requires a user-defined region and uses current catalogue availability when reachable.</div>`;
    return;
  }
  holder.innerHTML = candidates.map((candidate) => {
    const evidence = candidate.evidence || {};
    const newest = evidence.newest_acquisition_at ? formatDate(evidence.newest_acquisition_at) : "not available";
    const disabled = candidate.status !== "PRELIMINARY" ? "disabled" : "";
    return `<article class="candidate-row"><div><span class="status-tag ${candidate.status === "APPROVED" ? "area-active" : "area-paused"}">${escapeHtml(candidate.status)}</span><h3>${escapeHtml(candidate.region_name)}</h3><p>${escapeHtml(candidate.explanation)}</p><small>Score: ${Number(candidate.score).toFixed(1)}/100 · Observations: ${escapeHtml(evidence.observation_count ?? "unavailable")} · Newest: ${escapeHtml(newest)}</small></div><div class="candidate-actions"><button class="secondary-button compact approve-candidate" data-id="${candidate.id}" ${disabled}>Approve as AOI</button><button class="text-button dismiss-candidate" data-id="${candidate.id}" ${disabled}>Dismiss</button></div></article>`;
  }).join("");
  holder.querySelectorAll(".approve-candidate").forEach((button) => button.addEventListener("click", () => approveCandidate(button.dataset.id)));
  holder.querySelectorAll(".dismiss-candidate").forEach((button) => button.addEventListener("click", () => dismissCandidate(button.dataset.id)));
}

function renderDepartments(departments) {
  configuredDepartments = departments;
  const holder = $("#department-list");
  if (!departments.length) {
    holder.innerHTML = `<div class="empty-state">No routing departments configured. Add a local category such as Forest review or Planning review.</div>`;
    return;
  }
  holder.innerHTML = departments.map((department) => `<article class="department-row"><div><strong>${escapeHtml(department.name)}</strong><span>${escapeHtml(department.category)}</span></div><small>${department.recipient_email ? escapeHtml(department.recipient_email) : "No email recipient - in-app assignment only"}</small><span class="status-tag ${department.active ? "area-active" : "area-paused"}">${department.active ? "ACTIVE" : "INACTIVE"}</span></article>`).join("");
}

async function addDepartment() {
  const name = window.prompt("Department name (for example: Forest review):");
  if (!name) return;
  const category = window.prompt("Routing category (for example: vegetation):", "general review");
  if (!category) return;
  const recipient = window.prompt("Optional email recipient. Leave blank for in-app routing only:", "");
  try {
    await request("/api/departments", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, category, recipient_email: recipient || null }) });
    showToast("Department saved. Alerts can now be assigned to it.");
    refresh();
  } catch (error) { showToast(error.message); }
}

async function approveCandidate(candidateId) {
  const name = window.prompt("Name for the approved monitoring area:");
  if (!name) return;
  try {
    await request(`/api/priority-candidates/${candidateId}/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    showToast("Candidate approved as an active monitoring area.");
    refresh();
  } catch (error) { showToast(error.message); }
}

async function dismissCandidate(candidateId) {
  try {
    await request(`/api/priority-candidates/${candidateId}/dismiss`, { method: "POST" });
    showToast("Preliminary candidate dismissed.");
    refresh();
  } catch (error) { showToast(error.message); }
}

function runMarkup(run) {
  if (!run) return `<div class="run-state idle"><span class="run-dot"></span><div><strong>Ready to monitor</strong><small>No check has been run for this area.</small></div></div>`;
  const active = !FINAL_RUN_STATES.has(run.status);
  return `<div class="run-state ${stateClass(run.status)}"><span class="run-dot"></span><div><strong>${escapeHtml(run.stage.replaceAll("_", " "))} ${active ? `${run.progress_percent}%` : ""}</strong><small>${escapeHtml(run.message)}</small>${run.error_detail ? `<small class="run-error">${escapeHtml(run.error_detail)}</small>` : ""}</div></div>`;
}

function categoryLabel(value) { return value.replaceAll("_", " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase()); }

function renderAreas(areas) {
  const holder = $("#area-list");
  if (!areas.length) {
    holder.innerHTML = `<div class="empty-state wide"><strong>No monitoring areas yet.</strong><br><br>Start by drawing a real location with <em>Add monitoring area</em>. The system will search official Sentinel-2 metadata—not fabricated imagery.</div>`;
    return;
  }
  holder.innerHTML = areas.map((area) => {
    const latest = area.latest_run;
    const running = latest && !FINAL_RUN_STATES.has(latest.status);
    return `<article class="area-card" data-area-id="${area.id}">
      <div class="area-card-top"><div><span class="status-tag area-${area.status.toLowerCase()}">${escapeHtml(area.status)}</span><h3>${escapeHtml(area.name)}</h3><p class="coordinates">${area.center_lat.toFixed(5)}, ${area.center_lon.toFixed(5)}</p></div><a class="map-link" href="https://www.openstreetmap.org/?mlat=${encodeURIComponent(area.center_lat)}&mlon=${encodeURIComponent(area.center_lon)}#map=14/${encodeURIComponent(area.center_lat)}/${encodeURIComponent(area.center_lon)}" target="_blank" rel="noreferrer">View map ↗</a></div>
      <div class="area-tags">${area.categories.map((category) => `<span>${escapeHtml(categoryLabel(category))}</span>`).join("")}</div>
      <div class="area-config"><span>Cloud ≤ ${Math.round(area.max_cloud_cover)}%</span><span>Min ${formatArea(area.min_change_area_m2)}</span><span>${escapeHtml(area.sensitivity.toLowerCase())} sensitivity</span><span>Every ${formatInterval(area.monitoring_interval_minutes)}</span></div>
      ${runMarkup(latest)}
      <div class="area-actions">
        <button class="secondary-button compact check-area" data-area-id="${area.id}" ${running || area.status !== "ACTIVE" ? "disabled" : ""}>${running ? "Check in progress" : "Check now"}</button>
        <button class="text-button toggle-area" data-area-id="${area.id}" data-action="${area.status === "ACTIVE" ? "pause" : "resume"}">${area.status === "ACTIVE" ? "Pause" : "Resume"}</button>
        <button class="text-button history-area" data-area-id="${area.id}">Acquisitions</button>
      </div>
      <div class="area-history" id="area-history-${area.id}" hidden></div>
    </article>`;
  }).join("");
  holder.querySelectorAll(".check-area").forEach((button) => button.addEventListener("click", () => startCheck(button.dataset.areaId, button)));
  holder.querySelectorAll(".toggle-area").forEach((button) => button.addEventListener("click", () => toggleArea(button.dataset.areaId, button.dataset.action)));
  holder.querySelectorAll(".history-area").forEach((button) => button.addEventListener("click", () => toggleHistory(button.dataset.areaId)));
}

function formatInterval(minutes) {
  if (minutes % 1440 === 0) return `${minutes / 1440} day${minutes === 1440 ? "" : "s"}`;
  if (minutes % 60 === 0) return `${minutes / 60} hour${minutes === 60 ? "" : "s"}`;
  return `${minutes} min`;
}

async function toggleHistory(areaId) {
  const holder = document.getElementById(`area-history-${areaId}`);
  if (!holder) return;
  if (!holder.hidden) { holder.hidden = true; return; }
  holder.hidden = false;
  holder.innerHTML = `<p class="history-loading">Loading real acquisition metadata…</p>`;
  try {
    const [acquisitions, analyses] = await Promise.all([
      request(`/api/areas/${areaId}/acquisitions?limit=8`), request(`/api/areas/${areaId}/analyses?limit=5`)
    ]);
    const acquisitionRows = acquisitions.length ? acquisitions.map((item) => `<li><strong>${escapeHtml(item.acquisition_datetime.slice(0, 10))}</strong> · ${escapeHtml(item.product_id)}<small>Cloud: ${item.cloud_cover === null ? "not supplied" : `${item.cloud_cover.toFixed(1)}%`} · ${escapeHtml(item.selection_role || "CANDIDATE")} · ${escapeHtml(item.quality_status)}</small></li>`).join("") : "<li>No CDSE catalogue metadata has been recorded yet.</li>";
    const analysisRows = analyses.length ? analyses.map((item) => `<li><strong>${escapeHtml(item.created_at.slice(0, 10))}</strong> · ${escapeHtml(item.analysis_method)}<small>${escapeHtml(item.status)} · ${formatArea(item.result.changed_area_m2 || 0)}</small></li>`).join("") : "<li>No completed real analysis has been recorded yet.</li>";
    holder.innerHTML = `<div class="history-columns"><div><p class="history-title">Acquisition timeline</p><ul>${acquisitionRows}</ul></div><div><p class="history-title">Analysis history</p><ul>${analysisRows}</ul></div></div>`;
  } catch (error) {
    holder.innerHTML = `<p class="history-error">Could not load area history: ${escapeHtml(error.message)}</p>`;
  }
}

function renderCases(cases) {
  const holder = $("#case-list");
  if (!cases.length) {
    holder.innerHTML = `<div class="empty-state">No real monitoring evidence yet.<br><br>Add an AOI and select <strong>Check now</strong>. If credentials or a trained model are unavailable, the system will show that exact state rather than inventing a result.</div>`;
    return;
  }
  holder.innerHTML = cases.map((item) => `
    <article class="case-row" data-case-id="${item.id}" tabindex="0" role="button" aria-label="Open ${escapeHtml(item.title)}">
      <img class="case-thumb" src="${escapeHtml(item.mask_image)}" alt="Change evidence for ${escapeHtml(item.title)}">
      <div><h3 class="case-title">${escapeHtml(item.title)}</h3><div class="case-meta"><span>${escapeHtml(item.location || "AOI centre not recorded")}</span><span>•</span><span>${escapeHtml(item.before_date || "Unknown")} → ${escapeHtml(item.after_date || "Unknown")}</span><span>•</span><span>${escapeHtml(item.analysis_method)}</span></div><small class="case-provenance">${escapeHtml(item.data_origin || "unknown origin")} · ${escapeHtml(item.model_version_label || "model version unavailable")}</small></div>
      <div class="case-signal"><span class="confidence">${Math.round(item.overall_confidence * 100)}%</span><span class="severity-tag ${severityClass(item.highest_severity)}">${escapeHtml(item.highest_severity)}</span></div>
    </article>`).join("");
  holder.querySelectorAll(".case-row").forEach((row) => {
    const open = () => showCase(row.dataset.caseId);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
  });
}

function renderAlerts(alerts) {
  const holder = $("#alert-list");
  const open = alerts.filter((alert) => alert.status === "open");
  if (!open.length) {
    holder.innerHTML = `<div class="empty-state">No open real-data alerts.<br><br>Alerts appear only after a material monitoring result passes configured thresholds.</div>`;
    return;
  }
  const departmentOptions = configuredDepartments.filter((department) => department.active).map((department) => `<option value="${department.id}">${escapeHtml(department.name)}</option>`).join("");
  holder.innerHTML = open.map((alert) => `<article class="alert-row"><div class="alert-top"><span class="alert-title">${escapeHtml(alert.title)}</span><span class="severity-tag ${severityClass(alert.severity)}">${escapeHtml(alert.severity)}</span></div><p class="alert-message">${escapeHtml(alert.message)}</p><div class="alert-actions"><button class="ack-button" data-alert-id="${alert.id}">Acknowledge</button>${departmentOptions ? `<select class="assign-department" data-alert-id="${alert.id}"><option value="">Assign department...</option>${departmentOptions}</select><button class="ack-button assign-alert" data-alert-id="${alert.id}">Assign</button>` : ""}</div></article>`).join("");
  holder.querySelectorAll(".ack-button").forEach((button) => button.addEventListener("click", async () => {
    if (button.classList.contains("assign-alert")) return;
    try {
      await request(`/api/alerts/${button.dataset.alertId}/acknowledge`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ acknowledgement_note: "Acknowledged from monitoring dashboard" }) });
      showToast("Alert acknowledged. The audit trail was updated.");
      refresh();
    } catch (error) { showToast(error.message); }
  }));
  holder.querySelectorAll(".assign-alert").forEach((button) => button.addEventListener("click", async () => {
    const selector = holder.querySelector(`.assign-department[data-alert-id="${button.dataset.alertId}"]`);
    if (!selector?.value) { showToast("Select a department first."); return; }
    try {
      const attempts = await request(`/api/alerts/${button.dataset.alertId}/assign`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ department_id: selector.value, notify_email: false }) });
      showToast(`Alert assigned. ${attempts.map((item) => item.channel + ": " + item.status).join(" · ")}`); refresh();
    } catch (error) { showToast(error.message); }
  }));
}

function findingEvidence(finding) {
  try {
    const evidence = JSON.parse(finding.evidence_json);
    const bits = [evidence.rationale, `P(change) ${evidence.mean_change_probability}`];
    if (finding.ndvi_delta !== null) bits.push(`NDVI Δ ${Number(finding.ndvi_delta).toFixed(3)}`);
    if (finding.persistence_score !== null) bits.push(`persistence ${Math.round(Number(finding.persistence_score) * 100)}%`);
    return escapeHtml(bits.filter(Boolean).join("; "));
  } catch (_) { return "Evidence metadata unavailable"; }
}

async function showCase(caseId) {
  try {
    const item = await request(`/api/cases/${caseId}`);
    const findings = item.findings.length ? item.findings.map((finding) => `<tr><td><strong>${escapeHtml((finding.category || finding.activity).replaceAll("_", " "))}</strong></td><td>${Math.round(finding.confidence * 100)}%</td><td><span class="severity-tag ${severityClass(finding.severity)}">${escapeHtml(finding.severity)}</span></td><td>${formatArea(finding.area_m2)}</td><td>${findingEvidence(finding)}</td></tr>`).join("") : `<tr><td colspan="5">No components passed the applicable noise/materiality threshold.</td></tr>`;
    const reviews = item.reviews.length ? item.reviews.map((review) => `<li><strong>${escapeHtml(review.action)}</strong> · ${formatDate(review.created_at)}${review.note ? `<small>${escapeHtml(review.note)}</small>` : ""}</li>`).join("") : "<li>No human review has been recorded yet.</li>";
    const reportLink = `<a class="report-link" href="/api/cases/${encodeURIComponent(item.id)}/report" target="_blank" rel="noreferrer">Download investigation PDF</a>`;
    $("#case-detail").innerHTML = `<div class="detail-wrap"><div class="detail-header"><div><p class="eyebrow">REAL MONITORING EVIDENCE</p><h2>${escapeHtml(item.title)}</h2><p class="detail-sub">${escapeHtml(item.location || "AOI centre not recorded")} · ${escapeHtml(item.before_date || "unknown date")} → ${escapeHtml(item.after_date || "unknown date")} · ${escapeHtml(item.source || "source not recorded")}</p></div><button class="icon-button close-detail" aria-label="Close">×</button></div><img class="detail-image" src="${escapeHtml(item.mask_image)}" alt="Before, after, and highlighted change evidence"><p class="detail-disclaimer">This evidence indicates potential physical change only. It requires human verification and does not establish illegal activity.</p><div class="detail-info"><div class="detail-stat"><span>Analysis method</span><strong>${escapeHtml(item.analysis_method)}</strong></div><div class="detail-stat"><span>Model version</span><strong>${escapeHtml(item.model_version_label || "not recorded")}</strong></div><div class="detail-stat"><span>Change estimate</span><strong>${formatArea(item.changed_area_m2)}</strong></div><div class="detail-stat"><span>Case status</span><strong>${escapeHtml(item.status)}</strong></div></div><table class="finding-table"><thead><tr><th>Potential category</th><th>Confidence</th><th>Severity</th><th>Area</th><th>Evidence summary</th></tr></thead><tbody>${findings}</tbody></table><div class="review-row"><div><p class="history-title">Human review trail</p><ul class="review-list">${reviews}</ul>${reportLink}</div><div class="status-control"><label for="status-select">Investigator decision</label><select id="status-select"><option value="new">New</option><option value="reviewed">Reviewed</option><option value="verified">Verified physical change</option><option value="escalated">Escalated</option><option value="dismissed">Dismissed</option></select><button class="secondary-button" id="save-status">Save decision</button></div></div></div>`;
    $("#status-select").value = item.status;
    $(".close-detail").addEventListener("click", () => detailDialog.close());
    $("#save-status").addEventListener("click", async () => {
      try {
        await request(`/api/cases/${item.id}/status`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: $("#status-select").value, review_note: "Updated from monitoring dashboard", reviewer: "dashboard user" }) });
        showToast("Case decision saved in the review trail."); detailDialog.close(); refresh();
      } catch (error) { showToast(error.message); }
    });
    detailDialog.showModal();
  } catch (error) { showToast(error.message); }
}

async function startCheck(areaId, button) {
  if (button) { button.disabled = true; button.textContent = "Queueing…"; }
  try {
    const run = await request(`/api/areas/${areaId}/check-now`, { method: "POST" });
    activeRunIds.add(run.id);
    showToast("Real Copernicus monitoring check queued. The card will show actual backend progress.", 5200);
    await refresh();
    pollRun(run.id);
  } catch (error) {
    showToast(error.message);
    if (button) { button.disabled = false; button.textContent = "Check now"; }
  }
}

async function pollRun(runId) {
  try {
    const run = await request(`/api/monitoring-runs/${runId}`);
    await refresh({ quiet: true });
    if (FINAL_RUN_STATES.has(run.status)) {
      activeRunIds.delete(runId);
      showToast(`${run.stage.replaceAll("_", " ")}: ${run.message}`, 6000);
      return;
    }
    window.setTimeout(() => pollRun(runId), 1800);
  } catch (error) {
    activeRunIds.delete(runId);
    showToast(`Could not read monitoring progress: ${error.message}`);
  }
}

async function toggleArea(areaId, action) {
  try {
    await request(`/api/areas/${areaId}/${action}`, { method: "POST" });
    showToast(`Monitoring area ${action === "pause" ? "paused" : "resumed"}.`);
    refresh();
  } catch (error) { showToast(error.message); }
}

async function refresh(options = {}) {
  try {
    const [metrics, areas, cases, alerts, system, models, dataStatus, candidates, departments] = await Promise.all([
      request("/api/metrics"), request("/api/areas"), request("/api/cases"), request("/api/alerts?open_only=true"), request("/api/system/status"), request("/api/models"), request("/api/data-status"), request("/api/priority-candidates"), request("/api/departments")
    ]);
    renderMetrics(metrics); renderAreas(areas); renderCases(cases); renderDepartments(departments); renderAlerts(alerts); renderSystemStatus(system); renderModels(models); renderDataStatus(dataStatus); renderCandidates(candidates);
    for (const area of areas) {
      if (area.latest_run && !FINAL_RUN_STATES.has(area.latest_run.status) && !activeRunIds.has(area.latest_run.id)) {
        activeRunIds.add(area.latest_run.id); pollRun(area.latest_run.id);
      }
    }
  } catch (error) {
    if (!options.quiet) showToast(`Unable to load the monitoring API: ${error.message}`);
    $("#area-list").innerHTML = `<div class="empty-state wide">Unable to load monitoring data: ${escapeHtml(error.message)}</div>`;
  }
}

function resetAreaForm() {
  $("#area-form").reset();
  $("#area-error").textContent = "";
  $("#geometry-json").value = "";
  $("#bbox-input").value = "";
  $("#area-centre").value = "";
  resetMapLayers();
}

$("#open-area").addEventListener("click", () => {
  resetAreaForm();
  areaDialog.showModal();
  initialiseMap();
  window.setTimeout(() => aoiMap?.invalidateSize(), 80);
});
$("#open-priority").addEventListener("click", () => { $("#priority-form").reset(); $("#priority-error").textContent = ""; priorityDialog.showModal(); });
$("#close-priority").addEventListener("click", () => priorityDialog.close());
$("#cancel-priority").addEventListener("click", () => priorityDialog.close());
$("#refresh-data-status").addEventListener("click", () => refresh());
$("#add-department").addEventListener("click", addDepartment);
$("#close-area").addEventListener("click", () => areaDialog.close());
$("#cancel-area").addEventListener("click", () => areaDialog.close());
$("#refresh-dashboard").addEventListener("click", () => refresh());
$("#apply-bbox").addEventListener("click", () => {
  try {
    const geometry = bboxToGeometry($("#bbox-input").value.trim());
    updateGeometryFields(geometry); addGeometryToMap(geometry);
    $("#area-error").textContent = "";
  } catch (error) { $("#area-error").textContent = error.message; }
});
$("#geometry-json").addEventListener("change", () => {
  const text = $("#geometry-json").value.trim();
  if (!text) return;
  try {
    const geometry = getFormGeometry();
    updateGeometryFields(geometry); addGeometryToMap(geometry);
    $("#area-error").textContent = "";
  } catch (error) { $("#area-error").textContent = error.message; }
});
$("#area-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#submit-area");
  button.disabled = true; button.textContent = "Saving…"; $("#area-error").textContent = "";
  try {
    const form = new FormData(event.currentTarget);
    const categories = form.getAll("categories");
    const geometry = getFormGeometry();
    const area = await request("/api/areas", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: form.get("name"), geometry, categories,
        max_cloud_cover: Number(form.get("max_cloud_cover")),
        min_change_area_m2: Number(form.get("min_change_area_m2")),
        sensitivity: form.get("sensitivity"),
        monitoring_interval_minutes: Number(form.get("monitoring_interval_minutes"))
      })
    });
    areaDialog.close(); showToast(`Monitoring area “${area.name}” saved. Select Check now to perform a real catalogue search.`); await refresh();
  } catch (error) { $("#area-error").textContent = error.message; }
  finally { button.disabled = false; button.textContent = "Save monitoring area"; }
});
$("#priority-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#submit-priority");
  button.disabled = true; button.textContent = "Searching catalogue..."; $("#priority-error").textContent = "";
  try {
    const form = new FormData(event.currentTarget);
    const geometry = bboxToGeometry(String(form.get("bbox")));
    const candidates = await request("/api/priority-candidates/discover", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ region_name: form.get("region_name"), geometry, grid_size: Number(form.get("grid_size")), max_cloud_cover: Number(form.get("max_cloud_cover")), lookback_days: Number(form.get("lookback_days")) }) });
    priorityDialog.close(); renderCandidates(candidates); showToast(`Discovery finished: ${candidates.length} preliminary candidate(s) saved.`); refresh({ quiet: true });
  } catch (error) { $("#priority-error").textContent = error.message; }
  finally { button.disabled = false; button.textContent = "Discover candidates"; }
});

refresh();
