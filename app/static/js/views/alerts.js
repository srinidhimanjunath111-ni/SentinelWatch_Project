// Alerts: filterable table. Filters live in the hash so a filtered list can be linked and survives
// the back button. Live events refresh rows in place.

import { h, debounce, reconcile, setChildren, setText } from "../dom.js";
import { api, asList } from "../api.js";
import {
  ALERT_CATEGORIES, ALERT_STATUSES, SEVERITIES, categoryLabel, formatArea, formatPercent, severityLabel, statusLabel, verdictLabel,
} from "../format.js";
import { alertPlace, alertTime, departmentName, on } from "../state.js";
import { emptyState, errorState, imageBox, relTime, replayBadge, severityTag, skeletonRows, statusTag } from "../ui.js";
import { navigateQuery } from "../router.js";

export async function mountAlerts(root, params, routeQuery) {
  const filters = {
    status: routeQuery.get("status") || "",
    severity: routeQuery.get("severity") || "",
    category: routeQuery.get("category") || "",
    department_id: routeQuery.get("department_id") || "",
    origin: routeQuery.get("origin") || "",
  };

  let departments = [];
  const count = h("span.page-count");
  const tbody = h("tbody");
  const table = h("table.data-table.alerts-table",
    h("thead", h("tr",
      h("th", { scope: "col" }, h("span.sr-only", "Image")),
      h("th", { scope: "col" }, "Severity"),
      h("th", { scope: "col" }, "Category"),
      h("th", { scope: "col" }, "Site"),
      h("th", { scope: "col" }, "Verdict"),
      h("th", { scope: "col" }, "Department"),
      h("th", { scope: "col", class: "num" }, "Area"),
      h("th", { scope: "col", class: "num" }, "Confidence"),
      h("th", { scope: "col" }, "Status"),
      h("th", { scope: "col", "aria-sort": "descending" }, "Detected"))),
    tbody);
  const stateHost = h("div.table-state");
  const filterBar = h("form.filter-bar", { "aria-label": "Filter alerts", onsubmit: (event) => event.preventDefault() });

  function select(name, label, options) {
    const control = h("select.input.input-sm", {
      id: `filter-${name}`,
      onchange: () => {
        filters[name] = control.value;
        navigateQuery(filters);
        load();
      },
    },
    h("option", { value: "" }, `All`),
    options.map((option) => h("option", { value: option.value }, option.label)));
    control.value = filters[name];
    return h("label.filter", { for: `filter-${name}` }, h("span.filter-label", label), control);
  }

  function buildFilters() {
    const reset = h("button.btn.btn-sm.btn-ghost", {
      type: "button",
      onclick: () => {
        Object.keys(filters).forEach((key) => {
          filters[key] = "";
        });
        navigateQuery(filters);
        buildFilters();
        load();
      },
    }, "Clear filters");
    setChildren(filterBar,
      select("status", "Status", ALERT_STATUSES.map((value) => ({ value, label: statusLabel(value) }))),
      select("severity", "Severity", SEVERITIES.map((value) => ({ value, label: severityLabel(value) }))),
      select("category", "Category", ALERT_CATEGORIES.map((value) => ({ value, label: categoryLabel(value) }))),
      select("department_id", "Department", departments.map((dept) => ({ value: dept.id, label: dept.name }))),
      select("origin", "Origin", [{ value: "live", label: "Live" }, { value: "archive_replay", label: "Archive replay" }]),
      Object.values(filters).some(Boolean) ? reset : null);
  }

  const hasFilters = () => Object.values(filters).some(Boolean);

  function renderRow(alert) {
    const href = alert.case_id ? `#/case/${encodeURIComponent(alert.case_id)}` : "#/alerts";
    return h("tr.row-link", {
      dataset: { href },
      onclick: (event) => {
        if (event.target.closest("a, button")) return;
        window.location.hash = href;
      },
    },
    h("td.cell-thumb", imageBox(alert.thumbnail, { alt: "", ratio: "4 / 3", className: "table-thumb", fallback: null })),
    h("td", severityTag(alert.severity)),
    h("td", h("a.table-primary", { href }, categoryLabel(alert.category) || alert.title || "Change detected")),
    h("td", h("span.cell-stack", alertPlace(alert), replayBadge(alert))),
    h("td", verdictLabel(alert.verdict)),
    h("td", departmentName(alert)),
    h("td.num", formatArea(alert.area_m2)),
    h("td.num", formatPercent(alert.confidence)),
    h("td", statusTag(alert.status)),
    h("td", relTime(alertTime(alert))));
  }

  let loadedOnce = false;
  let sequence = 0;

  async function load() {
    const ticket = ++sequence;
    if (!loadedOnce) stateHost.replaceChildren(skeletonRows(8));
    try {
      const rows = asList(await api.alerts({
        status: filters.status,
        severity: filters.severity,
        category: filters.category,
        department_id: filters.department_id,
        limit: 200,
      }), "alerts");
      if (ticket !== sequence) return;
      const visible = rows
        .filter((row) => !filters.origin || row.origin === filters.origin)
        .sort((a, b) => (Date.parse(alertTime(b) || "") || 0) - (Date.parse(alertTime(a) || "") || 0));
      loadedOnce = true;
      setText(count, visible.length ? `${visible.length} ${visible.length === 1 ? "alert" : "alerts"}` : "");
      reconcile(tbody, visible, {
        key: (row) => row.id,
        render: renderRow,
      });
      table.hidden = !visible.length;
      if (visible.length) {
        stateHost.replaceChildren();
      } else if (hasFilters()) {
        stateHost.replaceChildren(emptyState("No alerts match these filters", "Widen a filter, or clear them to see every alert.", h("button.btn", {
          type: "button",
          onclick: () => {
            Object.keys(filters).forEach((key) => {
              filters[key] = "";
            });
            navigateQuery(filters);
            buildFilters();
            load();
          },
        }, "Clear filters")));
      } else {
        stateHost.replaceChildren(emptyState("No alerts yet", "Alerts appear here when a new scene shows material change at a monitored site."));
      }
    } catch (error) {
      if (ticket !== sequence) return;
      if (!loadedOnce) {
        table.hidden = true;
        stateHost.replaceChildren(errorState(error, load, "Could not load alerts"));
      }
    }
  }

  table.hidden = true;
  root.replaceChildren(h("div.page.page-wide",
    h("header.page-head",
      h("div.page-head-main", h("h1.page-title", "Alerts", " ", count))),
    filterBar,
    h("div.table-wrap", table, stateHost)));

  buildFilters();
  load();
  api.departments().then((list) => {
    departments = asList(list, "departments");
    buildFilters();
  }).catch(() => {});

  const refresh = debounce(load, 350);
  const off = on("alerts", refresh);
  return () => {
    off();
    refresh.cancel();
    sequence += 1;
  };
}
