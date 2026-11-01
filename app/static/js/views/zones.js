// Permits and authorised zones: the register the authorisation test checks against.

import { h, reconcile } from "../dom.js";
import { api, asList } from "../api.js";
import { icon } from "../icons.js";
import { ZONE_TYPES, errorText, formatArea, formatDate, zoneTypeLabel } from "../format.js";
import { createDrawer, createMap, geodesicAreaKm2, geometryBounds, INDIA_BOUNDS, polygonStyle, fitTo } from "../map.js";
import { confirmModal, emptyState, errorState, illustrativeTag, skeletonRows, toast } from "../ui.js";

const L = () => window.L;
const ZONE_COLOUR = "#7fb2ff";

export async function mountZones(root) {
  let disposed = false;
  let zones = [];
  let geometry = null;
  let map = null;
  let drawer = null;
  const zoneLayers = new Map();

  const mapHost = h("div.zones-map", { role: "application", "aria-label": "Authorised zones on a satellite map. Draw a new zone here." });
  const listHost = h("ul.zone-list", { "aria-label": "Authorised zones" });
  const listState = h("div");
  const drawNote = h("p.draw-note", { role: "status" }, "Draw a zone on the map to add it to the register.");

  const name = h("input.input", { id: "zone-name", type: "text", required: true, minlength: 2, maxlength: 160, autocomplete: "off" });
  const type = h("select.input", { id: "zone-type" }, ZONE_TYPES.map((value) => h("option", { value }, zoneTypeLabel(value))));
  const authority = h("input.input", { id: "zone-authority", type: "text", maxlength: 160, autocomplete: "off" });
  const reference = h("input.input", { id: "zone-reference", type: "text", maxlength: 160, autocomplete: "off" });
  const from = h("input.input", { id: "zone-from", type: "date" });
  const to = h("input.input", { id: "zone-to", type: "date" });
  const source = h("input.input", { id: "zone-source", type: "text", maxlength: 240, autocomplete: "off" });
  const illustrative = h("input", { type: "checkbox" });
  const formError = h("p.form-error", { role: "alert", hidden: true });
  const submit = h("button.btn.btn-primary", { type: "submit" }, "Add zone");

  function onGeometry(next) {
    geometry = next;
    if (!next) {
      drawNote.textContent = "Draw a zone on the map to add it to the register.";
      return;
    }
    drawNote.textContent = `Zone area ${formatArea(geodesicAreaKm2(next) * 1e6)}. Fill in the details to save it.`;
  }

  function fail(message) {
    formError.hidden = false;
    formError.textContent = message;
  }

  async function onSubmit(event) {
    event.preventDefault();
    formError.hidden = true;
    if (!geometry) return fail("Draw the zone on the map first.");
    if (from.value && to.value && from.value > to.value) return fail("The valid-from date must be before the valid-to date.");
    submit.disabled = true;
    try {
      await api.createZone({
        name: name.value.trim(),
        zone_type: type.value,
        geometry,
        authority: authority.value.trim() || null,
        reference: reference.value.trim() || null,
        valid_from: from.value || null,
        valid_to: to.value || null,
        source: source.value.trim() || null,
        is_illustrative: illustrative.checked,
      });
      [name, authority, reference, from, to, source].forEach((input) => {
        input.value = "";
      });
      illustrative.checked = false;
      if (drawer) drawer.clear();
      toast("Zone added to the register.", "ok");
      await load();
    } catch (error) {
      fail(errorText(error, "The zone could not be saved. Try again."));
    } finally {
      submit.disabled = false;
    }
    return undefined;
  }

  async function removeZone(id) {
    const zone = zones.find((item) => item.id === id);
    if (!zone) return;
    const ok = await confirmModal({
      title: "Remove zone",
      message: `Remove ${zone.name} from the register? Future detections inside it will no longer count as authorised.`,
      confirmLabel: "Remove zone",
      danger: true,
    });
    if (!ok || disposed) return;
    try {
      await api.deleteZone(id);
      toast("Zone removed.", "ok");
      await load();
    } catch (error) {
      toast(errorText(error, "The zone could not be removed."), "error");
    }
  }

  function focusZone(id) {
    const zone = zones.find((item) => item.id === id);
    const bounds = zone && geometryBounds(zone.geometry);
    if (map && bounds) map.flyToBounds(bounds, { padding: [40, 40], maxZoom: 15, duration: 0.9 });
  }

  function renderZone(zone) {
    const validity = [zone.valid_from ? formatDate(zone.valid_from) : null, zone.valid_to ? formatDate(zone.valid_to) : null].filter(Boolean).join(" to ");
    return h("li.zone-row",
      h("button.zone-main", { type: "button", onclick: () => focusZone(zone.id) },
        h("span.zone-top",
          h("span.zone-name", zone.name),
          zone.is_illustrative ? illustrativeTag() : null),
        h("span.zone-meta", [zoneTypeLabel(zone.zone_type), zone.authority].filter(Boolean).join(" · ")),
        zone.reference ? h("span.zone-meta.mono", zone.reference) : null,
        validity ? h("span.zone-meta", validity) : null,
        zone.source ? h("span.zone-meta", zone.source) : null),
      h("button.icon-btn", { type: "button", "aria-label": `Remove ${zone.name}`, title: "Remove zone", onclick: () => removeZone(zone.id) }, icon("trash", { size: 14 })));
  }

  function syncLayers() {
    if (!map) return;
    const ids = new Set(zones.map((zone) => zone.id));
    for (const [id, layer] of zoneLayers) {
      if (!ids.has(id)) {
        layer.remove();
        zoneLayers.delete(id);
      }
    }
    for (const zone of zones) {
      if (zoneLayers.has(zone.id) || !zone.geometry) continue;
      try {
        const layer = L().geoJSON(zone.geometry, { style: () => polygonStyle(ZONE_COLOUR, { fillOpacity: 0.12 }) });
        const label = document.createElement("span");
        label.textContent = zone.name;
        layer.bindTooltip(label, { sticky: true, className: "map-tooltip" });
        layer.addTo(map);
        zoneLayers.set(zone.id, layer);
      } catch {
        // A malformed zone geometry stays in the list but is not drawn.
      }
    }
  }

  let fitted = false;
  async function load() {
    try {
      zones = asList(await api.zones(), "zones");
    } catch (error) {
      if (disposed) return;
      listState.replaceChildren(errorState(error, load, "Could not load the zone register"));
      return;
    }
    if (disposed) return;
    reconcile(listHost, zones, { key: (zone) => zone.id, render: renderZone });
    listState.replaceChildren(...(zones.length ? [] : [emptyState("No zones registered", "A detection inside a registered zone of a matching type is logged as authorised rather than dispatched.")]));
    syncLayers();
    if (!fitted && map && zones.length) {
      fitted = true;
      const all = zones.map((zone) => geometryBounds(zone.geometry)).filter(Boolean);
      if (all.length) fitTo(map, all.reduce((acc, item) => acc.extend(item), L().latLngBounds([])), { padding: [40, 40], maxZoom: 12 });
    }
  }

  root.replaceChildren(h("div.zones-page",
    h("div.draw-map-col",
      mapHost,
      h("div.draw-tools",
        h("div.seg-group", { role: "group", "aria-label": "Drawing tools" },
          h("button.seg", { type: "button", onclick: () => drawer && drawer.start("rectangle") }, icon("rectangle", { size: 14 }), "Rectangle"),
          h("button.seg", { type: "button", onclick: () => drawer && drawer.start("polygon") }, icon("polygon", { size: 14 }), "Polygon"),
          h("button.seg", { type: "button", onclick: () => drawer && drawer.clear() }, "Clear"))),
      drawNote),
    h("div.zones-side",
      h("h1.page-title", "Permits and authorised zones"),
      h("section", { "aria-label": "Register" }, h("h2.section-label", "Register"), listHost, listState),
      h("form.form.zone-form", { onsubmit: onSubmit, "aria-label": "Add a zone" },
        h("h2.section-label", "Add a zone"),
        h("label.field", { for: "zone-name" }, h("span.field-label", "Name"), name),
        h("div.form-row",
          h("label.field", { for: "zone-type" }, h("span.field-label", "Type"), type),
          h("label.field", { for: "zone-authority" }, h("span.field-label", "Issuing authority"), authority)),
        h("label.field", { for: "zone-reference" }, h("span.field-label", "Permit reference"), reference),
        h("div.form-row",
          h("label.field", { for: "zone-from" }, h("span.field-label", "Valid from"), from),
          h("label.field", { for: "zone-to" }, h("span.field-label", "Valid to"), to)),
        h("label.field", { for: "zone-source" }, h("span.field-label", "Source of record"), source),
        h("label.check", illustrative, h("span", "Illustrative record, not an issued permit")),
        formError,
        h("div.form-actions", submit)))));

  listState.append(skeletonRows(3));

  if (window.L) {
    try {
      map = createMap(mapHost);
      map.fitBounds(INDIA_BOUNDS, { animate: false });
      drawer = createDrawer(map, { colour: "#f0c674", onChange: onGeometry });
    } catch {
      drawNote.textContent = "The map could not load. Reload the page to draw a zone.";
    }
  }
  requestAnimationFrame(() => {
    if (map && !disposed) map.invalidateSize({ animate: false });
  });
  load();

  return () => {
    disposed = true;
    if (drawer) drawer.destroy();
    if (map) map.remove();
  };
}
