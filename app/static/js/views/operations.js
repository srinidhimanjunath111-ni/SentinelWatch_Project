// Operations: site rail, map, KPI strip, activity log and live alert feed.
// This view is mounted once and kept alive for the whole session, so returning to it keeps the map
// view, scroll positions and loaded tiles exactly as they were.

import { h, morph, reconcile, setText } from "../dom.js";
import { icon } from "../icons.js";
import {
  categoryLabel, cleanText, formatArea, formatClock, formatCount, formatShortDate, healthLabel, runLabel, siteTypeLabel, verdictLabel,
} from "../format.js";
import {
  activityFeed, alertPlace, alertPlaceFull, alertTime, isOpenAlert, on, refreshAll, sortedAlerts, sortedAreas, store,
} from "../state.js";
import { areaBounds, createMap, flyTo, HEALTH_COLOURS, INDIA_BOUNDS, polygonStyle } from "../map.js";
import { emptyState, errorState, imageBox, progressBar, relTime, replayBadge, severityTag, statusTag } from "../ui.js";

const L = () => window.L;

export function createOperationsView(root) {
  const siteList = h("ul.site-list", { "aria-label": "Monitored sites" });
  const siteEmpty = h("div.rail-state");
  const feedList = h("ul.feed-list", { "aria-label": "Alert feed" });
  const feedEmpty = h("div.rail-state");
  const kpis = h("dl.kpi-strip", { "aria-label": "Key figures" });
  const logList = h("ol.log-list", { "aria-label": "Recent activity" });
  const mapEl = h("div.ops-map", { id: "ops-map", role: "application", "aria-label": "Map of monitored sites in India" });
  const siteCount = h("span.rail-count");
  const feedCount = h("span.rail-count");
  const legend = h("div.map-legend", { "aria-hidden": "true" },
    ["ALERT", "OK", "CLOUDED", "ERROR", "PAUSED"].map((state) => h("span.map-legend-item",
      h("span.dot", { dataset: { health: state } }), healthLabel(state))));

  root.append(
    h("div.ops",
      h("aside.rail.rail-left", { "aria-label": "Monitored sites" },
        h("header.rail-head",
          h("h2.rail-title", "Sites"), siteCount,
          h("a.btn.btn-sm", { href: "#/add-site" }, icon("plus", { size: 14 }), "Add site")),
        h("div.rail-scroll", siteList, siteEmpty)),
      h("section.ops-center", { "aria-label": "Situation map" },
        kpis,
        h("div.ops-map-wrap",
          mapEl,
          legend,
          h("div.map-offline-note", { role: "status" }, "Base map unavailable. Site boundaries remain accurate.")),
        h("div.log", h("div.log-label", "Activity"), logList)),
      h("aside.rail.rail-right", { "aria-label": "Live alert feed" },
        h("header.rail-head",
          h("h2.rail-title", "Alerts"), feedCount,
          h("a.btn.btn-sm.btn-ghost", { href: "#/alerts" }, "All alerts", icon("chevronRight", { size: 14 }))),
        h("div.rail-scroll", feedList, feedEmpty))));

  // --------------------------------------------------- map
  let map = null;
  const layers = new Map(); // area id -> {polygon, marker, sig}

  function ensureMap() {
    if (map || !L()) return;
    map = createMap(mapEl, { zoomControl: true });
    map.fitBounds(INDIA_BOUNDS, { animate: false });
    L().control.scale({ imperial: false, position: "bottomleft" }).addTo(map);
  }

  function markerIcon(area) {
    const health = area.health || "OK";
    const run = store.runs.get(area.id);
    const live = run && !run.final;
    return L().divIcon({
      className: "site-marker",
      html: `<span class="site-marker-dot" data-health="${health}" data-live="${live ? "1" : "0"}"><span class="site-marker-pulse"></span></span>`,
      iconSize: [18, 18],
      iconAnchor: [9, 9],
    });
  }

  function focusArea(areaId) {
    const area = store.areas.get(areaId);
    const bounds = areaBounds(area);
    if (map && bounds) flyTo(map, bounds);
  }

  function syncMapLayers() {
    if (!map) return;
    const seen = new Set();
    for (const area of store.areas.values()) {
      seen.add(area.id);
      const run = store.runs.get(area.id);
      const health = area.health || "OK";
      const sig = `${health}|${run && !run.final ? 1 : 0}|${JSON.stringify(area.geometry)}|${area.name}`;
      const existing = layers.get(area.id);
      if (existing && existing.sig === sig) continue;
      if (existing) {
        existing.polygon?.remove();
        existing.marker?.remove();
      }
      const colour = HEALTH_COLOURS[health] || HEALTH_COLOURS.OK;
      const className = health === "ALERT" ? "aoi-alert" : "";
      let polygon = null;
      try {
        if (area.geometry && area.geometry.type) {
          polygon = L().geoJSON(area.geometry, { style: () => polygonStyle(colour, { className }) });
          polygon.on("click", () => focusArea(area.id));
          polygon.addTo(map);
        }
      } catch {
        polygon = null;
      }
      const lat = Number(area.center_lat);
      const lon = Number(area.center_lon);
      let marker = null;
      if (Number.isFinite(lat) && Number.isFinite(lon)) {
        marker = L().marker([lat, lon], { icon: markerIcon(area), title: area.name, keyboard: false });
        marker.bindTooltip(tooltipNode(area.name), { direction: "top", offset: [0, -8], className: "map-tooltip" });
        marker.on("click", () => {
          focusArea(area.id);
          highlightSite(area.id);
        });
        marker.addTo(map);
      }
      layers.set(area.id, { polygon, marker, sig });
    }
    for (const [id, entry] of layers) {
      if (!seen.has(id)) {
        entry.polygon?.remove();
        entry.marker?.remove();
        layers.delete(id);
      }
    }
  }

  function tooltipNode(text) {
    const span = document.createElement("span");
    span.textContent = text || "";
    return span;
  }

  function highlightSite(areaId) {
    const row = siteList.querySelector(`[data-area="${CSS.escape(areaId)}"]`);
    if (!row) return;
    row.scrollIntoView({ block: "nearest" });
    row.classList.add("is-flash");
    setTimeout(() => row.classList.remove("is-flash"), 1400);
  }

  /** One-off ripple where a new alert was raised. The operator's map view is left alone. */
  function rippleAt(areaId) {
    const area = store.areas.get(areaId);
    if (!map || !area) return;
    const lat = Number(area.center_lat);
    const lon = Number(area.center_lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    const ripple = L().marker([lat, lon], {
      interactive: false,
      keyboard: false,
      icon: L().divIcon({ className: "site-ripple", html: "<span></span><span></span>", iconSize: [18, 18], iconAnchor: [9, 9] }),
    }).addTo(map);
    setTimeout(() => ripple.remove(), 3200);
  }

  // --------------------------------------------------- site rail

  function renderSite(entry) {
    const { area, run, open } = entry;
    const live = run && !run.final;
    const health = area.health || (area.status === "PAUSED" ? "PAUSED" : "OK");
    const meta = [area.state, siteTypeLabel(area.site_type)].filter(Boolean).join(" · ");
    const scene = area.last_scene && area.last_scene.date;
    const row = h("li.site-row", { dataset: { area: area.id, health } },
      h("button.site-focus", {
        type: "button",
        "aria-label": `Show ${area.name} on the map`,
        title: "Show on map",
        onclick: () => focusArea(area.id),
      }, h("span.dot", { dataset: { health }, title: healthLabel(health) }, h("span.sr-only", healthLabel(health)))),
      h("a.site-link", { href: `#/site/${encodeURIComponent(area.id)}` },
        h("span.site-top",
          h("span.site-name", area.name),
          open > 0 ? h("span.count-badge", { title: `${open} open ${open === 1 ? "alert" : "alerts"}` }, open) : null),
        meta ? h("span.site-meta", meta) : null,
        live
          ? h("span.site-run", h("span.site-run-text", run.origin === "archive_replay" ? "Archive replay: " : "", cleanText(run.message) || runLabel(run.stage) || "Checking"), progressBar(run.progress_percent, { label: `Check progress for ${area.name}` }))
          : h("span.site-meta.site-scene", scene ? `Last scene ${formatShortDate(scene)}` : healthLine(area))));
    return row;
  }

  function healthLine(area) {
    if (area.status === "PAUSED") return "Paused";
    return "No scene received yet";
  }

  function renderSites() {
    const rows = sortedAreas().map((area) => ({
      area,
      run: store.runs.get(area.id) || null,
      open: Number(area.open_alerts) || 0,
    }));
    setText(siteCount, rows.length ? rows.length : "");
    reconcile(siteList, rows, {
      key: (row) => row.area.id,
      render: renderSite,
      signature: (row) => {
        const { area, run, open } = row;
        return JSON.stringify([area.name, area.state, area.site_type, area.health, area.status, area.last_scene && area.last_scene.date, open, run && !run.final ? [run.message, run.stage, run.progress_percent, run.origin] : null]);
      },
    });
    if (rows.length) {
      siteEmpty.replaceChildren();
    } else if (store.failed.areas && !store.loadedOnce.areas) {
      siteEmpty.replaceChildren(errorState(store.failed.areas, refreshAll, "Could not load sites"));
    } else if (store.loadedOnce.areas) {
      siteEmpty.replaceChildren(emptyState("No sites yet", "Mark a place to start monitoring.", h("a.btn", { href: "#/add-site" }, icon("plus"), "Add site")));
    } else {
      siteEmpty.replaceChildren(h("div.skeleton-rows", Array.from({ length: 6 }, () => h("div.skeleton.skeleton-site"))));
    }
    syncMapLayers();
  }

  // --------------------------------------------------- feed

  function renderFeedItem(alert) {
    return h("li.feed-item", { dataset: { severity: String(alert.severity || "none").toLowerCase(), alert: alert.id } },
      h("a.feed-link", {
        href: alert.case_id ? `#/case/${encodeURIComponent(alert.case_id)}` : "#/alerts",
      },
      imageBox(alert.thumbnail, { alt: "", ratio: "1 / 1", className: "feed-thumb", fallback: null }),
      h("span.feed-body",
        h("span.feed-top",
          severityTag(alert.severity),
          relTime(alertTime(alert), "feed-age")),
        h("span.feed-title", categoryLabel(alert.category) || alert.title || "Change detected"),
        h("span.feed-place", alertPlaceFull(alert)),
        h("span.feed-foot",
          alert.verdict ? h("span.feed-verdict", verdictLabel(alert.verdict)) : null,
          replayBadge(alert),
          alert.status && alert.status !== "open" && alert.status !== "dispatched" ? statusTag(alert.status) : null))),
      h("button.feed-locate.icon-btn", {
        type: "button",
        title: "Show on map",
        "aria-label": `Show ${alertPlace(alert)} on the map`,
        onclick: () => {
          const current = store.alerts.get(alert.id);
          if (current && current.area_id) focusArea(current.area_id);
        },
      }, icon("map", { size: 14 })));
  }

  function renderFeed() {
    const alerts = sortedAlerts().slice(0, 60);
    const open = alerts.filter(isOpenAlert).length;
    setText(feedCount, open ? `${open} open` : "");
    const scrollEl = feedList.parentElement;
    const before = scrollEl.scrollHeight;
    const top = scrollEl.scrollTop;
    const created = reconcile(feedList, alerts, {
      key: (alert) => alert.id,
      render: renderFeedItem,
      signature: (alert) => JSON.stringify([alert.severity, alert.category, alert.area_name, alert.verdict, alert.status, alert.origin, alert.thumbnail, alertTime(alert)]),
    });
    if (feedRendered && created.size) {
      if (top > 2) {
        // Reading further down the feed: keep what the operator is looking at where it is.
        scrollEl.scrollTop = top + (scrollEl.scrollHeight - before);
      } else {
        for (const id of created) {
          const node = feedList.querySelector(`[data-alert="${CSS.escape(id)}"]`);
          if (node) node.classList.add("is-new");
        }
      }
    }
    feedRendered = store.loadedOnce.alerts;
    if (alerts.length) {
      feedEmpty.replaceChildren();
    } else if (store.failed.alerts && !store.loadedOnce.alerts) {
      feedEmpty.replaceChildren(errorState(store.failed.alerts, refreshAll, "Could not load alerts"));
    } else if (store.loadedOnce.alerts) {
      const count = store.areas.size;
      feedEmpty.replaceChildren(emptyState(
        count ? `Monitoring ${count} ${count === 1 ? "site" : "sites"}. No alerts yet.` : "No alerts yet.",
        "New detections appear here the moment a scene is analysed.",
      ));
    } else {
      feedEmpty.replaceChildren(h("div.skeleton-rows", Array.from({ length: 4 }, () => h("div.skeleton.skeleton-feed"))));
    }
  }
  let feedRendered = false;

  // --------------------------------------------------- KPIs and activity

  function kpi(label, value, emphasis = false) {
    return h("div.kpi", { class: emphasis ? "kpi-critical" : null },
      h("dt.kpi-label", label),
      h("dd.kpi-value", value === "" || value === null || value === undefined ? "–" : value));
  }

  function renderKpis() {
    const counts = (store.overview && store.overview.counts) || null;
    const next = h("dl.kpi-strip", { "aria-label": "Key figures" },
      kpi("Scenes analysed", counts ? formatCount(counts.scenes_analysed) : ""),
      kpi("Open alerts", counts ? formatCount(counts.open_alerts) : ""),
      kpi("Critical", counts ? formatCount(counts.critical_alerts) : "", counts && Number(counts.critical_alerts) > 0),
      kpi("Area changed", counts ? formatArea(counts.changed_area_m2) : ""),
      kpi("Alerts in 24 h", counts ? formatCount(counts.alerts_24h) : ""));
    morph(kpis, next);
  }

  function renderLog() {
    const rows = activityFeed(12);
    if (!rows.length) {
      morph(logList, h("ol.log-list", { "aria-label": "Recent activity" }, h("li.log-row.log-empty", store.loadedOnce.overview ? "Waiting for the next check." : "")));
      logList._keyed = null;
      return;
    }
    if (logList.querySelector(".log-empty")) logList.replaceChildren();
    reconcile(logList, rows, {
      key: (row) => `${row.type}|${row.text}`,
      render: (row) => h("li.log-row", { dataset: { type: row.type } },
        h("time.log-time", { datetime: row.at }, formatClock(row.at)),
        row.area_id
          ? h("a.log-text", { href: `#/site/${encodeURIComponent(row.area_id)}` }, row.origin === "archive_replay" ? "Archive replay: " : "", row.text)
          : h("span.log-text", row.origin === "archive_replay" ? "Archive replay: " : "", row.text)),
      signature: (row) => `${row.at}|${row.text}|${row.origin || ""}`,
    });
  }

  // --------------------------------------------------- lifecycle

  const unsubscribers = [
    on("areas", renderSites),
    on("runs", renderSites),
    on("alerts", renderFeed),
    on("overview", () => {
      renderKpis();
      renderLog();
    }),
    on("fresh-alerts", (batch) => {
      for (const alert of batch) if (alert.area_id) rippleAt(alert.area_id);
    }),
  ];

  renderSites();
  renderFeed();
  renderKpis();
  renderLog();

  return {
    show() {
      ensureMap();
      if (map) {
        map.invalidateSize({ animate: false });
        syncMapLayers();
      }
    },
    hide() {},
    focusArea,
    destroy() {
      unsubscribers.forEach((off) => off());
      if (map) map.remove();
    },
  };
}
