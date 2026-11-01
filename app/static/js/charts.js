// Small hand-built charts following one spec: thin marks with rounded data ends, direct labels,
// text in text colours only, and emphasis on the one bar the story is about.

import { h } from "./dom.js";

/**
 * Rainfall in the window before the scene against the same window in earlier years. One measure on
 * one axis: the current bar takes the series colour and earlier years stay neutral.
 * @param rows [{label, value, current}]
 */
export function rainfallBars(rows, { unit = "mm", normal = null } = {}) {
  const peak = Math.max(...rows.map((row) => row.value), normal || 0, 1);
  const position = (value) => `${(value / peak) * 100}%`;
  return h("figure.bars", { role: "img", "aria-label": rows.map((row) => `${row.label}: ${row.value} ${unit}`).join(". ") },
    rows.map((row) => h("div.bar-row", { class: row.current ? "is-current" : null, title: `${row.label}: ${row.value.toFixed(1)} ${unit}` },
      h("span.bar-label", row.label),
      h("span.bar-track",
        h("span.bar-fill", { style: { width: position(row.value) } }),
        normal !== null ? h("span.bar-normal", { style: { left: position(normal) } }) : null),
      h("span.bar-value", `${row.value.toFixed(0)} ${unit}`))));
}

/** Horizontal data bar for table cells; value text stays in text colour beside it. */
export function metricBar(value, max = 1) {
  const fraction = Math.max(0, Math.min(1, Number(value) / max));
  return h("span.metric-bar", { "aria-hidden": "true" }, h("span.metric-bar-fill", { style: { width: `${fraction * 100}%` } }));
}
