// Models and data: what the system runs on, how it scored, and where it falls short.

import { h, morph, reconcile } from "../dom.js";
import { api, asList } from "../api.js";
import { icon } from "../icons.js";
import {
  cleanText, formatCount, formatDate, formatDateTime, formatDuration, formatMetric, formatParameters, isNum, landcoverLabel, relativeAge, humanise,
} from "../format.js";
import { metricBar } from "../charts.js";
import { emptyState, errorState, imageBox, kv, skeletonRows } from "../ui.js";
import { on, store } from "../state.js";

const STAGES = [
  { key: "scene", title: "Sentinel-2 scene", detail: "New L2A scene from catalogue" },
  { key: "cloud", title: "Cloud mask", detail: "SCL drops cloud, shadow, cirrus" },
  { key: "change", title: "Change model", detail: "Siamese U-Net on six bands" },
  { key: "landcover", title: "Land cover", detail: "Eight classes, before and after" },
  { key: "seasonal", title: "Seasonal test", detail: "Same season, one year earlier" },
  { key: "auth", title: "Authorisation test", detail: "Checks the zone register" },
  { key: "verdict", title: "Verdict", detail: "Five outcomes, with reasons" },
  { key: "dispatch", title: "Dispatch", detail: "Routed to the department" },
];

/** Pipeline as real SVG: wrapped into two rows at any width, connected by arrows. */
function pipelineDiagram() {
  const columns = 4;
  const boxW = 208;
  const boxH = 64;
  const gapX = 36;
  const gapY = 40;
  const pad = 2;
  const width = columns * boxW + (columns - 1) * gapX + pad * 2;
  const height = 2 * boxH + gapY + pad * 2;
  const svg = h("svg", {
    class: "pipeline",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `Processing pipeline: ${STAGES.map((stage) => stage.title).join(", then ")}.`,
  },
  h("defs", h("marker", { id: "pipeline-arrow", viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
    h("path", { d: "M1 1 7 4 1 7", fill: "none", stroke: "currentColor", "stroke-width": 1.4, "stroke-linecap": "round", "stroke-linejoin": "round" }))));
  const position = (index) => {
    const row = index < columns ? 0 : 1;
    const col = row === 0 ? index : index - columns;
    return { x: pad + col * (boxW + gapX), y: pad + row * (boxH + gapY) };
  };
  STAGES.forEach((stage, index) => {
    const { x, y } = position(index);
    if (index < STAGES.length - 1) {
      const next = position(index + 1);
      const line = next.y === y
        ? h("line.pipeline-link", { x1: x + boxW, y1: y + boxH / 2, x2: next.x - 3, y2: y + boxH / 2, "marker-end": "url(#pipeline-arrow)" })
        : h("path.pipeline-link", {
          d: `M${x + boxW / 2} ${y + boxH} V${y + boxH + gapY / 2} H${next.x + boxW / 2} V${next.y - 3}`,
          fill: "none",
          "marker-end": "url(#pipeline-arrow)",
        });
      svg.appendChild(line);
    }
    svg.appendChild(h("g.pipeline-node",
      h("rect", { x, y, width: boxW, height: boxH, rx: 4 }),
      h("text.pipeline-step", { x: x + 12, y: y + 18 }, String(index + 1).padStart(2, "0")),
      h("text.pipeline-title", { x: x + 36, y: y + 19 }, stage.title),
      h("text.pipeline-detail", { x: x + 12, y: y + 44 }, stage.detail)));
  });
  return svg;
}

function statBlock(label, value) {
  if (!isNum(value)) return null;
  return h("div.metric", h("dt", label), h("dd", formatMetric(value)));
}

function externalLink(url, label) {
  if (!url || !/^https?:/i.test(url)) return label;
  return h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, label, icon("external", { size: 12 }));
}

/** The registry lists one row per split. Operators read one row per dataset. */
function datasetTable(datasets) {
  if (!datasets.length) return null;
  const grouped = new Map();
  for (const item of datasets) {
    const key = `${item.name}|${item.licence}`;
    if (!grouped.has(key)) grouped.set(key, { ...item, splits: [] });
    if (item.role) grouped.get(key).splits.push(`${humanise(item.role)} ${formatCount(item.samples)}`.trim());
  }
  return h("div.table-scroll", h("table.data-table.data-table-compact",
    h("thead", h("tr",
      h("th", { scope: "col" }, "Dataset"),
      h("th", { scope: "col" }, "Samples"),
      h("th", { scope: "col" }, "Licence"))),
    h("tbody", [...grouped.values()].map((item) => h("tr",
      h("td", externalLink(item.url, item.name)),
      h("td", item.splits.join(" · ")),
      h("td", item.licence || ""))))));
}

