// One icon set: 16 unit grid, 1.5 stroke, round joins, no fills except where the shape is the mark.
import { h } from "./dom.js";

const PATHS = {
  map: ["M1.5 4 6 2l4 2 4.5-2v10L10 14l-4-2-4.5 2z", "M6 2v10", "M10 4v10"],
  bell: ["M4 11V7a4 4 0 1 1 8 0v4l1.2 1.5H2.8z", "M6.6 14a1.5 1.5 0 0 0 2.8 0"],
  building: ["M2.5 14V3.5l6-1.5v12", "M8.5 6l5 1.5V14", "M1.5 14h13", "M5 5.5v.01", "M5 8.5v.01", "M5 11.5v.01", "M11 9.5v.01", "M11 12v.01"],
  shield: ["M8 1.8 13.2 3.6v4c0 3.2-2.1 5.4-5.2 6.6-3.1-1.2-5.2-3.4-5.2-6.6v-4z", "m5.8 8 1.6 1.6 2.9-3.1"],
  layers: ["M8 1.8 14.2 5 8 8.2 1.8 5z", "M2.2 8 8 11l5.8-3", "M2.2 11 8 14l5.8-3"],
  plus: ["M8 3v10", "M3 8h10"],
  x: ["m4 4 8 8", "m12 4-8 8"],
  check: ["m3 8.5 3.2 3.2L13 4.8"],
  search: ["M7 12a5 5 0 1 0 0-10 5 5 0 0 0 0 10z", "m10.7 10.7 3.3 3.3"],
  refresh: ["M13.5 8a5.5 5.5 0 1 1-1.6-3.9", "M13.5 2v3h-3"],
  pause: ["M5.5 3v10", "M10.5 3v10"],
  play: ["M5 3v10l8-5z"],
  history: ["M2.5 8a5.5 5.5 0 1 0 1.6-3.9", "M2.5 2v3h3", "M8 5.2V8l2 1.3"],
  download: ["M8 2.5v8", "m4.8 7.5 3.2 3.2 3.2-3.2", "M2.5 13.5h11"],
  volumeOn: ["M2.5 6v4h2.6L8.5 13V3L5.1 6z", "M11 5.8a3 3 0 0 1 0 4.4", "M12.8 4a5.5 5.5 0 0 1 0 8"],
  volumeOff: ["M2.5 6v4h2.6L8.5 13V3L5.1 6z", "m11 6.3 3 3.4", "m14 6.3-3 3.4"],
  chevronRight: ["m6 3.5 4.5 4.5L6 12.5"],
  chevronLeft: ["M10 3.5 5.5 8l4.5 4.5"],
  arrowRight: ["M2.5 8h11", "m9.5 4 4 4-4 4"],
  external: ["M6.5 3H3v10h10V9.5", "M9.5 2.5h4v4", "m13.5 2.5-6 6"],
  cloud: ["M4.5 12.5a3 3 0 0 1-.4-6 4 4 0 0 1 7.7-.5 3.3 3.3 0 0 1-.3 6.5z"],
  alert: ["M8 2 14.5 13.5h-13z", "M8 6.5v3.2", "M8 11.8v.01"],
  info: ["M8 14.2A6.2 6.2 0 1 0 8 1.8a6.2 6.2 0 0 0 0 12.4z", "M8 7.3v3.6", "M8 5v.01"],
  mail: ["M2 3.5h12v9H2z", "m2 4 6 5 6-5"],
  send: ["M14 2 7 9", "M14 2 9.5 14 7 9 2 6.5z"],
  polygon: ["M8 2.2 13.5 6.3l-2 6.9h-7l-2-6.9z"],
  archive: ["M2 3h12v3H2z", "M3 6v7.5h10V6", "M6.3 9h3.4"],
  flag: ["M3.5 14.5v-13", "M3.5 2.5h9l-2 3 2 3h-9"],
  trash: ["M2.5 4.2h11", "M6 4V2.5h4V4", "M4 4.2l.7 9.3h6.6l.7-9.3"],
  satellite: ["m6.2 9.8-2-2 3.6-3.6 2 2z", "m7.4 3.6 1.9-1.9 2.4 2.4-1.9 1.9", "M4 5.9 2.1 7.8l2.4 2.4 1.9-1.9", "m9 10.5 1.2 1.2", "M11.3 8.5a2.5 2.5 0 0 1-.1 3.3 2.5 2.5 0 0 1-3.3.1", "M13.5 9a4.8 4.8 0 0 1-1 3.5 4.8 4.8 0 0 1-3.5 1"],
  link: ["M6.5 9.5a2.8 2.8 0 0 0 4 0l2.2-2.2a2.8 2.8 0 1 0-4-4L7.8 4.2", "M9.5 6.5a2.8 2.8 0 0 0-4 0L3.3 8.7a2.8 2.8 0 1 0 4 4l.9-.9"],
  circleCheck: ["M8 14.2A6.2 6.2 0 1 0 8 1.8a6.2 6.2 0 0 0 0 12.4z", "m5.4 8.2 1.8 1.8 3.5-3.7"],
  rectangle: ["M2.5 3.5h11v9h-11z"],
};

export function icon(name, { size = 16, label = null, className = "" } = {}) {
  const svg = h("svg", {
    class: `icon ${className}`.trim(),
    viewBox: "0 0 16 16",
    width: size,
    height: size,
    fill: "none",
    stroke: "currentColor",
    "stroke-width": 1.5,
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": label ? null : "true",
    role: label ? "img" : null,
    "aria-label": label,
  });
  for (const d of PATHS[name] || []) svg.appendChild(h("path", { d }));
  return svg;
}

/** Severity is encoded by shape as well as colour, so it survives colour-blindness and projectors. */
export function severityGlyph(severity, size = 10) {
  const level = String(severity || "none").toLowerCase();
  const shapes = {
    critical: h("polygon", { points: "5,0.5 9.5,5 5,9.5 0.5,5", fill: "currentColor" }),
    high: h("polygon", { points: "5,1 9.6,9 0.4,9", fill: "currentColor" }),
    medium: h("circle", { cx: 5, cy: 5, r: 3.8, fill: "currentColor" }),
    low: h("circle", { cx: 5, cy: 5, r: 3.2, fill: "none", stroke: "currentColor", "stroke-width": 1.6 }),
    none: h("circle", { cx: 5, cy: 5, r: 2, fill: "currentColor" }),
  };
  return h("svg", {
    class: `sev-glyph sev-${shapes[level] ? level : "none"}`,
    viewBox: "0 0 10 10",
    width: size,
    height: size,
    "aria-hidden": "true",
  }, shapes[level] || shapes.none);
}
