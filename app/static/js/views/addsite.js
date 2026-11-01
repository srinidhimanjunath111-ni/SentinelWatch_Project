// Add site: draw an area on the map, describe it, start monitoring it.

import { h, debounce } from "../dom.js";
import { api, nominatimSearch } from "../api.js";
import { icon } from "../icons.js";
import {
  INTERVALS, MONITOR_CATEGORIES, SITE_TYPES, categoryLabel, errorText, formatArea, siteTypeLabel,
} from "../format.js";
import { upsertArea } from "../state.js";
import { createDrawer, createMap, geodesicAreaKm2, INDIA_BOUNDS } from "../map.js";
import { toast } from "../ui.js";

const MIN_KM2 = 0.5;
const MAX_KM2 = 2500;

const STATES = [
  "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
  "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
  "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
  "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal", "Andaman and Nicobar Islands", "Chandigarh",
  "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
];

export async function mountAddSite(root) {
  let disposed = false;
  let geometry = null;
  let map = null;
  let drawer = null;

  const mapHost = h("div.draw-map", { role: "application", "aria-label": "Draw the monitored area on this map" });
  const sizeNote = h("p.draw-note", { role: "status" }, "Draw the area to monitor, then fill in the details.");

  const name = h("input.input", { id: "site-name", type: "text", required: true, minlength: 2, maxlength: 160, autocomplete: "off" });
  const state = h("select.input", { id: "site-state" },
    h("option", { value: "" }, "Select state"),
    STATES.map((value) => h("option", { value }, value)));
  const district = h("input.input", { id: "site-district", type: "text", maxlength: 120, autocomplete: "off" });
  const type = h("select.input", { id: "site-type" }, SITE_TYPES.map((value) => h("option", { value }, siteTypeLabel(value))));
  const interval = h("select.input", { id: "site-interval" }, INTERVALS.map((item) => h("option", { value: item.minutes }, item.label)));
  interval.value = "1440";
  const sensitivity = h("select.input", { id: "site-sensitivity" },
    h("option", { value: "LOW" }, "Low: larger changes only"),
    h("option", { value: "MEDIUM" }, "Medium"),
    h("option", { value: "HIGH" }, "High: smaller changes"));
  sensitivity.value = "MEDIUM";
  const categoryInputs = MONITOR_CATEGORIES.map((value) => {
    const input = h("input", { type: "checkbox", value, checked: true });
    return { value, input, node: h("label.check", input, h("span", categoryLabel(value))) };
  });
  const formError = h("p.form-error", { role: "alert", hidden: true });
  const submit = h("button.btn.btn-primary", { type: "submit" }, icon("satellite"), "Start monitoring");

  function setDrawMode(kind) {
    if (drawer) drawer.start(kind);
    sizeNote.textContent = kind === "rectangle" ? "Click and drag to draw a rectangle." : "Click to place corners. Click the first corner to finish.";
    sizeNote.dataset.state = "";
  }

  function sizeCheck(km2) {
    if (km2 < MIN_KM2) return `This area is ${formatArea(km2 * 1e6)}. Draw at least ${MIN_KM2} km² so changes clear the 10 m pixel grid.`;
    if (km2 > MAX_KM2) return `This area is ${formatArea(km2 * 1e6)}. Keep a site under ${MAX_KM2.toLocaleString("en-IN")} km²: split a large region into several sites.`;
    return null;
  }

  function onGeometry(next) {
    geometry = next;
    if (!next) {
      sizeNote.textContent = "Draw the area to monitor, then fill in the details.";
      sizeNote.dataset.state = "";
      return;
    }
    const km2 = geodesicAreaKm2(next);
    const problem = sizeCheck(km2);
    sizeNote.textContent = problem || `Area ${formatArea(km2 * 1e6)}.`;
    sizeNote.dataset.state = problem ? "error" : "ok";
  }

  // Place search. Nominatim failing just means no suggestions.
  const searchResults = h("ul.search-results", { hidden: true, role: "listbox", "aria-label": "Place results" });
  const searchInput = h("input.input.search-input", {
    type: "search",
    placeholder: "Search for a place in India",
    "aria-label": "Search for a place",
    autocomplete: "off",
    onkeydown: (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        runSearch.cancel();
        doSearch();
      } else if (event.key === "Escape") {
        searchResults.hidden = true;
      }
    },
    oninput: () => runSearch(),
  });

  let controller = null;
  async function doSearch() {
    const text = searchInput.value.trim();
    if (controller) controller.abort();
    if (text.length < 3) {
      searchResults.hidden = true;
      return;
    }
    controller = new AbortController();
    const rows = await nominatimSearch(text, controller.signal);
    if (disposed) return;
    searchResults.replaceChildren(...rows.map((row) => h("li",
      h("button.search-result", {
        type: "button",
        role: "option",
        onclick: () => {
          searchResults.hidden = true;
          searchInput.value = row.display_name;
          applyPlace(row);
        },
      }, row.display_name))));
    searchResults.hidden = rows.length === 0;
  }
  const runSearch = debounce(doSearch, 450);

  function applyPlace(row) {
    if (!map) return;
    const box = (row.boundingbox || []).map(Number);
    if (box.length === 4 && box.every(Number.isFinite)) {
      map.fitBounds([[box[0], box[2]], [box[1], box[3]]], { maxZoom: 13, animate: true, padding: [30, 30] });
    } else if (Number.isFinite(Number(row.lat))) {
      map.setView([Number(row.lat), Number(row.lon)], 12);
    }
    const address = row.address || {};
    if (!name.value) name.value = String(row.name || row.display_name || "").split(",")[0].slice(0, 160);
    if (!district.value) district.value = address.state_district || address.county || "";
    const stateName = address.state;
    if (stateName && STATES.includes(stateName)) state.value = stateName;
  }

  async function onSubmit(event) {
    event.preventDefault();
    formError.hidden = true;
    if (!geometry) {
      return showError("Draw the area on the map first.");
    }
    const km2 = geodesicAreaKm2(geometry);
    const sizeProblem = sizeCheck(km2);
    if (sizeProblem) return showError(sizeProblem);
    const categories = categoryInputs.filter((item) => item.input.checked).map((item) => item.value);
    if (!categories.length) return showError("Select at least one category to watch for.");
    const interval_ = Number(interval.value);
    submit.disabled = true;
    try {
      const created = await api.createArea({
        name: name.value.trim(),
        state: state.value || null,
        district: district.value.trim() || null,
        site_type: type.value,
        geometry,
        categories,
        monitoring_interval_minutes: interval_,
        sensitivity: sensitivity.value,
      });
      upsertArea(created);
      toast(`Monitoring ${created.name}.`, "ok");
      window.location.hash = `#/site/${encodeURIComponent(created.id)}`;
    } catch (error) {
      showError(errorText(error, "The site could not be saved. Try again."));
      submit.disabled = false;
    }
    return undefined;
  }

  function showError(message) {
    formError.hidden = false;
    formError.textContent = message;
  }

  root.replaceChildren(h("div.draw-page",
    h("div.draw-map-col",
      mapHost,
      h("div.draw-tools",
        h("div.draw-search", icon("search"), searchInput, searchResults),
        h("div.seg-group", { role: "group", "aria-label": "Drawing tools" },
          h("button.seg", { type: "button", onclick: () => setDrawMode("rectangle") }, icon("rectangle", { size: 14 }), "Rectangle"),
          h("button.seg", { type: "button", onclick: () => setDrawMode("polygon") }, icon("polygon", { size: 14 }), "Polygon"),
          h("button.seg", { type: "button", onclick: () => drawer && drawer.clear() }, "Clear"))),
      sizeNote),
    h("form.draw-form", { onsubmit: onSubmit, "aria-label": "Site details" },
      h("nav.crumbs", { "aria-label": "Breadcrumb" }, h("a", { href: "#/" }, icon("chevronLeft", { size: 12 }), "Operations")),
      h("h1.page-title", "Add site"),
      h("label.field", { for: "site-name" }, h("span.field-label", "Name"), name),
      h("div.form-row",
        h("label.field", { for: "site-state" }, h("span.field-label", "State"), state),
        h("label.field", { for: "site-district" }, h("span.field-label", "District"), district)),
      h("div.form-row",
        h("label.field", { for: "site-type" }, h("span.field-label", "Type"), type),
        h("label.field", { for: "site-interval" }, h("span.field-label", "Check catalogue"), interval)),
      h("p.form-note", "Sentinel-2 images a location every 2 to 5 days, so a new scene arrives only when the satellites have passed and the view is clear."),
      h("label.field", { for: "site-sensitivity" }, h("span.field-label", "Sensitivity"), sensitivity),
      h("fieldset.field.checks",
        h("legend.field-label", "Watch for"),
        h("div.check-grid", categoryInputs.map((item) => item.node))),
      formError,
      h("div.form-actions",
        h("a.btn", { href: "#/" }, "Cancel"),
        submit))));

  if (window.L) {
    try {
      map = createMap(mapHost);
      map.fitBounds(INDIA_BOUNDS, { animate: false });
      drawer = createDrawer(map, { onChange: onGeometry });
    } catch {
      sizeNote.textContent = "The map could not load. Reload the page to draw a site.";
    }
  }
  requestAnimationFrame(() => {
    if (map && !disposed) map.invalidateSize({ animate: false });
  });

  return () => {
    disposed = true;
    runSearch.cancel();
    if (controller) controller.abort();
    if (drawer) drawer.destroy();
    if (map) map.remove();
  };
}
