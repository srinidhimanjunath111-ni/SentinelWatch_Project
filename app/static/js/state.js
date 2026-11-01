// Client store. One place holds areas, alerts, live runs and overview; views subscribe to topics and
// patch themselves. Polling and SSE feed the same functions, so a poll with no change emits nothing.

import { api, asList } from "./api.js";
import { channelLabel, cleanText, dispatchStatusLabel, isFinalRun, verdictHeadline } from "./format.js";

const listeners = new Map();
const pendingTopics = new Set();
let flushTimer = null;

export const store = {
  ready: false,
  connection: "connecting",
  overview: null,
  overviewError: null,
  areas: new Map(),
  alerts: new Map(),
  runs: new Map(), // area_id -> latest live run
  localActivity: [],
  system: null,
  loadedOnce: { areas: false, alerts: false, overview: false },
  failed: { areas: null, alerts: null, overview: null },
};

const signatures = { overview: "", areas: new Map(), alerts: new Map() };
const knownAlertIds = new Set();

export function on(topic, fn) {
  if (!listeners.has(topic)) listeners.set(topic, new Set());
  listeners.get(topic).add(fn);
  return () => listeners.get(topic)?.delete(fn);
}

function dispatchTopic(topic, payload) {
  for (const fn of listeners.get(topic) || []) {
    try {
      fn(payload);
    } catch (error) {
      // A rendering problem in one view must not stop the others updating.
      console.warn(`Update for ${topic} skipped`, error);
    }
  }
}

/** Topic updates are coalesced so a burst of events produces one paint. */
function emit(topic) {
  pendingTopics.add(topic);
  if (flushTimer !== null) return;
  flushTimer = setTimeout(() => {
    flushTimer = null;
    const topics = [...pendingTopics];
    pendingTopics.clear();
    topics.forEach((name) => dispatchTopic(name));
  }, 80);
}

export const emitNow = dispatchTopic;

// ---------------------------------------------------------------- normalise

export function departmentName(alert) {
  const dept = alert && alert.department;
  if (!dept) return alert && alert.department_name ? alert.department_name : "";
  return typeof dept === "string" ? dept : dept.name || "";
}

export function departmentId(alert) {
  const dept = alert && alert.department;
  return (dept && typeof dept === "object" && dept.id) || (alert && alert.department_id) || "";
}

export function alertTime(alert) {
  return alert.detected_at || alert.created_at || alert.dispatched_at || null;
}

export function alertPlace(alert) {
  if (alert.area_name) return alert.area_name;
  const area = alert.area_id && store.areas.get(alert.area_id);
  return area ? area.name : alert.title || "";
}

export function alertPlaceFull(alert) {
  return [alertPlace(alert), alert.state].filter(Boolean).join(", ");
}

export const isReplay = (record) => record && record.origin === "archive_replay";

const SEVERITY_RANK = { critical: 4, high: 3, medium: 2, low: 1, none: 0 };
export const severityRank = (value) => SEVERITY_RANK[String(value || "none").toLowerCase()] ?? 0;

const OPEN_STATES = new Set(["open", "dispatched", "acknowledged", "field_verification"]);
export const isOpenAlert = (alert) => OPEN_STATES.has(alert.status);

export function sortedAlerts() {
  return [...store.alerts.values()].sort((a, b) => {
    const left = Date.parse(alertTime(a) || "") || 0;
    const right = Date.parse(alertTime(b) || "") || 0;
    return right - left;
  });
}

/** Newest real scene across sites and the provider, so an archive replay never makes it look older. */
export function newestSceneDate() {
  let best = store.overview && store.overview.provider ? store.overview.provider.last_scene_at : null;
  for (const area of store.areas.values()) {
    const date = area.last_scene && area.last_scene.date;
    if (date && (!best || String(date).slice(0, 10) > String(best).slice(0, 10))) best = date;
  }
  return best;
}

export function sortedAreas() {
  return [...store.areas.values()].sort((a, b) => {
    const rank = (area) => ((area.open_alerts || 0) > 0 ? 0 : area.health === "ERROR" ? 1 : area.status === "PAUSED" ? 3 : 2);
    return rank(a) - rank(b) || String(a.name).localeCompare(String(b.name));
  });
}

// ---------------------------------------------------------------- applying server data

/** A stored run in the shape of an SSE run event. Provenance must survive: a replay stays a replay. */
export function runEvent(run, areaId, areaName) {
  return {
    run_id: run.id || run.run_id,
    area_id: areaId,
    area_name: areaName,
    status: run.status,
    stage: run.stage,
    progress_percent: run.progress_percent,
    message: run.message,
    origin: run.origin,
    replay_id: run.replay_id,
    as_of_date: run.as_of_date,
  };
}

