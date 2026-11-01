// Site detail: live run status, acquisition timeline, findings history, config, and archive replay.

import { h, debounce, morph, reconcile, setText } from "../dom.js";
import { api, asList } from "../api.js";
import { icon } from "../icons.js";
import {
  categoryLabel, cleanText, errorText, formatArea, formatDate, formatDateTime, formatPercent, formatShortDate, healthLabel, intervalLabel, isFailedRun, isFinalRun, runLabel,
  sceneStatusKey, sceneStatusLabel, sensitivityLabel, siteTypeLabel,
} from "../format.js";
import { applyRun, on, removeArea, store, upsertArea } from "../state.js";
import { areaBounds, createMap, fitTo, HEALTH_COLOURS, polygonStyle } from "../map.js";
import {
  confirmModal, emptyState, errorState, imageBox, kv, onTick, progressBar, relTime, replayBadge, severityTag, skeletonRows, statusTag, toast,
} from "../ui.js";

const L = () => window.L;

function isoDate(date) {
  return date.toISOString().slice(0, 10);
}

export async function mountSite(root, params) {
  const areaId = params.id;
  let area = store.areas.get(areaId) || null;
  let map = null;
  let polygon = null;
  let disposed = false;
  let lastFinalRun = null;
  const cleanups = [];
  const replayRunIds = new Set();

  root.replaceChildren(h("div.page.page-wide", skeletonRows(2), h("div.skeleton.skeleton-hero")));

  try {
    area = await api.area(areaId);
    upsertArea(area);
  } catch (error) {
    if (disposed) return () => {};
    root.replaceChildren(h("div.page", error.status === 404
      ? emptyState("This site no longer exists", "It may have been removed from monitoring.", h("a.btn", { href: "#/" }, "Back to operations"))
      : errorState(error, () => mountAgain(), "Could not load this site")));
    return () => {
      disposed = true;
    };
  }

  function mountAgain() {
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  }

  const headerHost = h("div");
  const statusHost = h("div");
  const configHost = h("div");
  const timelineStrip = h("ol.timeline", { "aria-label": "Scene timeline" });
  const timelineState = h("div.timeline-state");
  const timelineCount = h("span.panel-sub");
  const findingsBody = h("div");
  const mapHost = h("div.site-map", { role: "img", "aria-label": `Monitored area of ${area.name}` });
  const nextCheck = h("span.status-next");

  function renderHeader() {
    const health = area.health || (area.status === "PAUSED" ? "PAUSED" : "OK");
    const paused = area.status === "PAUSED";
    const next = h("header.page-head",
      h("div.page-head-main",
        h("nav.crumbs", { "aria-label": "Breadcrumb" }, h("a", { href: "#/" }, icon("chevronLeft", { size: 12 }), "Operations")),
        h("h1.page-title", area.name),
        h("div.page-meta",
          h("span.health-inline", h("span.dot", { dataset: { health } }), healthLabel(health)),
          [area.district, area.state].filter(Boolean).length ? h("span", [area.district, area.state].filter(Boolean).join(", ")) : null,
          area.site_type ? h("span", siteTypeLabel(area.site_type)) : null)),
      h("div.page-actions",
        h("button.btn", { type: "button", id: "site-pause", onclick: togglePause }, icon(paused ? "play" : "pause"), paused ? "Resume" : "Pause"),
        h("button.btn.btn-primary", { type: "button", id: "site-check", onclick: checkNow, disabled: paused || liveRun() ? true : null }, icon("refresh"), "Check now")));
    if (headerHost.firstElementChild) morph(headerHost.firstElementChild, next);
    else headerHost.appendChild(next);
  }

  function liveRun() {
    const run = store.runs.get(areaId);
    return run && !run.final ? run : null;
  }

  async function checkNow() {
    try {
      const run = await api.checkNow(areaId);
      applyRun({
        run_id: run.id || run.run_id,
        area_id: areaId,
        area_name: area.name,
        status: run.status || "QUEUED",
        stage: run.stage || "QUEUED",
        progress_percent: run.progress_percent || 0,
        message: run.message || "Queued for the next available worker.",
      });
    } catch (error) {
      toast(errorText(error, "Could not start a check."), "error");
    }
  }

  async function togglePause() {
    const paused = area.status === "PAUSED";
    try {
      area = paused ? await api.resume(areaId) : await api.pause(areaId);
      upsertArea(area);
      renderHeader();
      renderStatus();
      toast(paused ? "Monitoring resumed." : "Monitoring paused.", "ok");
    } catch (error) {
      toast(errorText(error, "Could not change monitoring state."), "error");
    }
  }

  function statusIdleLine() {
    if (area.status === "PAUSED") return "Monitoring is paused. No checks will run until it is resumed.";
    if (lastFinalRun) return lastFinalRun.message || runLabel(lastFinalRun.stage);
    const latest = area.latest_run;
    if (latest && latest.message) return latest.message;
    return "Waiting for the next check.";
  }

  function renderStatus() {
    const run = liveRun();
    const failed = lastFinalRun && isFailedRun(lastFinalRun.status);
    const isReplayRun = run && (run.origin === "archive_replay" || replayRunIds.has(run.run_id));
    const next = h("section.status-line", {
      dataset: { state: run ? "live" : failed ? "failed" : area.status === "PAUSED" ? "paused" : "idle" },
      "aria-live": "polite",
      "aria-label": "Monitoring status",
    },
    h("span.status-main",
      run ? h("span.status-spinner", { "aria-hidden": "true" }) : h("span.dot", { dataset: { health: area.health || "OK" } }),
      h("span.status-stage", run ? runLabel(run.stage) || "Running" : "Status"),
      h("span.status-message", cleanText(run ? run.message || "Running" : statusIdleLine())),
      isReplayRun ? h("span.tag.tag-replay", icon("history", { size: 12 }), "Archive replay") : null),
    run ? progressBar(run.progress_percent, { label: "Run progress" }) : null);
    if (statusHost.firstElementChild) morph(statusHost.firstElementChild, next);
    else statusHost.appendChild(next);
  }

  function updateNextCheck() {
    if (area.status === "PAUSED") {
      setText(nextCheck, "");
      return;
    }
    const at = area.next_check_at;
    if (!at) {
      setText(nextCheck, "");
      return;
    }
    const time = Date.parse(at.endsWith("Z") ? at : `${at}Z`);
    if (!Number.isFinite(time)) return;
    const delta = time - Date.now();
    setText(nextCheck, delta <= 0 ? "Next check due now" : `Next check in ${duration(delta / 1000)}`);
  }

  function duration(seconds) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.round((seconds % 3600) / 60);
    if (hours >= 24) return `${Math.floor(hours / 24)} d ${hours % 24} h`;
    if (hours > 0) return `${hours} h ${minutes} min`;
    return `${Math.max(1, minutes)} min`;
  }

  function renderConfig() {
    const categories = asList(area.categories).map(categoryLabel).join(", ");
    const next = h("dl.kv-list",
      kv("Check interval", intervalLabel(area.monitoring_interval_minutes)),
      kv("Sensitivity", sensitivityLabel(area.sensitivity)),
      kv("Categories", categories),
      kv("Cloud limit", area.max_cloud_cover !== undefined ? `${Math.round(area.max_cloud_cover)}%` : null),
      kv("Smallest change", formatArea(area.min_change_area_m2)),
      kv("Last checked", formatDateTime(area.last_checked_at)),
      kv("Description", area.description),
      kv("Added", formatDate(area.created_at)));
    if (configHost.firstElementChild) morph(configHost.firstElementChild, next);
    else configHost.appendChild(next);
  }

  // ------------------------------------------------------ timeline

  function renderScene(scene) {
    const status = sceneStatusKey(scene.status);
    const inner = [
      imageBox(scene.thumbnail, { alt: `Scene of ${formatDate(scene.date)}`, ratio: "1 / 1", className: "scene-thumb", fallback: status === "cloudy" ? "Cloud" : null }),
      h("span.scene-date", formatShortDate(scene.date)),
      h("span.scene-status", h("span.scene-dot", { dataset: { status } }), sceneStatusLabel(scene.status)),
      scene.aoi_clear_fraction !== undefined && scene.aoi_clear_fraction !== null
        ? h("span.scene-cloud", `${formatPercent(scene.aoi_clear_fraction)} clear`)
        : scene.cloud_cover !== undefined && scene.cloud_cover !== null
          ? h("span.scene-cloud", `${formatPercent(Number(scene.cloud_cover) / 100)} cloud`)
          : null,
    ];
    const title = [formatDate(scene.date), asList(scene.categories).map(categoryLabel).join(", "), scene.satellite, scene.product_id].filter(Boolean).join("\n");
    return h("li.scene", { class: scene.thumbnail ? null : "is-bare", dataset: { status }, title },
      scene.case_id
        ? h("a.scene-link", { href: `#/case/${encodeURIComponent(scene.case_id)}` }, inner)
        : h("div.scene-link.scene-static", inner));
  }

  let timelineLoaded = false;
  async function loadTimeline() {
    try {
      const scenes = asList(await api.timeline(areaId), "timeline", "scenes");
      if (disposed) return;
      timelineLoaded = true;
      const sorted = [...scenes].sort((a, b) => String(a.date).localeCompare(String(b.date)));
      setText(timelineCount, sorted.length ? `${sorted.length} ${sorted.length === 1 ? "scene" : "scenes"}` : "");
      const atEnd = timelineStrip.scrollWidth - timelineStrip.scrollLeft - timelineStrip.clientWidth < 12;
      const created = reconcile(timelineStrip, sorted, { key: (scene) => scene.product_id || scene.date, render: renderScene });
      if (created.size && atEnd) timelineStrip.scrollLeft = timelineStrip.scrollWidth;
      timelineState.replaceChildren();
      if (!sorted.length) {
        timelineState.append(emptyState("No scenes yet", "Sentinel-2 passes every 2 to 5 days. A check finds only scenes that exist and are not cloud-covered."));
      }
    } catch (error) {
      if (disposed) return;
      if (!timelineLoaded) timelineState.replaceChildren(errorState(error, loadTimeline, "Could not load the scene timeline"));
    }
  }

  // ------------------------------------------------------ findings

  let findingsLoaded = false;
  async function loadFindings() {
    try {
      const rows = asList(await api.cases({ area_id: areaId, limit: 50 }), "cases");
      if (disposed) return;
      findingsLoaded = true;
      if (!rows.length) {
        findingsBody.replaceChildren(h("p.panel-empty", "No findings at this site yet."));
        findingsBody._keyed = null;
        return;
      }
      const list = findingsBody.querySelector("ul") || (() => {
        const ul = h("ul.finding-list");
        findingsBody.replaceChildren(ul);
        return ul;
      })();
      reconcile(list, rows, {
        key: (row) => row.id,
        render: (row) => h("li.finding-row",
          h("a.finding-link", { href: `#/case/${encodeURIComponent(row.id)}` },
            imageBox(row.thumbnail || row.after_image, { alt: "", ratio: "1 / 1", className: "finding-thumb", fallback: null }),
            h("span.finding-body",
              h("span.finding-top", severityTag(row.highest_severity || row.severity), row.status && row.alert_status ? statusTag(row.alert_status) : null, replayBadge(row)),
              h("span.finding-title", asList(row.categories).map(categoryLabel).join(", ") || row.title || "Change detected"),
              h("span.finding-meta",
                row.after_date ? `Scene ${formatDate(row.after_date)}` : null,
                row.changed_area_m2 !== undefined ? ` · ${formatArea(row.changed_area_m2)}` : null)),
            relTime(row.created_at, "finding-age"))),
      });
    } catch (error) {
      if (disposed) return;
      if (!findingsLoaded) findingsBody.replaceChildren(errorState(error, loadFindings, "Could not load findings"));
    }
  }

  // ------------------------------------------------------ replay

  function replayForm() {
    const today = new Date();
    const end = new Date(today.getTime() - 86400000);
    const start = new Date(today.getTime() - 86400000 * 365);
    const startInput = h("input.input", { id: "replay-start", type: "date", value: isoDate(start), max: isoDate(end), min: "2017-01-01", required: true });
    const endInput = h("input.input", { id: "replay-end", type: "date", value: isoDate(end), max: isoDate(today), min: "2017-01-01", required: true });
    const scenes = h("input.input", { id: "replay-max", type: "number", value: 6, min: 2, max: 30, required: true });
    const error = h("p.form-error", { role: "alert", hidden: true });
    const submit = h("button.btn", { type: "submit" }, icon("history"), "Start replay");
    return h("form.form", {
      onsubmit: async (event) => {
        event.preventDefault();
        error.hidden = true;
        if (startInput.value >= endInput.value) {
          error.hidden = false;
          error.textContent = "The start date must be before the end date.";
          return;
        }
        submit.disabled = true;
        try {
          const result = await api.replay(areaId, {
            start_date: startInput.value,
            end_date: endInput.value,
            max_scenes: Number(scenes.value) || 6,
          });
          asList(result && result.run_ids).forEach((id) => replayRunIds.add(id));
          toast("Archive replay started.", "ok");
          pollRuns(asList(result && result.run_ids));
        } catch (failure) {
          error.hidden = false;
          error.textContent = errorText(failure, "Replay could not be started.");
        } finally {
          submit.disabled = false;
        }
      },
    },
    h("p.form-note", "Runs the same analysis over past scenes. Results carry an Archive replay badge."),
    h("div.form-row",
      h("label.field", { for: "replay-start" }, h("span.field-label", "From"), startInput),
      h("label.field", { for: "replay-end" }, h("span.field-label", "To"), endInput),
      h("label.field.field-narrow", { for: "replay-max" }, h("span.field-label", "Max scenes"), scenes)),
    error,
    h("div.form-actions", submit));
  }

  /** If the live stream is down, a replay still shows progress. */
  async function pollRuns(ids) {
    for (const id of ids) {
      while (!disposed && store.connection !== "live") {
        try {
          const run = await api.run(id);
          if (isFinalRun(run.status)) {
            applyRun({ run_id: id, area_id: areaId, area_name: area.name, status: run.status, stage: run.stage, progress_percent: run.progress_percent, message: run.message });
            break;
          }
          applyRun({ run_id: id, area_id: areaId, area_name: area.name, status: run.status, stage: run.stage, progress_percent: run.progress_percent, message: run.message });
        } catch {
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 2500));
      }
    }
  }

  // ------------------------------------------------------ map

  function mountMap() {
    if (!L()) return;
    map = createMap(mapHost, { scrollWheelZoom: false });
    drawPolygon();
    requestAnimationFrame(() => {
      if (!disposed) fitTo(map, areaBounds(area), { padding: [24, 24], maxZoom: 15 });
    });
  }

  function drawPolygon() {
    if (!map || !area.geometry || !area.geometry.type) return;
    if (polygon) polygon.remove();
    const colour = HEALTH_COLOURS[area.health] || HEALTH_COLOURS.OK;
    polygon = L().geoJSON(area.geometry, { style: () => polygonStyle(colour) }).addTo(map);
  }

  async function removeSite() {
    const ok = await confirmModal({
      title: "Remove site",
      message: `Stop monitoring ${area.name} and remove it? This also removes its scene history and incidents.`,
      confirmLabel: "Remove site",
      danger: true,
    });
    if (!ok || disposed) return;
    try {
      await api.deleteArea(areaId);
      removeArea(areaId);
      toast("Site removed.", "ok");
      window.location.hash = "#/";
    } catch (error) {
      toast(errorText(error, "Site could not be removed."), "error");
    }
  }

  // ------------------------------------------------------ assemble

  renderHeader();
  renderStatus();
  renderConfig();
  updateNextCheck();

  root.replaceChildren(h("div.page.page-wide.site-page",
    headerHost,
    h("div.status-wrap", statusHost, nextCheck),
    h("section.panel.timeline-panel", { "aria-label": "Scene timeline" },
      h("header.panel-head", h("h2.panel-title", "Scene timeline"), timelineCount,
        h("span.timeline-legend",
          ["alert", "analysed", "cloudy", "clear"].map((status) => h("span.timeline-legend-item", h("span.scene-dot", { dataset: { status } }), sceneStatusLabel(status))))),
      timelineStrip,
      timelineState),
    h("div.site-grid",
      h("div.site-col",
        h("section.panel", { "aria-label": "Monitored area" }, mapHost),
        h("section.panel", { "aria-label": "Findings history" },
          h("header.panel-head", h("h2.panel-title", "Findings history")),
          findingsBody)),
      h("div.site-col",
        h("section.panel", { "aria-label": "Archive replay" },
          h("header.panel-head", h("h2.panel-title", "Archive replay")),
          h("div.panel-body", replayForm())),
        h("section.panel", { "aria-label": "Configuration" },
          h("header.panel-head", h("h2.panel-title", "Configuration"),
            h("button.btn.btn-sm.btn-ghost", { type: "button", onclick: removeSite }, "Remove site")),
          configHost)))));

  findingsBody.append(skeletonRows(3));
  timelineState.append(skeletonRows(1));
  mountMap();
  loadTimeline();
  loadFindings();

  // ------------------------------------------------------ live updates

  const refreshData = debounce(() => {
    loadTimeline();
    loadFindings();
  }, 600);

  cleanups.push(on("run-event", (event) => {
    if (event.area_id !== areaId) return;
    if (isFinalRun(event.status) || isFinalRun(event.stage)) {
      lastFinalRun = event;
      refreshData();
    }
    renderStatus();
    renderHeader();
  }));
  cleanups.push(on("runs", () => {
    renderStatus();
    renderHeader();
  }));
  cleanups.push(on("areas", () => {
    const fresh = store.areas.get(areaId);
    if (!fresh) return;
    const geometryChanged = JSON.stringify(fresh.geometry) !== JSON.stringify(area.geometry) || fresh.health !== area.health;
    area = fresh;
    renderHeader();
    renderStatus();
    renderConfig();
    updateNextCheck();
    if (geometryChanged) drawPolygon();
  }));
  cleanups.push(on("scene-event", (event) => {
    if (event.area_id === areaId) refreshData();
  }));
  cleanups.push(on("alerts", refreshData));
  cleanups.push(onTick(updateNextCheck));

  return () => {
    disposed = true;
    refreshData.cancel();
    cleanups.forEach((off) => off());
    if (map) map.remove();
  };
}
