// Application shell: top bar, routing, polling, live stream, alert announcements.

import { h, setChildren, setText } from "./dom.js";
import { icon } from "./icons.js";
import { formatDuration, formatShortDate, parseDate, relativeAge } from "./format.js";
import { chime, isMuted, setMuted } from "./audio.js";
import { connectEvents } from "./sse.js";
import {
  alertPlaceFull, applyAlertEvent, applyDispatch, applyHeartbeat, applyRun, applySceneEvent, on, reconcileRunsFromAreas, refreshAll, setConnection, store,
} from "./state.js";
import { emptyState, onTick, openModal, refreshRelativeTimes, showAlertBanner } from "./ui.js";
import { login, setAuthHandler } from "./api.js";
import { startRouter } from "./router.js";
import { createOperationsView } from "./views/operations.js";
import { mountAlerts } from "./views/alerts.js";
import { mountIncident } from "./views/incident.js";
import { mountSite } from "./views/site.js";
import { mountAddSite } from "./views/addsite.js";
import { mountDepartments } from "./views/departments.js";
import { mountZones } from "./views/zones.js";
import { mountModels } from "./views/models.js";

const POLL_MS = 20000;
const TICK_MS = 20000;

// ------------------------------------------------------------------ top bar

const NAV = [
  { href: "#/", label: "Operations", iconName: "map", routes: ["operations", "site", "add-site"] },
  { href: "#/alerts", label: "Alerts", iconName: "bell", routes: ["alerts", "case"] },
  { href: "#/departments", label: "Departments", iconName: "building", routes: ["departments"] },
  { href: "#/zones", label: "Zones", iconName: "shield", routes: ["zones"] },
  { href: "#/models", label: "Models and data", iconName: "layers", routes: ["models"] },
];