function setArea(area) {
  const sig = JSON.stringify(area);
  if (signatures.areas.get(area.id) === sig) return false;
  signatures.areas.set(area.id, sig);
  store.areas.set(area.id, area);
  const run = area.latest_run;
  if (run && !isFinalRun(run.status)) {
    const current = store.runs.get(area.id);
    // While the stream is live its events are newer than a poll. Without it, the poll is the truth.
    if (!current || store.connection !== "live") {
      store.runs.set(area.id, runEvent(run, area.id, area.name));
      emit("runs");
    }
  }
  return true;
}

export function mergeAreas(list) {
  let changed = false;
  const seen = new Set();
  for (const area of list) {
    seen.add(area.id);
    if (setArea(area)) changed = true;
  }
  for (const id of [...store.areas.keys()]) {
    if (!seen.has(id)) {
      store.areas.delete(id);
      signatures.areas.delete(id);
      changed = true;
    }
  }
  if (changed || !store.loadedOnce.areas) emit("areas");
  store.loadedOnce.areas = true;
}

export function upsertArea(area) {
  if (setArea(area)) emit("areas");
}

export function removeArea(id) {
  store.areas.delete(id);
  signatures.areas.delete(id);
  store.runs.delete(id);
  emit("areas");
}

function setAlert(alert) {
  const prior = store.alerts.get(alert.id);
  const merged = prior ? { ...prior, ...alert } : alert;
  const sig = JSON.stringify(merged);
  if (signatures.alerts.get(alert.id) === sig) return false;
  signatures.alerts.set(alert.id, sig);
  store.alerts.set(alert.id, merged);
  return true;
}

/** Bursts of new alerts arrive here. Everything after the initial load counts as fresh. */
let freshAlerts = [];
let freshTimer = null;
function queueFresh(alert) {
  freshAlerts.push(alert);
  if (freshTimer) return;
  freshTimer = setTimeout(() => {
    const batch = freshAlerts;
    freshAlerts = [];
    freshTimer = null;
    dispatchTopic("fresh-alerts", batch);
  }, 300);
}

export function applyAlert(alert) {
  const isNew = !knownAlertIds.has(alert.id);
  knownAlertIds.add(alert.id);
  const changed = setAlert(alert);
  if (changed) emit("alerts");
  if (isNew && alert.event_kind !== "updated" && store.loadedOnce.alerts) queueFresh(store.alerts.get(alert.id));
}

export function mergeAlerts(list) {
  let changed = false;
  for (const alert of list) {
    const isNew = !knownAlertIds.has(alert.id);
    knownAlertIds.add(alert.id);
    if (setAlert(alert)) changed = true;
    if (isNew && store.loadedOnce.alerts) queueFresh(store.alerts.get(alert.id));
  }
  if (changed || !store.loadedOnce.alerts) emit("alerts");
  store.loadedOnce.alerts = true;
}

export function applyOverview(overview) {
  const { generated_at: _ignored, ...stable } = overview;
  const sig = JSON.stringify(stable);
  store.overview = overview;
  const first = !store.loadedOnce.overview;
  store.loadedOnce.overview = true;
  if (sig === signatures.overview && !first) return;
  signatures.overview = sig;
  const generated = Date.parse(overview.generated_at || "") || 0;
  store.localActivity = store.localActivity.filter((item) => Date.parse(item.at) > generated);
  emit("overview");
}

export function pushActivity(item) {
  const entry = { at: new Date().toISOString(), ...item };
  store.localActivity = [entry, ...store.localActivity].slice(0, 30);
  emit("overview");
}

/** Server activity lines are logs, some with raw channel and status codes. Show them as sentences. */
function readableActivity(row) {
  const match = /^([A-Z_]+) to (.+): ([A-Z_]+)$/.exec(row.text || "");
  if (!match) return { ...row, text: cleanText(row.text) };
  return { ...row, text: `${channelLabel(match[1])} to ${match[2]}: ${dispatchStatusLabel(match[3])}` };
}

export function activityFeed(limit = 20) {
  const server = asList(store.overview && store.overview.activity).map(readableActivity);
  const rows = [...store.localActivity, ...server];
  const seen = new Set();
  const unique = [];
  for (const row of rows) {
    const key = `${row.origin || ""}|${row.area_id || ""}|${row.type}|${row.text}`;
    if (!row.text || seen.has(key)) continue;
    seen.add(key);
    unique.push(row);
  }
  unique.sort((a, b) => (Date.parse(b.at) || 0) - (Date.parse(a.at) || 0));
  return unique.slice(0, limit);
}

// ---------------------------------------------------------------- SSE events

const refreshOverviewSoon = (() => {
  let timer = null;
  return (wait = 700) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      api.overview().then(applyOverview).catch(() => {});
    }, wait);
  };
})();

const refreshAreaSoon = (() => {
  const timers = new Map();
  return (areaId, wait = 500) => {
    clearTimeout(timers.get(areaId));
    timers.set(areaId, setTimeout(() => {
      timers.delete(areaId);
      api.area(areaId).then(upsertArea).catch(() => {});
    }, wait));
  };
})();

