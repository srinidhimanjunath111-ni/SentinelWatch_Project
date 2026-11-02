// Shared UI parts: tags, image boxes, modal, toasts, alert banner, empty and error states.

import { h, setChildren, setText } from "./dom.js";
import { icon, severityGlyph } from "./icons.js";
import {
  alertGroupLabel, errorText, formatDateTime, parseDate, persistenceLabel, relativeAge, severityLabel, statusLabel,
} from "./format.js";

export function severityTag(severity) {
  return h("span.tag.tag-sev", { dataset: { severity: String(severity || "none").toLowerCase() } },
    severityGlyph(severity), severityLabel(severity) || "Info");
}

export function statusTag(status) {
  if (!status) return null;
  return h("span.tag.tag-status", { dataset: { status } }, statusLabel(status));
}

export function replayBadge(record) {
  if (!record || record.origin !== "archive_replay") return null;
  return h("span.tag.tag-replay", { title: "Result from past imagery, not a live detection" }, icon("history", { size: 12 }), "Archive replay");
}

export function illustrativeTag() {
  return h("span.tag.tag-illustrative", { title: "Example record, not an issued permit" }, "Illustrative");
}

/** <time> whose text the global ticker keeps fresh. Hover shows the exact IST time. */
export function relTime(value, className = "") {
  if (!value || !parseDate(value)) return null;
  return h("time", {
    class: `rel ${className}`.trim(),
    datetime: String(value),
    title: formatDateTime(value),
    dataset: { rel: String(value) },
  }, relativeAge(value));
}

const ticking = new Set();
export function refreshRelativeTimes(root = document) {
  for (const el of root.querySelectorAll("[data-rel]")) setText(el, relativeAge(el.dataset.rel));
  for (const fn of ticking) fn();
}
export function onTick(fn) {
  ticking.add(fn);
  return () => ticking.delete(fn);
}

/**
 * Fixed-aspect image box with a skeleton: nothing shifts when the image arrives, and a failed image
 * shows a quiet placeholder rather than a broken-image glyph. Morphing skips it while src is same.
 */
export function imageBox(src, { alt = "", ratio = "4 / 3", className = "", fallback = "Image not available", eager = false } = {}) {
  const box = h("span", {
    class: `imgbox ${className}`.trim(),
    style: { aspectRatio: ratio },
    dataset: { morphKey: src || "none" },
  });
  if (!src) {
    box.classList.add("is-empty");
    if (fallback) box.appendChild(h("span.imgbox-note", fallback));
    return box;
  }
  const img = h("img", { alt, decoding: "async", loading: eager ? "eager" : "lazy" });
  img.addEventListener("load", () => box.classList.add("is-loaded"), { once: true });
  img.addEventListener("error", () => {
    img.remove();
    box.classList.add("is-empty");
    if (fallback) box.appendChild(h("span.imgbox-note", fallback));
  }, { once: true });
  img.src = src;
  box.appendChild(img);
  return box;
}

export function skeletonRows(count = 4, className = "") {
  return h("div", { class: `skeleton-rows ${className}`.trim(), "aria-hidden": "true" },
    Array.from({ length: count }, () => h("div.skeleton.skeleton-row")));
}

export function emptyState(title, detail, action = null) {
  return h("div.empty", h("div.empty-title", title), detail ? h("p.empty-detail", detail) : null, action);
}

export function errorState(error, retry, fallback) {
  return h("div.empty.empty-error", { role: "alert" },
    h("div.empty-title", icon("alert"), fallback || "Could not load this"),
    h("p.empty-detail", errorText(error)),
    retry ? h("button.btn", { type: "button", onclick: retry }, icon("refresh"), "Try again") : null);
}

// ---------------------------------------------------------------- modal

const modalRoot = () => document.getElementById("modal-host");

/**
 * Promise-based dialog. `build(close)` returns body nodes and may submit by calling close(value).
 * Uses <dialog> for the focus trap, Escape and inert background.
 */
export function openModal({ title, build, width = 460 }) {
  return new Promise((resolve) => {
    const dialog = h("dialog.modal", { style: { width: `min(${width}px, calc(100vw - 32px))` }, "aria-label": title });
    let settled = false;
    const close = (value) => {
      if (settled) return;
      settled = true;
      dialog.close();
      dialog.remove();
      resolve(value);
    };
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      close(undefined);
    });
    dialog.addEventListener("mousedown", (event) => {
      if (event.target === dialog) close(undefined);
    });
    setChildren(dialog,
      h("header.modal-head",
        h("h2.modal-title", title),
        h("button.icon-btn", { type: "button", "aria-label": "Close", onclick: () => close(undefined) }, icon("x"))),
      h("div.modal-body", build(close)));
    modalRoot().appendChild(dialog);
    dialog.showModal();
  });
}

export function confirmModal({ title, message, confirmLabel = "Confirm", danger = false }) {
  return openModal({
    title,
    build: (close) => [
      h("p.modal-text", message),
      h("div.modal-actions",
        h("button.btn", { type: "button", onclick: () => close(false) }, "Cancel"),
        h("button.btn", { type: "button", class: danger ? "btn-danger" : "btn-primary", onclick: () => close(true) }, confirmLabel)),
    ],
  }).then(Boolean);
}