function buildTopbar(host) {
  const navLinks = NAV.map((item) => {
    const link = h("a.nav-link", { href: item.href }, icon(item.iconName), h("span.nav-text", item.label));
    return { ...item, link };
  });
  const schedulerText = h("span.top-status-text");
  const schedulerDot = h("span.dot", { dataset: { health: "CLOUDED" } });
  const sceneText = h("span.top-status-text");
  const connText = h("span.top-status-text");
  const connDot = h("span.dot.conn-dot", { dataset: { conn: store.connection } });
  const clock = h("span.top-status-text.mono");
  const muteButton = h("button.icon-btn", {
    type: "button",
    onclick: () => {
      setMuted(!isMuted());
      paintMute();
    },
  });

  function paintMute() {
    const muted = isMuted();
    muteButton.setAttribute("aria-label", muted ? "Unmute alert sound" : "Mute alert sound");
    muteButton.setAttribute("aria-pressed", muted ? "true" : "false");
    muteButton.title = muted ? "Alert sound off" : "Alert sound on";
    setChildren(muteButton, icon(muted ? "volumeOff" : "volumeOn"));
  }
  paintMute();

  setChildren(host,
    h("a.brand", { href: "#/", "aria-label": "SentinelWatch operations" },
      h("svg", { class: "brand-mark", viewBox: "0 0 20 20", width: 20, height: 20, "aria-hidden": "true" },
        h("circle", { cx: 10, cy: 10, r: 8, fill: "none", stroke: "currentColor", "stroke-width": 1.5 }),
        h("path", { d: "M10 2v5M10 13v5M2 10h5M13 10h5", stroke: "currentColor", "stroke-width": 1.5, "stroke-linecap": "round" }),
        h("circle", { cx: 10, cy: 10, r: 1.6, fill: "currentColor" })),
      h("span.brand-name", "SentinelWatch")),
    h("nav.nav", { "aria-label": "Main" }, navLinks.map((item) => item.link)),
    h("div.top-status", { role: "status", "aria-live": "off" },
      h("span.top-status-item.top-scheduler", schedulerDot, schedulerText),
      h("span.top-status-item.top-scene", sceneText),
      h("span.top-status-item.top-conn", connDot, connText),
      h("span.top-status-item.top-clock", clock)),
    muteButton);

  function paintScheduler() {
    const overview = store.overview;
    if (!overview) {
      setText(schedulerText, store.failed.overview ? "Status unavailable" : "Loading");
      return;
    }
    const counts = overview.counts || {};
    const scheduler = overview.scheduler || {};
    const active = Number(counts.active_areas ?? counts.areas) || 0;
    const running = Number(scheduler.active_runs) || 0;
    const queued = Number(scheduler.queued_runs) || 0;
    const label = `Monitoring ${active} ${active === 1 ? "site" : "sites"}`;
    let tail = "";
    if (running + queued > 0) {
      tail = `${running} running${queued ? `, ${queued} queued` : ""}`;
    } else if (scheduler.enabled === false) {
      tail = "scheduler off";
    } else if (scheduler.next_due_at) {
      const due = parseDate(scheduler.next_due_at);
      if (due) {
        const seconds = (due.getTime() - Date.now()) / 1000;
        tail = seconds <= 0 ? "check due now" : `next check ${formatDuration(seconds)}`;
      }
    }
    setText(schedulerText, tail ? `${label} · ${tail}` : label);
    schedulerDot.dataset.health = scheduler.enabled === false ? "PAUSED" : running + queued > 0 ? "CLOUDED" : "OK";
    if (running + queued > 0) schedulerDot.dataset.health = "OK";
    const lastScene = overview.provider && overview.provider.last_scene_at;
    setText(sceneText, lastScene ? `Last scene ${formatShortDate(lastScene)}, ${relativeAge(lastScene)}` : "No scene received yet");
  }

  function paintConnection() {
    const state = store.connection;
    connDot.dataset.conn = state;
    setText(connText, state === "live" ? "Live" : state === "reconnecting" ? "Reconnecting…" : "Connecting…");
  }

  function paintClock() {
    const now = new Intl.DateTimeFormat("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date());
    setText(clock, `${now} IST`);
  }

  paintScheduler();
  paintConnection();
  paintClock();
  on("overview", paintScheduler);
  on("connection", paintConnection);
  onTick(() => {
    paintScheduler();
    paintClock();
  });

  return {
    setActive(routeName) {
      for (const item of navLinks) {
        if (item.routes.includes(routeName)) item.link.setAttribute("aria-current", "page");
        else item.link.removeAttribute("aria-current");
      }
    },
  };
}

// ------------------------------------------------------------------ routing

const opsHost = document.getElementById("view-ops");
const pageHost = document.getElementById("view-page");
const topbar = buildTopbar(document.getElementById("topbar"));

let opsView = null;
let currentCleanup = null;
let mountToken = 0;

const MOUNTS = {
  alerts: mountAlerts,
  case: mountIncident,
  site: mountSite,
  "add-site": mountAddSite,
  departments: mountDepartments,
  zones: mountZones,
  models: mountModels,
};

const TITLES = {
  operations: "Operations",
  alerts: "Alerts",
  case: "Incident",
  site: "Site",
  "add-site": "Add site",
  departments: "Departments",
  zones: "Authorised zones",
  models: "Models and data",
};

async function runCleanup() {
  const cleanup = currentCleanup;
  currentCleanup = null;
  if (cleanup) {
    try {
      cleanup();
    } catch (error) {
      console.warn("View cleanup skipped", error);
    }
  }
}

async function onRoute(route) {
  const token = ++mountToken;
  await runCleanup();
  topbar.setActive(route.name);
  document.title = `${TITLES[route.name] || "SentinelWatch"} · SentinelWatch`;

  if (route.name === "operations") {
    pageHost.hidden = true;
    pageHost.replaceChildren();
    opsHost.hidden = false;
    if (!opsView) opsView = createOperationsView(opsHost);
    opsView.show();
    return;
  }

  opsHost.hidden = true;
  pageHost.hidden = false;
  pageHost.scrollTop = 0;
  // Each screen renders into its own node. A slow screen the operator already left paints into a
  // detached node and can never overwrite the current one.
  const host = h("div.view-root");
  pageHost.replaceChildren(host);

  if (route.name === "not-found") {
    host.append(h("div.page", emptyState("That page does not exist", "Check the link, or go back to operations.", h("a.btn", { href: "#/" }, "Back to operations"))));
    return;
  }

  try {
    const cleanup = await MOUNTS[route.name](host, route.params, route.query);
    if (token === mountToken) currentCleanup = cleanup || null;
    else if (cleanup) cleanup();
  } catch (error) {
    console.warn("View failed to mount", error);
    if (token === mountToken) {
      host.replaceChildren(h("div.page", emptyState("This screen could not open", "Reload the page, or go back to operations.", h("a.btn", { href: "#/" }, "Back to operations"))));
    }
  }
}

// ------------------------------------------------------------------ sign-in

let signingIn = null;
setAuthHandler(() => {
  if (signingIn) return signingIn;
  signingIn = openModal({
    title: "Sign in to continue",
    build: (close) => {
      const password = h("input.input", { id: "auth-password", type: "password", autocomplete: "current-password", required: true });
      const error = h("p.form-error", { role: "alert", hidden: true });
      return h("form.form", {
        onsubmit: async (event) => {
          event.preventDefault();
          try {
            await login(password.value);
            close(true);
          } catch {
            error.hidden = false;
            error.textContent = "That password was not accepted.";
            password.select();
          }
        },
      },
      h("p.modal-text", "Changes on this deployment need the operator password."),
      h("label.field", { for: "auth-password" }, h("span.field-label", "Password"), password),
      error,
      h("div.modal-actions",
        h("button.btn", { type: "button", onclick: () => close(false) }, "Cancel"),
        h("button.btn.btn-primary", { type: "submit" }, "Sign in")));
    },
  }).then((ok) => {
    signingIn = null;
    return Boolean(ok);
  });
  return signingIn;
});

// ------------------------------------------------------------------ live data

on("fresh-alerts", (batch) => {
  // An authorised activity is a logged notice: it joins the feed without interrupting anyone.
  const loud = batch.filter((alert) => alert.verdict !== "AUTHORISED_ACTIVITY");
  if (!loud.length) return;
  showAlertBanner(loud, alertPlaceFull);
  const worst = ["critical", "high", "medium", "low"].find((level) => loud.some((alert) => alert.severity === level));
  chime(worst || "medium");
});

on("areas", reconcileRunsFromAreas);

const events = connectEvents({
  onState: setConnection,
  onEvent(name, data) {
    switch (name) {
      case "heartbeat":
        applyHeartbeat(data);
        break;
      case "run":
        applyRun(data);
        break;
      case "alert":
        applyAlertEvent(data);
        break;
      case "dispatch":
        applyDispatch(data);
        break;
      case "scene":
        applySceneEvent(data);
        break;
      case "reconnected":
        refreshAll();
        break;
      default:
        break;
    }
  },
});

async function poll() {
  if (!(await refreshAll())) events.kick();
}

poll();
setInterval(poll, POLL_MS);
setInterval(() => refreshRelativeTimes(), TICK_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    poll();
    refreshRelativeTimes();
  }
});

startRouter(onRoute);