function perClassTable(perClass) {
  const rows = Object.entries(perClass || {});
  if (!rows.length) return null;
  return h("div",
    h("h4.section-label", "Per class"),
    h("div.table-scroll", h("table.data-table.data-table-compact",
      h("thead", h("tr",
        h("th", { scope: "col" }, "Class"),
        h("th", { scope: "col", class: "num" }, "F1"),
        h("th", { scope: "col", class: "metric-col" }, h("span.sr-only", "F1 bar")),
        h("th", { scope: "col", class: "num" }, "IoU"))),
      h("tbody", rows.map(([name, values]) => h("tr",
        h("td", landcoverLabel(name)),
        h("td.num", formatMetric(values && values.f1)),
        h("td.metric-col", isNum(values && values.f1) ? metricBar(values.f1) : null),
        h("td.num", formatMetric(values && values.iou))))))));
}

function datasetKeyLabel(name) {
  return humanise(name).replace(/\boscd\b/gi, "OSCD");
}

function perDatasetTable(perDataset) {
  const rows = Object.entries(perDataset || {});
  if (!rows.length) return null;
  const keys = [...new Set(rows.flatMap(([, values]) => Object.keys(values || {})))].filter((key) => ["f1", "iou", "precision", "recall", "accuracy"].includes(key));
  return h("div",
    h("h4.section-label", "Per dataset"),
    h("div.table-scroll", h("table.data-table.data-table-compact",
      h("thead", h("tr",
        h("th", { scope: "col" }, "Dataset"),
        keys.map((key) => h("th", { scope: "col", class: "num" }, key === "f1" ? "F1" : key === "iou" ? "IoU" : humanise(key))))),
      h("tbody", rows.map(([name, values]) => h("tr",
        h("td", datasetKeyLabel(name)),
        keys.map((key) => h("td.num", formatMetric(values && values[key])))))))));
}

function artifactFigure(label, url) {
  if (!url) return null;
  return h("figure.artifact",
    h("figcaption.section-label", label),
    h("a", { href: url, target: "_blank", rel: "noopener noreferrer", "aria-label": `Open ${label} at full size` },
      imageBox(url, { alt: label, ratio: "3 / 2", className: "artifact-image contain", fallback: "Figure not available" })));
}

const MODEL_NAMES = { change: "Change detector", landcover: "Land cover classifier" };

/**
 * `status` says whether the model can be used; `runtime_status` says what this process really has in
 * memory. A model that failed to load means scenes run on spectral rules, so it must not read Ready.
 */
function runtimeState(model, system) {
  const loaded = system && system.models_loaded ? system.models_loaded : {};
  const runtime = String(model.runtime_status || loaded[`${model.key}_runtime`] || "").toUpperCase();
  const status = String(model.status || "").toUpperCase();
  const reason = cleanText(model.unavailable_reason);
  if (runtime === "LOAD_FAILED" || status === "LOAD_FAILED") {
    return { key: "failed", tag: "Failed to load", detail: reason || "The model failed to load." };
  }
  if (runtime === "NO_CHECKPOINT" || status === "CHECKPOINT_MISSING") {
    return { key: "failed", tag: "Not installed", detail: reason || "The checkpoint is not on this machine." };
  }
  if (runtime === "LOADED") {
    return { key: "loaded", tag: "Loaded", detail: model.device ? `Loaded on ${model.device}` : "Loaded" };
  }
  if (status === "READY" && runtime === "NOT_LOADED") {
    return { key: "pending", tag: "Not loaded", detail: reason || "Checkpoint available; not loaded in this process yet." };
  }
  return { key: "pending", tag: humanise(runtime || status) || "Unknown", detail: reason || "Runtime state not available." };
}

function modelCard(model, system) {
  const metrics = model.metrics || {};
  const artifacts = model.artifacts || {};
  const datasets = asList(model.datasets);
  const limitations = asList(model.limitations);
  const figures = [
    artifactFigure("Learning curve", artifacts.learning_curve),
    artifactFigure("Confusion matrix", artifacts.confusion_matrix),
    artifactFigure("Sample predictions", artifacts.samples),
  ].filter(Boolean);
  const runtime = runtimeState(model, system);
  return h("article.panel.model-card", { "aria-label": model.name },
    h("header.panel-head",
      h("h2.panel-title", model.name || MODEL_NAMES[model.key] || humanise(model.key)),
      model.version ? h("span.panel-sub.mono", model.version) : null,
      h("span.tag.tag-status", { dataset: { status: runtime.key === "loaded" ? "ready" : "unavailable" } }, runtime.tag)),
    h("div.model-body",
      h("div.model-metrics",
        metrics.split ? h("p.section-label", `${humanise(metrics.split)} set`) : null,
        h("dl.metric-row",
          statBlock("F1", metrics.f1),
          statBlock("IoU", metrics.iou),
          statBlock("Precision", metrics.precision),
          statBlock("Recall", metrics.recall),
          statBlock("Accuracy", metrics.accuracy))),
      h("dl.kv-list.model-facts",
        kv("Architecture", model.architecture),
        kv("Parameters", formatParameters(model.parameters)),
        kv("Input bands", asList(model.input_bands).join(", ")),
        kv("Decision threshold", isNum(model.threshold) ? String(model.threshold) : null),
        kv("Trained", formatDate(model.trained_at)),
        kv("Epochs", isNum(model.epochs) && Number(model.epochs) > 0 ? model.epochs : null),
        kv("Training time", isNum(model.training_seconds) && Number(model.training_seconds) > 0 ? formatDuration(model.training_seconds) : null),
        kv("Runtime", runtime.detail),
        kv("Registry status", humanise(model.registry_status)),
        kv("Checkpoint hash", model.checkpoint_sha256 ? String(model.checkpoint_sha256).slice(0, 16) : null, { mono: true })),
      datasets.length ? h("div", h("h4.section-label", "Datasets"), datasetTable(datasets)) : null,
      perClassTable(metrics.per_class),
      perDatasetTable(metrics.per_dataset),
      figures.length ? h("div.artifact-grid", figures) : null,
      limitations.length
        ? h("div", h("h4.section-label", "Limitations"), h("ul.plain-list", limitations.map((text) => h("li", text))))
        : null));
}

