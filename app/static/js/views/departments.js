// Departments: each department's inbox as they would see it, plus the outbound dispatch log.

import { h, debounce, morph, reconcile } from "../dom.js";
import { api, asList } from "../api.js";
import { icon } from "../icons.js";
import {
  categoryLabel, channelLabel, dispatchLine, formatArea, formatDateTime, verdictLabel,
} from "../format.js";
import { alertPlace, alertTime, isOpenAlert, on } from "../state.js";
import { emptyState, errorState, imageBox, relTime, replayBadge, severityTag, skeletonRows, statusTag } from "../ui.js";

function rowsFromInbox(body) {
  return asList(body, "alerts", "inbox");
}

function dispatchState(alert) {
  const rows = asList(alert.dispatches || alert.notifications);
  if (!rows.length) return null;
  // The email attempt says more about delivery than the inbox record written alongside it.
  const outbound = rows.filter((row) => String(row.channel).toUpperCase() !== "IN_APP");
  const pool = outbound.length ? outbound : rows;
  return [...pool].sort((a, b) => (Date.parse(b.at || b.created_at || "") || 0) - (Date.parse(a.at || a.created_at || "") || 0))[0];
}

export async function mountDepartments(root, params) {
  let disposed = false;
  let departments = [];
  const counts = new Map();
  let selectedId = params.id || null;

  const deptList = h("ul.dept-list", { "aria-label": "Departments" });
  const inboxHost = h("div.dept-inbox");
  const logHost = h("div");

  root.replaceChildren(h("div.page.page-wide",
    h("header.page-head", h("div.page-head-main", h("h1.page-title", "Departments"))),
    skeletonRows(5)));

  function renderDept(dept) {
    const count = counts.get(dept.id) || { total: 0, open: 0 };
    return h("li", h("a.dept-row", {
      href: `#/departments/${encodeURIComponent(dept.id)}`,
      "aria-current": dept.id === selectedId ? "true" : null,
    },
    h("span.dept-name", dept.name),
    dept.recipient_email ? h("span.dept-mail.mono", dept.recipient_email) : null,
    h("span.dept-counts",
      h("span", `${count.open} open`),
      h("span.muted", `${count.total} routed`))));
  }

  function renderDeptList() {
    reconcile(deptList, departments, {
      key: (dept) => dept.id,
      render: renderDept,
      signature: (dept) => JSON.stringify([dept, counts.get(dept.id), dept.id === selectedId]),
    });
  }

  function renderInboxRow(alert) {
    const latest = dispatchState(alert);
    return h("li.inbox-row",
      h("a.inbox-link", { href: alert.case_id ? `#/case/${encodeURIComponent(alert.case_id)}` : "#/alerts" },
        imageBox(alert.thumbnail, { alt: "", ratio: "4 / 3", className: "inbox-thumb", fallback: null }),
        h("span.inbox-body",
          h("span.inbox-top", severityTag(alert.severity), statusTag(alert.status), replayBadge(alert), relTime(alertTime(alert), "inbox-age")),
          h("span.inbox-title", `${categoryLabel(alert.category) || "Change"} at ${alertPlace(alert)}`),
          h("span.inbox-meta",
            [verdictLabel(alert.verdict), formatArea(alert.area_m2)].filter(Boolean).join(" · ")),
          latest ? h("span.inbox-dispatch", icon("mail", { size: 12 }), dispatchLine(latest)) : null)));
  }

  let inboxSeq = 0;
  async function loadInbox() {
    const dept = departments.find((item) => item.id === selectedId);
    if (!dept) {
      inboxHost.replaceChildren(emptyState("Select a department", "Choose a department to see its inbox."));
      return;
    }
    const ticket = ++inboxSeq;
    const header = h("header.panel-head",
      h("h2.panel-title", dept.name),
      dept.recipient_email ? h("span.panel-sub.mono", dept.recipient_email) : null);
    const existing = inboxHost.querySelector(".inbox-list");
    if (!existing || inboxHost.dataset.dept !== dept.id) {
      inboxHost.dataset.dept = dept.id;
      inboxHost.replaceChildren(header, skeletonRows(4));
    }
    try {
      const rows = rowsFromInbox(await api.inbox(dept.id)).sort((a, b) => (Date.parse(alertTime(b) || "") || 0) - (Date.parse(alertTime(a) || "") || 0));
      if (disposed || ticket !== inboxSeq) return;
      counts.set(dept.id, { total: rows.length, open: rows.filter(isOpenAlert).length });
      renderDeptList();
      if (!rows.length) {
        inboxHost.replaceChildren(header, h("p.panel-empty", "Inbox is empty. Nothing has been routed to this department."));
        return;
      }
      let list = inboxHost.querySelector(".inbox-list");
      if (!list) {
        list = h("ul.inbox-list");
        inboxHost.replaceChildren(header, list);
      }
      reconcile(list, rows, { key: (row) => row.id, render: renderInboxRow });
    } catch (error) {
      if (disposed || ticket !== inboxSeq) return;
      inboxHost.replaceChildren(header, errorState(error, loadInbox, "Could not load this inbox"));
    }
  }

  async function loadCounts() {
    await Promise.all(departments.map(async (dept) => {
      try {
        const rows = rowsFromInbox(await api.inbox(dept.id));
        counts.set(dept.id, { total: rows.length, open: rows.filter(isOpenAlert).length });
      } catch {
        // Counts stay as last known.
      }
    }));
    if (!disposed) renderDeptList();
  }

  async function loadLog() {
    try {
      const rows = asList(await api.dispatches({ limit: 50 }), "dispatches");
      if (disposed) return;
      const next = h("section.panel", { "aria-label": "Dispatch log" },
        h("header.panel-head", h("h2.panel-title", "Dispatch log")),
        rows.length
          ? h("div.table-scroll", h("table.data-table.data-table-compact",
            h("thead", h("tr",
              h("th", { scope: "col" }, "Time"),
              h("th", { scope: "col" }, "Department"),
              h("th", { scope: "col" }, "Channel"),
              h("th", { scope: "col" }, "Destination"),
              h("th", { scope: "col" }, "State"))),
            h("tbody", rows.map((row) => h("tr",
              h("td", formatDateTime(row.at || row.created_at)),
              h("td", typeof row.department === "object" && row.department ? row.department.name : row.department || row.department_name || ""),
              h("td", channelLabel(row.channel)),
              h("td.mono", row.destination || ""),
              h("td", dispatchLine(row)))))))
          : h("p.panel-empty", "Nothing has been dispatched yet."));
      if (logHost.firstElementChild) morph(logHost.firstElementChild, next);
      else logHost.appendChild(next);
    } catch (error) {
      if (disposed) return;
      if (!logHost.firstElementChild) logHost.append(errorState(error, loadLog, "Could not load the dispatch log"));
    }
  }

  async function load() {
    try {
      departments = asList(await api.departments(), "departments");
    } catch (error) {
      if (disposed) return;
      root.replaceChildren(h("div.page", errorState(error, load, "Could not load departments")));
      return;
    }
    if (disposed) return;
    if (!selectedId && departments.length) selectedId = departments[0].id;
    root.replaceChildren(h("div.page.page-wide",
      h("header.page-head", h("div.page-head-main", h("h1.page-title", "Departments"))),
      departments.length
        ? h("div.dept-grid",
          h("nav.panel.dept-nav", { "aria-label": "Departments" }, deptList),
          h("section.panel", { "aria-label": "Inbox" }, inboxHost))
        : emptyState("No departments yet", "A department appears here when an alert is first routed to it."),
      logHost));
    renderDeptList();
    loadCounts();
    loadInbox();
    loadLog();
  }

  load();

  const refresh = debounce(() => {
    loadInbox();
    loadCounts();
    loadLog();
  }, 500);
  const offs = [on("alerts", refresh), on("dispatch-event", refresh)];

  return () => {
    disposed = true;
    refresh.cancel();
    offs.forEach((off) => off());
  };
}
