// The single home for every human label and every number or date format.

const STATUS_LABELS = {
  open: "Open",
  dispatched: "Dispatched",
  acknowledged: "Acknowledged",
  field_verification: "Field verification",
  resolved: "Resolved",
  false_positive: "False positive",
};

const SEVERITY_LABELS = { critical: "Critical", high: "High", medium: "Medium", low: "Low", none: "Info" };

const CATEGORY_LABELS = {
  DEFORESTATION: "Deforestation",
  MINING: "Mining",
  CONSTRUCTION: "Construction",
  URBANISATION: "Urbanisation",
  FLOOD: "Flood",
  LANDSLIDE: "Landslide",
  BURN_SCAR: "Burn scar",
  SEASONAL_VEGETATION: "Seasonal vegetation",
  CROP_CYCLE: "Crop cycle",
  WATER_CHANGE: "Water change",
  OTHER_CHANGE: "Other change",
  VEGETATION_LOSS: "Vegetation loss",
  MINING_LAND_DISTURBANCE: "Mining disturbance",
};

const VERDICT_LABELS = {
  SUSPECTED_UNAUTHORISED: "Suspected unauthorised",
  AUTHORISED_ACTIVITY: "Authorised activity",
  NATURAL_EVENT: "Natural event",
  SEASONAL_BENIGN: "Seasonal, no action",
  NEEDS_REVIEW: "Needs review",
};

const LANDCOVER_LABELS = {
  tree: "Tree cover",
  shrub_grass: "Shrub and grass",
  crop: "Cropland",
  built: "Built-up",
  bare: "Bare ground",
  water: "Water",
  wetland: "Wetland",
  mine: "Mine",
};

const HEALTH_LABELS = {
  OK: "Normal",
  CLOUDED: "Clouded",
  ALERT: "In alert",
  ERROR: "Check failed",
  PAUSED: "Paused",
};

const RUN_LABELS = {
  QUEUED: "Queued",
  RUNNING: "Running",
  STARTED: "Started",
  SEARCHING_CATALOGUE: "Searching catalogue",
  CHECKING_CLOUD: "Checking cloud cover",
  DETECTING_CHANGE: "Detecting change",
  GATHERING_CONTEXT: "Gathering context",
  WRITING_EVIDENCE: "Writing evidence",
  RECORDING_ACQUISITIONS: "Recording scenes",
  SELECTING_PAIR: "Selecting scene pair",
  RETRIEVING_IMAGERY: "Retrieving imagery",
  PREPROCESSING: "Preprocessing",
  CLOUD_MASK: "Masking cloud",
  CHANGE_MODEL: "Running change model",
  LAND_COVER: "Classifying land cover",
  LANDCOVER: "Classifying land cover",
  RUNNING_SIAMESE_MODEL: "Running change model",
  RUNNING_BASELINE: "Running change analysis",
  EXTRACTING_REGIONS: "Extracting changed regions",
  SEASONAL_TEST: "Same-season comparison",
  AUTHORISATION_TEST: "Authorisation check",
  CONTEXT: "Gathering context",
  CLASSIFYING: "Classifying change",
  CREATING_CASE_AND_ALERTS: "Creating incident",
  DISPATCHING: "Dispatching",
  COMPLETED: "Completed",
  COMPLETED_NO_SIGNIFICANT_CHANGE: "Completed, no significant change",
  NO_NEW_ACQUISITION: "No new scene",
  NO_SUITABLE_ACQUISITION: "No usable scene",
  SKIPPED_PAUSED: "Skipped, site paused",
  BLOCKED_DEPENDENCY: "Could not run",
  BLOCKED_CREDENTIALS: "Could not reach imagery provider",
  MODEL_UNAVAILABLE: "Model unavailable",
  UNSUITABLE_IMAGERY: "Imagery unsuitable",
  FAILED: "Failed",
};