export function applyRun(event) {
  if (!event || !event.area_id) return;
  const previous = store.runs.get(event.area_id);
  const final = isFinalRun(event.status) || isFinalRun(event.stage) || (Number(event.progress_percent) >= 100 && event.status !== "RUNNING");
  store.runs.set(event.area_id, { ...event, final, updatedAt: Date.now() });
  if (!previous || previous.stage !== event.stage) {
    if (event.message) pushActivity({ type: "run", origin: event.origin, text: `${event.area_name || "Site"}: ${event.message}`, area_id: event.area_id });
  }
  emit("runs");
  dispatchTopic("run-event", event);
  if (final) {
    refreshAreaSoon(event.area_id);
    refreshOverviewSoon();
    setTimeout(() => {
      const current = store.runs.get(event.area_id);
      if (current && current.final) {
        store.runs.delete(event.area_id);
        emit("runs");
      }
    }, 6000);
  }
}

export function applyDispatch(event) {
  if (!event) return;
  const alert = store.alerts.get(event.alert_id);
  if (alert) {
    const dept = typeof event.department === "object" ? event.department?.name : event.department;
    pushActivity({ type: "dispatch", origin: alert.origin, text: `${alert.area_name || "Alert"} dispatched to ${dept || "department"}`, area_id: alert.area_id });
    refreshAlertSoon(event.alert_id);
  }
  dispatchTopic("dispatch-event", event);
  refreshOverviewSoon();
}

const refreshAlertSoon = (() => {
  const timers = new Map();
  return (alertId) => {
    clearTimeout(timers.get(alertId));
    timers.set(alertId, setTimeout(() => {
      timers.delete(alertId);
      api.alert(alertId).then((full) => {
        if (setAlert(full)) emit("alerts");
      }).catch(() => {});
    }, 400));
  };
})();

export function applySceneEvent(event) {
  if (!event) return;
  dispatchTopic("scene-event", event);
  if (event.area_id) refreshAreaSoon(event.area_id);
  refreshOverviewSoon();
}

export function applyHeartbeat(event) {
  if (!event || !store.overview || !store.overview.scheduler) return;
  const scheduler = store.overview.scheduler;
  if (scheduler.active_runs !== event.active_runs || scheduler.queued_runs !== event.queued_runs) {
    scheduler.active_runs = event.active_runs;
    scheduler.queued_runs = event.queued_runs;
    emit("overview");
  }
}

export function applyAlertEvent(event) {
  // "created" is a new alert. "updated" is a dispatch or a repeat detection merging into it.
  const kind = event.event_kind;
  const alert = event;
  const isNew = !knownAlertIds.has(alert.id);
  applyAlert(alert);
  if (kind !== "updated" && (isNew || kind === "created")) {
    pushActivity({ type: "alert", origin: alert.origin, text: `${verdictHeadline(alert.verdict, alert.category)} at ${alertPlace(alert)}`, area_id: alert.area_id });
  }
  if (alert.area_id) refreshAreaSoon(alert.area_id);
  refreshOverviewSoon(400);
}

export function setConnection(state) {
  if (store.connection === state) return;
  store.connection = state;
  dispatchTopic("connection");
}

// ---------------------------------------------------------------- polling

let polling = false;

/** Resolves false only when no request reached the server at all. */
export async function refreshAll() {
  if (polling) return true;
  polling = true;
  try {
    const [overview, areas, alerts] = await Promise.allSettled([
      api.overview(),
      api.areas(),
      api.alerts({ limit: 100 }),
    ]);
    if (overview.status === "fulfilled") {
      store.failed.overview = null;
      applyOverview(overview.value);
    } else {
      store.failed.overview = overview.reason;
      emit("overview");
    }
    if (areas.status === "fulfilled") {
      store.failed.areas = null;
      mergeAreas(asList(areas.value, "areas"));
    } else {
      store.failed.areas = areas.reason;
      emit("areas");
    }
    if (alerts.status === "fulfilled") {
      store.failed.alerts = null;
      mergeAlerts(asList(alerts.value, "alerts"));
    } else {
      store.failed.alerts = alerts.reason;
      emit("alerts");
    }
    return [overview, areas, alerts].some((result) => result.status === "fulfilled" || result.reason?.status > 0);
  } finally {
    polling = false;
  }
}

/** Runs seen on a poll but no longer active: clear any stale progress. */
export function reconcileRunsFromAreas() {
  for (const [areaId, run] of store.runs) {
    const area = store.areas.get(areaId);
    const latest = area && area.latest_run;
    if (latest && latest.id === run.run_id && isFinalRun(latest.status)) {
      store.runs.delete(areaId);
      emit("runs");
    }
  }
}
