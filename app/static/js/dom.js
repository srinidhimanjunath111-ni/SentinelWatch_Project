// Minimal safe DOM toolkit. All text goes through text nodes, never innerHTML, so
// server strings cannot inject markup.

const SVG_NS = "http://www.w3.org/2000/svg";
const SVG_TAGS = new Set([
  "svg", "g", "path", "rect", "circle", "line", "polyline", "polygon", "text", "title",
  "defs", "marker", "tspan", "clipPath", "desc",
]);

function apply(el, attrs, isSvg) {
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") {
      if (isSvg) el.setAttribute("class", value);
      else el.className = value;
    } else if (key === "style" && typeof value === "object") {
      for (const [prop, v] of Object.entries(value)) {
        if (v === null || v === undefined) continue;
        if (prop.startsWith("--")) el.style.setProperty(prop, v);
        else el.style[prop] = v;
      }
    } else if (key === "dataset") {
      for (const [name, v] of Object.entries(value)) {
        if (v !== null && v !== undefined) el.dataset[name] = v;
      }
    } else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "value" && !isSvg) {
      el.value = value;
    } else if (value === true) {
      el.setAttribute(key, "");
    } else {
      el.setAttribute(key, String(value));
    }
  }
}

function append(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) append(el, child);
    else if (child instanceof Node) el.appendChild(child);
    else el.appendChild(document.createTextNode(String(child)));
  }
}

function isAttrs(value) {
  return value && typeof value === "object" && !Array.isArray(value) && !(value instanceof Node);
}

/** Create an element. `h("a.link", {href}, "text")`; class shorthand after the tag name. */
export function h(spec, attrs, ...children) {
  const [tag, ...classes] = spec.split(".");
  const isSvg = SVG_TAGS.has(tag);
  const el = isSvg ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  if (classes.length) el.setAttribute("class", classes.join(" "));
  if (isAttrs(attrs)) {
    if (attrs.class && classes.length) {
      const { class: extra, ...rest } = attrs;
      el.setAttribute("class", `${classes.join(" ")} ${extra}`);
      apply(el, rest, isSvg);
    } else {
      apply(el, attrs, isSvg);
    }
  } else {
    children.unshift(attrs);
  }
  append(el, children);
  return el;
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

export function setChildren(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

/** Only write text when it changed, so repeated polls cause no DOM mutation at all. */
export function setText(el, value) {
  const text = value === null || value === undefined ? "" : String(value);
  if (el && el.textContent !== text) el.textContent = text;
}

/**
 * Bring `from` into line with `to` in place. Listeners stay on the existing nodes, which is why
 * row handlers must look up current state by id rather than close over a stale object.
 * An element with data-morph-key equal on both sides is treated as unchanged: this is how loaded
 * images and interactive sub-widgets survive a refresh without flicker.
 */
export function morph(from, to) {
  if (from.nodeType !== to.nodeType || from.nodeName !== to.nodeName) {
    from.replaceWith(to);
    return to;
  }
  if (from.nodeType === Node.TEXT_NODE) {
    if (from.nodeValue !== to.nodeValue) from.nodeValue = to.nodeValue;
    return from;
  }
  if (from.nodeType !== Node.ELEMENT_NODE) return from;
  const fromKey = from.getAttribute("data-morph-key");
  const toKey = to.getAttribute("data-morph-key");
  if (fromKey !== null || toKey !== null) {
    if (fromKey === toKey) return from;
    from.replaceWith(to);
    return to;
  }
  for (const attr of [...from.attributes]) {
    if (!to.hasAttribute(attr.name)) from.removeAttribute(attr.name);
  }
  for (const attr of [...to.attributes]) {
    if (from.getAttribute(attr.name) !== attr.value) from.setAttribute(attr.name, attr.value);
  }
  const oldKids = [...from.childNodes];
  const newKids = [...to.childNodes];
  const shared = Math.min(oldKids.length, newKids.length);
  for (let i = 0; i < shared; i += 1) morph(oldKids[i], newKids[i]);
  for (let i = shared; i < oldKids.length; i += 1) oldKids[i].remove();
  for (let i = shared; i < newKids.length; i += 1) from.appendChild(newKids[i]);
  return from;
}

/**
 * Keyed list rendering. Nodes are kept, reordered with the fewest moves and morphed only when the
 * item signature changed. Returns the set of newly created keys.
 */
export function reconcile(parent, items, { key, render, signature = JSON.stringify }) {
  const known = parent._keyed || new Map();
  const nextKeys = new Set();
  const created = new Set();
  let cursor = parent.firstElementChild;
  for (const item of items) {
    const id = key(item);
    nextKeys.add(id);
    const sig = signature(item);
    let entry = known.get(id);
    if (!entry) {
      entry = { node: render(item), sig };
      known.set(id, entry);
      created.add(id);
    } else if (entry.sig !== sig) {
      entry.node = morph(entry.node, render(item));
      entry.sig = sig;
    }
    if (entry.node === cursor) {
      cursor = cursor.nextElementSibling;
    } else {
      parent.insertBefore(entry.node, cursor);
    }
  }
  for (const [id, entry] of known) {
    if (!nextKeys.has(id)) {
      entry.node.remove();
      known.delete(id);
    }
  }
  parent._keyed = known;
  return created;
}

/** Coalesce rapid calls into one trailing call. */
export function debounce(fn, wait = 150) {
  let timer = null;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

export function isNearBottom(el, tolerance = 4) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= tolerance;
}
