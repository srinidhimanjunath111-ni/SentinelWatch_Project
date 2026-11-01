// Small hand-built SVG charts that follow one spec: thin marks with rounded data ends, a hairline
// recessive grid, text in text colours only, a hover tooltip, and a table view for every chart.

import { h } from "./dom.js";
import { formatShortDate } from "./format.js";

const SERIES = "#6da7ec"; // one series, one colour. Reference line is neutral text colour.

function niceMax(value) {
  if (value <= 0) return 10;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const scaled = value / magnitude;
  const nice = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return nice * magnitude;
}

/**
 * Column chart of daily rainfall.
 * @param rows [{date, value}]
 * @param markers [{date, label}] scene dates drawn as thin neutral rules
 */
export function rainfallChart(rows, { markers = [], unit = "mm", title = "Daily rainfall" } = {}) {
  const width = 560;
  const height = 150;
  const margin = { top: 16, right: 8, bottom: 22, left: 30 };
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const peak = Math.max(...rows.map((row) => row.value), 0);
  const top = niceMax(peak);
  const band = plotW / Math.max(rows.length, 1);
  const barW = Math.max(1.5, Math.min(24, band - 2));
  const y = (value) => margin.top + plotH - (value / top) * plotH;

  const tooltip = h("div.chart-tip", { hidden: true, role: "status" });
  const svg = h("svg", {
    class: "chart-svg",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `${title}. Peak ${peak.toFixed(1)} ${unit}.`,
    preserveAspectRatio: "xMidYMid meet",
  });

  for (const tick of [0, top / 2, top]) {
    svg.appendChild(h("line.chart-grid", { x1: margin.left, x2: width - margin.right, y1: y(tick), y2: y(tick) }));
    svg.appendChild(h("text.chart-tick", { x: margin.left - 6, y: y(tick) + 3.5, "text-anchor": "end" }, tick % 1 === 0 ? tick : tick.toFixed(1)));
  }

  const dateIndex = new Map(rows.map((row, index) => [String(row.date).slice(0, 10), index]));
  for (const marker of markers) {
    const index = dateIndex.get(String(marker.date).slice(0, 10));
    if (index === undefined) continue;
    const x = margin.left + band * index + band / 2;
    svg.appendChild(h("line.chart-marker", { x1: x, x2: x, y1: margin.top - 4, y2: margin.top + plotH }));
    svg.appendChild(h("text.chart-tick.chart-marker-label", {
      x,
      y: margin.top - 7,
      "text-anchor": index < rows.length * 0.15 ? "start" : index > rows.length * 0.85 ? "end" : "middle",
    }, marker.label));
  }

  rows.forEach((row, index) => {
    const x = margin.left + band * index + (band - barW) / 2;
    const barHeight = Math.max(row.value > 0 ? 1 : 0, margin.top + plotH - y(row.value));
    if (barHeight > 0) {
      const radius = Math.min(barW / 2, 4, barHeight);
      const baseline = margin.top + plotH;
      svg.appendChild(h("path.chart-bar", {
        d: `M${x},${baseline}V${baseline - barHeight + radius}Q${x},${baseline - barHeight} ${x + radius},${baseline - barHeight}H${x + barW - radius}Q${x + barW},${baseline - barHeight} ${x + barW},${baseline - barHeight + radius}V${baseline}Z`,
        fill: SERIES,
      }));
    }
    // Hit target spans the whole column, much larger than the mark.
    const hit = h("rect.chart-hit", { x: margin.left + band * index, y: margin.top, width: band, height: plotH, fill: "transparent" });
    hit.addEventListener("pointerenter", () => {
      tooltip.hidden = false;
      tooltip.textContent = `${formatShortDate(row.date)}: ${row.value.toFixed(1)} ${unit}`;
      tooltip.style.left = `${((margin.left + band * index + band / 2) / width) * 100}%`;
    });
    hit.addEventListener("pointerleave", () => {
      tooltip.hidden = true;
    });
    svg.appendChild(hit);
  });

  const first = rows[0];
  const last = rows[rows.length - 1];
  if (first) svg.appendChild(h("text.chart-tick", { x: margin.left, y: height - 6, "text-anchor": "start" }, formatShortDate(first.date)));
  if (last) svg.appendChild(h("text.chart-tick", { x: width - margin.right, y: height - 6, "text-anchor": "end" }, formatShortDate(last.date)));

  const tableRows = rows.filter((row) => row.value > 0);
  const table = h("details.chart-table",
    h("summary", "Show values"),
    tableRows.length
      ? h("table.data-table.data-table-compact",
        h("thead", h("tr", h("th", { scope: "col" }, "Date"), h("th", { scope: "col", class: "num" }, `Rain (${unit})`))),
        h("tbody", tableRows.map((row) => h("tr", h("td", formatShortDate(row.date)), h("td.num", row.value.toFixed(1))))))
      : h("p.muted", "No rain recorded in this window."));

  return h("figure.chart", h("div.chart-canvas", svg, tooltip), table);
}

/** Horizontal data bar for table cells; value text stays in text colour beside it. */
export function metricBar(value, max = 1) {
  const fraction = Math.max(0, Math.min(1, Number(value) / max));
  return h("span.metric-bar", { "aria-hidden": "true" }, h("span.metric-bar-fill", { style: { width: `${fraction * 100}%` } }));
}