const FINAL_RUN_STATES = new Set([
  "COMPLETED", "COMPLETED_NO_SIGNIFICANT_CHANGE", "NO_NEW_ACQUISITION", "NO_SUITABLE_ACQUISITION",
  "SKIPPED_PAUSED", "BLOCKED_CREDENTIALS", "BLOCKED_DEPENDENCY", "UNSUITABLE_IMAGERY", "FAILED",
  "MODEL_UNAVAILABLE", "CANCELLED",
]);

const SITE_TYPE_LABELS = {
  FOREST: "Forest",
  FOREST_RESERVE: "Forest reserve",
  MINING: "Mining belt",
  MINING_BELT: "Mining belt",
  URBAN: "Urban fringe",
  URBAN_FRINGE: "Urban fringe",
  FLOODPLAIN: "Floodplain",
  RIVER: "River basin",
  HILL_SLOPE: "Hill slope",
  LANDSLIDE: "Hill slope",
  PROTECTED_AREA: "Protected area",
  COASTAL: "Coastal",
  AGRICULTURE: "Agriculture",
  OTHER: "General",
};

const ZONE_TYPE_LABELS = { MINING: "Mining lease", CONSTRUCTION: "Construction permit", FORESTRY: "Forestry clearance", OTHER: "Other" };

const SCENE_STATUS_LABELS = {
  analysed: "Analysed",
  cloudy: "Cloudy",
  alert: "Change found",
  clear: "Clear",
  catalogued: "Catalogued",
};

/** Timeline statuses from the API (DISCOVERED, CLEAR, CLOUDED, ANALYSED, CHANGE) to display groups. */
export function sceneStatusKey(value) {
  const raw = String(value || "").toUpperCase();
  if (raw === "CHANGE" || raw === "ALERT") return "alert";
  if (raw === "ANALYSED" || raw === "ANALYZED") return "analysed";
  if (raw === "CLOUDED" || raw === "CLOUDY") return "cloudy";
  if (raw === "CLEAR") return "clear";
  return "catalogued";
}

const DISPATCH_STATUS_LABELS = {
  SENT: "Sent",
  RECORDED: "Delivered to inbox",
  OUTBOX_SAVED: "Saved to outbox (SMTP not configured)",
  QUEUED: "Queued",
  PENDING: "Pending",
  FAILED: "Delivery failed",
};

/** The dispatch state as operators read it. A failure also carries the reason from the server. */
export function dispatchLine(row) {
  if (!row) return "";
  const label = dispatchStatusLabel(row.status);
  if (String(row.status).toUpperCase() === "FAILED" && row.detail && row.detail.length < 160) return `${label}: ${row.detail}`;
  return label;
}

const CHANNEL_LABELS = { email: "Email", webhook: "Webhook", sms: "SMS", in_app: "Department inbox" };

const SENSITIVITY_LABELS = { LOW: "Low", MEDIUM: "Medium", HIGH: "High" };