/** Note + officer name dialog for lifecycle actions. Resolves {note, actor} or undefined. */
export function noteModal({ title, message, confirmLabel, placeholder = "", required = false, danger = false, submit }) {
  return openModal({
    title,
    build: (close) => {
      const remembered = (() => {
        try {
          return localStorage.getItem("sentinelwatch.actor") || "";
        } catch {
          return "";
        }
      })();
      const note = h("textarea.input", { id: "modal-note", rows: 4, maxlength: 2000, placeholder });
      const actor = h("input.input", { id: "modal-actor", type: "text", maxlength: 120, autocomplete: "name", value: remembered });
      const error = h("p.form-error", { role: "alert", hidden: true });
      const confirm = h("button.btn", { type: "submit", class: danger ? "btn-danger" : "btn-primary" }, confirmLabel);
      const form = h("form.form", {
        onsubmit: async (event) => {
          event.preventDefault();
          const noteText = note.value.trim();
          if (required && !noteText) {
            error.hidden = false;
            error.textContent = "Add a short note for the record.";
            note.focus();
            return;
          }
          const value = { note: noteText || null, actor: actor.value.trim() || null };
          try {
            localStorage.setItem("sentinelwatch.actor", actor.value.trim());
          } catch {
            // Convenience only.
          }
          if (!submit) {
            close(value);
            return;
          }
          confirm.disabled = true;
          error.hidden = true;
          try {
            await submit(value);
            close(value);
          } catch (failure) {
            error.hidden = false;
            error.textContent = errorText(failure, "That action could not be saved. Try again.");
            confirm.disabled = false;
          }
        },
      },
      message ? h("p.modal-text", message) : null,
      h("label.field", { for: "modal-note" }, h("span.field-label", required ? "Note" : "Note (optional)"), note),
      h("label.field", { for: "modal-actor" }, h("span.field-label", "Officer"), actor),
      error,
      h("div.modal-actions",
        h("button.btn", { type: "button", onclick: () => close(undefined) }, "Cancel"),
        confirm));
      return form;
    },
  });
}

// ---------------------------------------------------------------- toast

export function toast(message, kind = "info") {
  const host = document.getElementById("toast-host");
  const node = h("div.toast", { dataset: { kind }, role: kind === "error" ? "alert" : "status" },
    icon(kind === "error" ? "alert" : kind === "ok" ? "check" : "info"), h("span", message));
  host.appendChild(node);
  while (host.children.length > 3) host.firstElementChild.remove();
  setTimeout(() => {
    node.classList.add("is-leaving");
    setTimeout(() => node.remove(), 220);
  }, kind === "error" ? 6000 : 3600);
}

// ---------------------------------------------------------------- alert banner

let bannerTimer = null;
let bannerCount = 0;

function closeBanner() {
  const host = document.getElementById("banner-host");
  clearTimeout(bannerTimer);
  bannerCount = 0;
  const node = host.firstElementChild;
  if (!node) return;
  node.classList.remove("is-open");
  setTimeout(() => {
    if (!node.classList.contains("is-open")) node.remove();
  }, 260);
}

export function showAlertBanner(alerts, placeOf) {
  alerts = alerts.filter((alert) => alert.event_kind !== "updated");
  if (!alerts.length) return;
  const host = document.getElementById("banner-host");
  const ranked = [...alerts].sort((a, b) => rank(b.severity) - rank(a.severity));
  const lead = ranked[0];
  bannerCount += alerts.length;
  const extra = bannerCount - 1;
  const href = `#/case/${encodeURIComponent(lead.case_id || "")}`;
  const content = [
    h("a.banner-main", { href: lead.case_id ? href : "#/alerts", onclick: () => closeBanner() },
      severityGlyph(lead.severity, 12),
      h("span.banner-sev", severityLabel(lead.severity) || "Alert"),
      h("span.banner-text", [alertGroupLabel(lead), persistenceLabel(lead)].filter(Boolean).join(" · ")),
      h("span.banner-place", placeOf(lead)),
      lead.origin === "archive_replay" ? h("span.tag.tag-replay", icon("history", { size: 12 }), "Archive replay") : null,
      extra > 0 ? h("span.banner-more", `+${extra} more`) : null),
    h("button.icon-btn", { type: "button", "aria-label": "Dismiss alert banner", onclick: closeBanner }, icon("x")),
  ];
  let node = host.firstElementChild;
  if (!node) {
    node = h("div.banner", { role: "alert" });
    host.appendChild(node);
    node.addEventListener("mouseenter", () => clearTimeout(bannerTimer));
    node.addEventListener("mouseleave", armBanner);
  }
  node.dataset.severity = String(lead.severity || "none").toLowerCase();
  setChildren(node, content);
  requestAnimationFrame(() => node.classList.add("is-open"));
  armBanner();
}

function armBanner() {
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(closeBanner, 14000);
}

function rank(severity) {
  return { critical: 4, high: 3, medium: 2, low: 1 }[String(severity || "").toLowerCase()] || 0;
}

// ---------------------------------------------------------------- bits

export function kv(label, value, { mono = false, node = null } = {}) {
  if (!node && (value === null || value === undefined || value === "")) return null;
  return h("div.kv", h("dt", label), h("dd", { class: mono ? "mono" : null }, node || String(value)));
}

export function progressBar(percent, { label = "Progress" } = {}) {
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  return h("div.progress", { role: "progressbar", "aria-label": label, "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(value) },
    h("span.progress-fill", { style: { width: `${value}%` } }));
}

export function pageHeader({ crumbs = [], title, meta = null, actions = null }) {
  return h("header.page-head",
    h("div.page-head-main",
      crumbs.length ? h("nav.crumbs", { "aria-label": "Breadcrumb" }, crumbs.map((crumb) => h("a", { href: crumb.href }, icon("chevronLeft", { size: 12 }), crumb.label))) : null,
      h("h1.page-title", title),
      meta ? h("div.page-meta", meta) : null),
    actions ? h("div.page-actions", actions) : null);
}