const PROVIDER_NAMES = {
  "earth-search": { name: "Earth Search (Element 84)", url: "https://element84.com/earth-search/" },
  "planetary-computer": { name: "Microsoft Planetary Computer", url: "https://planetarycomputer.microsoft.com/" },
  "copernicus-data-space": { name: "Copernicus Data Space", url: "https://dataspace.copernicus.eu/" },
};

function providerRow(provider) {
  const known = PROVIDER_NAMES[provider.name] || { name: humanise(provider.name) };
  const used = Boolean(provider.last_success_at);
  let health = "CLOUDED";
  let state = "Ready";
  if (!provider.healthy) {
    health = "ERROR";
    state = "Not responding, next source in use";
  } else if (used) {
    health = "OK";
    state = `Connected, last response ${relativeAge(provider.last_success_at)}`;
  }
  return h("li.source-row",
    h("span.dot", { dataset: { health } }),
    h("span.source-name", externalLink(known.url, known.name)),
    h("span.source-state", state),
    provider.last_scene_at ? h("span.source-detail", `Newest scene ${formatDate(provider.last_scene_at)}`) : null);
}

function dataSources(system, dataStatus) {
  const providers = asList(system && system.providers);
  const rows = providers.map(providerRow);
  const smtp = system && system.smtp;
  if (smtp) {
    rows.push(h("li.source-row",
      h("span.dot", { dataset: { health: smtp.configured ? "OK" : "CLOUDED" } }),
      h("span.source-name", "Department email"),
      h("span.source-state", smtp.configured ? "SMTP connected" : "Saving to outbox (SMTP not configured)")));
  }
  const facts = dataStatus ? [
    kv("Scenes on record", formatCount(dataStatus.observations)),
    kv("Analyses", formatCount(dataStatus.analyses)),
    kv("Newest scene", formatDateTime(dataStatus.newest_actual_acquisition_at)),
    kv("Last catalogue query", formatDateTime(dataStatus.latest_catalogue_query_at)),
  ].filter(Boolean) : [];
  return h("section.panel", { "aria-label": "Data sources" },
    h("header.panel-head", h("h2.panel-title", "Data sources")),
    rows.length ? h("ul.source-list", rows) : h("p.panel-empty", "Source status not available."),
    facts.length ? h("dl.kv-list", facts) : null);
}

export async function mountModels(root) {
  let disposed = false;
  let runtimeSignature = JSON.stringify(store.overview?.models);
  const pipeline = h("section.panel", { "aria-label": "How it works" },
    h("header.panel-head", h("h2.panel-title", "How it works")),
    h("div.pipeline-wrap", pipelineDiagram()));
  const modelHost = h("div.model-list", skeletonRows(6));
  const sourceHost = h("div", skeletonRows(3));

  root.replaceChildren(h("div.page.page-wide",
    h("header.page-head", h("div.page-head-main", h("h1.page-title", "Models and data"))),
    pipeline,
    h("div.models-grid", modelHost, sourceHost)));

  async function load() {
    const [models, system, dataStatus] = await Promise.allSettled([api.models(), api.system(), api.dataStatus()]);
    if (disposed) return;
    if (models.status === "fulfilled") {
      const list = asList(models.value, "models");
      const systemValue = system.status === "fulfilled" ? system.value : null;
      if (list.length) {
        if (!modelHost._keyed) modelHost.replaceChildren();
        reconcile(modelHost, list, { key: (model) => model.key, render: (model) => modelCard(model, systemValue) });
      } else modelHost.replaceChildren(emptyState("No models registered", "Model details appear here once the registry is present."));
    } else {
      modelHost.replaceChildren(errorState(models.reason, load, "Could not load model details"));
    }
    const next = dataSources(
      system.status === "fulfilled" ? system.value : null,
      dataStatus.status === "fulfilled" ? dataStatus.value : null,
    );
    if (sourceHost.firstElementChild?.classList.contains("panel")) morph(sourceHost.firstElementChild, next);
    else sourceHost.replaceChildren(next);
  }
  load();
  const off = on("overview", () => {
    const next = JSON.stringify(store.overview?.models);
    if (next === runtimeSignature) return;
    runtimeSignature = next;
    load();
  });

  return () => {
    disposed = true;
    off();
  };
}