/** Fallback for a value the map has not heard of: readable, never a raw enum. */
export function humanise(value) {
  if (value === null || value === undefined || value === "") return "";
  const text = String(value).replace(/[_-]+/g, " ").trim().toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function lookup(map, value) {
  if (value === null || value === undefined || value === "") return "";
  const raw = String(value);
  return map[raw] ?? map[raw.toUpperCase()] ?? map[raw.toLowerCase()] ?? humanise(raw);
}

export const statusLabel = (v) => lookup(STATUS_LABELS, v);
export const severityLabel = (v) => lookup(SEVERITY_LABELS, v);
export const categoryLabel = (v) => lookup(CATEGORY_LABELS, v);
export const verdictLabel = (v) => lookup(VERDICT_LABELS, v);
export const landcoverLabel = (v) => lookup(LANDCOVER_LABELS, v);
export const healthLabel = (v) => lookup(HEALTH_LABELS, v);
export const runLabel = (v) => lookup(RUN_LABELS, v);
export const siteTypeLabel = (v) => lookup(SITE_TYPE_LABELS, v);
export const zoneTypeLabel = (v) => lookup(ZONE_TYPE_LABELS, v);
export const sceneStatusLabel = (v) => SCENE_STATUS_LABELS[sceneStatusKey(v)];
export const dispatchStatusLabel = (v) => lookup(DISPATCH_STATUS_LABELS, v);
export const channelLabel = (v) => lookup(CHANNEL_LABELS, v);
export const sensitivityLabel = (v) => lookup(SENSITIVITY_LABELS, v);

export const ALERT_STATUSES = Object.keys(STATUS_LABELS);
export const SEVERITIES = ["critical", "high", "medium", "low"];
export const ALERT_CATEGORIES = [
  "DEFORESTATION", "MINING", "CONSTRUCTION", "URBANISATION", "FLOOD", "LANDSLIDE", "BURN_SCAR",
  "WATER_CHANGE", "OTHER_CHANGE",
];
export const MONITOR_CATEGORIES = [
  "DEFORESTATION", "MINING", "CONSTRUCTION", "URBANISATION", "FLOOD", "LANDSLIDE", "BURN_SCAR",
  "WATER_CHANGE",
];
export const SITE_TYPES = ["FOREST", "MINING_BELT", "URBAN_FRINGE", "FLOODPLAIN", "HILL_SLOPE", "PROTECTED_AREA", "COASTAL", "AGRICULTURE", "OTHER"];
export const ZONE_TYPES = Object.keys(ZONE_TYPE_LABELS);
export const INTERVALS = [
  { minutes: 360, label: "Every 6 h" },
  { minutes: 720, label: "Every 12 h" },
  { minutes: 1440, label: "Every 24 h" },
  { minutes: 10080, label: "Weekly" },
];

export function intervalLabel(minutes) {
  const value = Number(minutes);
  if (!Number.isFinite(value) || value <= 0) return "";
  const known = INTERVALS.find((item) => item.minutes === value);
  if (known) return known.label;
  if (value % 1440 === 0) return `Every ${value / 1440} d`;
  if (value % 60 === 0) return `Every ${value / 60} h`;
  return `Every ${value} min`;
}

/** A headline an operator would actually say, from verdict and category together. */
export function verdictHeadline(verdict, category) {
  const cat = categoryLabel(category);
  const lower = cat.toLowerCase();
  switch (verdict) {
    case "SUSPECTED_UNAUTHORISED":
      return cat ? `Suspected unauthorised ${lower}` : "Suspected unauthorised change";
    case "AUTHORISED_ACTIVITY":
      return cat ? `${cat} inside an authorised zone` : "Change inside an authorised zone";
    case "NATURAL_EVENT":
      return cat ? `Natural event: ${lower}` : "Natural event";
    case "SEASONAL_BENIGN":
      return cat ? `Seasonal change: ${lower}` : "Seasonal change, no action needed";
    case "NEEDS_REVIEW":
      return cat ? `${cat} detected, cause undetermined` : "Change detected, cause undetermined";
    default:
      return cat || "Change detected";
  }
}

export function isFinalRun(status) {
  return FINAL_RUN_STATES.has(String(status || "").toUpperCase());
}

export function isFailedRun(status) {
  return ["FAILED", "BLOCKED_DEPENDENCY", "BLOCKED_CREDENTIALS", "MODEL_UNAVAILABLE"].includes(String(status || "").toUpperCase());
}

// ---------------------------------------------------------------- dates

const TZ = "Asia/Kolkata";
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const dateFmt = new Intl.DateTimeFormat("en-IN", { timeZone: TZ, day: "numeric", month: "short", year: "numeric" });
const shortFmt = new Intl.DateTimeFormat("en-IN", { timeZone: TZ, day: "numeric", month: "short" });
const timeFmt = new Intl.DateTimeFormat("en-IN", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const clockFmt = new Intl.DateTimeFormat("en-IN", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });

/** Datetimes are UTC; a bare calendar date stays on that date in any zone. */
export function parseDate(value) {
  if (!value) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  const text = String(value);
  let iso = text;
  if (DATE_ONLY.test(text)) iso = `${text}T12:00:00+05:30`;
  else if (/^\d{4}-\d{2}-\d{2}[T ]\d/.test(text) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(text)) iso = `${text.replace(" ", "T")}Z`;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDate(value) {
  const date = parseDate(value);
  return date ? dateFmt.format(date) : "";
}

export function formatShortDate(value) {
  const date = parseDate(value);
  return date ? shortFmt.format(date) : "";
}

export function formatDateTime(value) {
  const date = parseDate(value);
  return date ? `${dateFmt.format(date)}, ${timeFmt.format(date)} IST` : "";
}

export function formatClock(value) {
  const date = parseDate(value);
  return date ? clockFmt.format(date) : "";
}

export function relativeAge(value, now = Date.now()) {
  const date = parseDate(value);
  if (!date) return "";
  const seconds = Math.round((now - date.getTime()) / 1000);
  if (seconds < 0) return `in ${formatDuration(-seconds)}`;
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  if (seconds < 86400 * 60) return `${Math.round(seconds / 86400)} d ago`;
  return `${Math.round(seconds / (86400 * 30))} mo ago`;
}

export function formatDuration(totalSeconds) {
  const seconds = Math.max(0, Math.round(Number(totalSeconds) || 0));
  if (seconds < 60) return `${seconds}s`;
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return hours ? `${days}d ${hours}h` : `${days}d`;
  if (hours > 0) return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
  return `${minutes}m`;
}

export function formatLatency(seconds) {
  const value = Number(seconds);
  if (seconds === null || seconds === undefined || !Number.isFinite(value) || value < 0) return "";
  if (value < 1) return `${Math.round(value * 1000)} ms`;
  if (value < 60) return `${value.toFixed(1)} s`;
  return formatDuration(value);
}

// ---------------------------------------------------------------- numbers

export function isNum(value) {
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
}

export function formatCount(value) {
  if (!isNum(value)) return "";
  return Number(value).toLocaleString("en-IN");
}

export function formatArea(m2) {
  if (!isNum(m2)) return "";
  const value = Number(m2);
  if (value >= 1_000_000) {
    const km = value / 1_000_000;
    return `${km >= 100 ? Math.round(km).toLocaleString("en-IN") : km.toFixed(km >= 10 ? 1 : 2)} km²`;
  }
  const ha = value / 10_000;
  if (ha === 0) return "0 ha";
  return `${ha >= 10 ? ha.toFixed(1) : ha.toFixed(2)} ha`;
}

/** Accepts a 0-1 fraction or a 0-100 value. */
export function formatPercent(value, digits = 0) {
  if (!isNum(value)) return "";
  const n = Number(value);
  const pct = Math.abs(n) <= 1 ? n * 100 : n;
  return `${pct.toFixed(digits)}%`;
}

export function formatMetric(value, digits = 3) {
  if (!isNum(value)) return "";
  return Number(value).toFixed(digits);
}

export function formatSigned(value, digits = 2) {
  if (!isNum(value)) return "";
  const n = Number(value);
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(digits)}`;
}

export function formatParameters(value) {
  if (!isNum(value)) return "";
  const n = Number(value);
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)} M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)} K`;
  return String(n);
}

/**
 * Sentences written by the server occasionally carry a code ("shrub_grass", "OUTBOX_SAVED").
 * Product IDs and paths mix digits and capitals, so only all-letter tokens are rewritten.
 */
export function cleanText(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/\b[a-z]+(?:_[a-z]+)+\b/g, (token) => landcoverLabel(token).toLowerCase())
    .replace(/\b[A-Z]+(?:_[A-Z]+)+\b/g, (token) => humanise(token).toLowerCase());
}

/** Human-readable error text. Never leak a stack trace or a raw validation object. */
export function errorText(error, fallback = "Something went wrong. Try again.") {
  const message = error && error.message ? String(error.message) : "";
  if (!message || message.length > 240 || /traceback|exception|sqlalchemy|\bfile "/i.test(message)) return fallback;
  return message;
}
