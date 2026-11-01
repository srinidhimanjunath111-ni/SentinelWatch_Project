// Before/after swipe comparison over real evidence images.

import { h } from "../dom.js";
import { formatDate } from "../format.js";

function imageLayer(src, alt, className = "") {
  const img = h("img", { class: `cmp-img ${className}`.trim(), alt, draggable: "false", decoding: "async" });
  return { img, src };
}

function aspectFromBounds(bounds) {
  if (!Array.isArray(bounds) || bounds.length !== 2) return null;
  const [[south, west], [north, east]] = bounds;
  const numbers = [south, west, north, east].map(Number);
  if (numbers.some((value) => !Number.isFinite(value))) return null;
  const height = numbers[2] - numbers[0];
  const width = (numbers[3] - numbers[1]) * Math.cos(((numbers[0] + numbers[2]) / 2) * (Math.PI / 180));
  if (height <= 0 || width <= 0) return null;
  return width / height;
}

/**
 * @param images {before, after, overlay, ndvi_change}
 * @param meta {beforeDate, afterDate, bounds, findings[] with geometry}
 */
export function createCompare({ images, beforeDate, afterDate, bounds, polygons = [], overlayDefault = true }) {
  let position = 50;
  let dragging = false;

  const before = imageLayer(images.before, beforeDate ? `Scene before change, ${formatDate(beforeDate)}` : "Scene before change");
  const after = imageLayer(images.after, afterDate ? `Scene after change, ${formatDate(afterDate)}` : "Scene after change");
  const overlay = images.overlay ? imageLayer(images.overlay, "Detected change overlay", "cmp-overlay") : null;
  const ndvi = images.ndvi_change ? imageLayer(images.ndvi_change, "NDVI change", "cmp-ndvi") : null;

  const afterClip = h("div.cmp-after", after.img);
  const outline = polygonLayer(polygons, bounds);

  const handle = h("div.cmp-handle", {
    role: "slider",
    tabindex: 0,
    "aria-label": "Swipe between before and after images",
    "aria-valuemin": 0,
    "aria-valuemax": 100,
    "aria-valuenow": 50,
    "aria-valuetext": "50 percent after image",
    "aria-orientation": "horizontal",
  }, h("span.cmp-grip", { "aria-hidden": "true" },
    svgChevron("m6 3.5-3 4.5 3 4.5"),
    svgChevron("m10 3.5 3 4.5-3 4.5")));

  const beforeLabel = h("span.cmp-label.cmp-label-left", h("span.cmp-label-k", "Before"), beforeDate ? formatDate(beforeDate) : null);
  const afterLabel = h("span.cmp-label.cmp-label-right", h("span.cmp-label-k", "After"), afterDate ? formatDate(afterDate) : null);
  const skeleton = h("div.skeleton.cmp-skeleton", { "aria-hidden": "true" });

  const ratio = aspectFromBounds(bounds) || 4 / 3;
  const frame = h("div.cmp", { style: { "--ar": String(ratio) } },
    before.img,
    afterClip,
    overlay ? overlay.img : null,
    ndvi ? ndvi.img : null,
    outline,
    skeleton,
    beforeLabel,
    afterLabel,
    handle);

  function apply(next) {
    position = Math.max(0, Math.min(100, next));
    frame.style.setProperty("--pos", `${position}%`);
    handle.setAttribute("aria-valuenow", String(Math.round(position)));
    handle.setAttribute("aria-valuetext", `${Math.round(100 - position)} percent after image shown`);
  }
  apply(50);

  function fromPointer(event) {
    const rect = frame.getBoundingClientRect();
    if (!rect.width) return;
    apply(((event.clientX - rect.left) / rect.width) * 100);
  }

  frame.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    dragging = true;
    frame.setPointerCapture(event.pointerId);
    frame.classList.add("is-dragging");
    fromPointer(event);
    handle.focus({ preventScroll: true });
    event.preventDefault();
  });
  frame.addEventListener("pointermove", (event) => {
    if (dragging) fromPointer(event);
  });
  const endDrag = () => {
    dragging = false;
    frame.classList.remove("is-dragging");
  };
  frame.addEventListener("pointerup", endDrag);
  frame.addEventListener("pointercancel", endDrag);

  handle.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 10 : 2;
    const moves = {
      ArrowLeft: position - step,
      ArrowRight: position + step,
      ArrowDown: position - step,
      ArrowUp: position + step,
      Home: 0,
      End: 100,
    };
    if (event.key in moves) {
      apply(moves[event.key]);
      event.preventDefault();
    }
  });

  // Load once both base images are decoded so the two sides appear together.
  let pending = 2;
  const settled = () => {
    pending -= 1;
    if (pending <= 0) {
      skeleton.remove();
      frame.classList.add("is-ready");
    }
  };
  const failed = (layer) => {
    layer.img.remove();
    settled();
    frame.classList.add("has-missing");
  };
  for (const layer of [before, after]) {
    layer.img.addEventListener("load", () => {
      if (layer === before && layer.img.naturalWidth && layer.img.naturalHeight) {
        frame.style.setProperty("--ar", String(layer.img.naturalWidth / layer.img.naturalHeight));
      }
      settled();
    }, { once: true });
    layer.img.addEventListener("error", () => failed(layer), { once: true });
    layer.img.src = layer.src || "";
    if (!layer.src) failed(layer);
  }
  if (overlay) {
    overlay.img.addEventListener("error", () => overlay.img.remove(), { once: true });
    overlay.img.src = overlay.src;
  }
  if (ndvi) {
    ndvi.img.addEventListener("error", () => ndvi.img.remove(), { once: true });
    ndvi.img.src = ndvi.src;
  }

  function setLayer(name, visible) {
    frame.classList.toggle(`show-${name}`, visible);
  }
  setLayer("overlay", overlayDefault && Boolean(overlay));
  setLayer("outline", polygons.length > 0);

  return {
    element: frame,
    setLayer,
    has: { overlay: Boolean(overlay), ndvi: Boolean(ndvi), outline: polygons.length > 0 },
    setPosition: apply,
  };
}

function svgChevron(d) {
  return h("svg", { viewBox: "0 0 16 16", width: 14, height: 14, fill: "none", stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" }, h("path", { d }));
}

/** Draws finding polygons in image space using the georeferenced image bounds. */
function polygonLayer(polygons, bounds) {
  if (!polygons.length || !Array.isArray(bounds) || bounds.length !== 2) return null;
  const [[south, west], [north, east]] = bounds.map((pair) => pair.map(Number));
  if (![south, west, north, east].every(Number.isFinite) || north <= south || east <= west) return null;
  const toPoint = ([lon, lat]) => `${(((lon - west) / (east - west)) * 100).toFixed(3)},${(((north - lat) / (north - south)) * 100).toFixed(3)}`;
  const shapes = [];
  const addRing = (ring) => shapes.push(h("polygon", { points: ring.map(toPoint).join(" "), "vector-effect": "non-scaling-stroke" }));
  for (const geometry of polygons) {
    if (!geometry || !geometry.coordinates) continue;
    if (geometry.type === "Polygon") addRing(geometry.coordinates[0]);
    if (geometry.type === "MultiPolygon") geometry.coordinates.forEach((polygon) => addRing(polygon[0]));
  }
  if (!shapes.length) return null;
  return h("svg", { class: "cmp-outline", viewBox: "0 0 100 100", preserveAspectRatio: "none", "aria-hidden": "true" }, shapes);
}
