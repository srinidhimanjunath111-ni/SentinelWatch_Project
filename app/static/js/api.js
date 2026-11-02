// Thin typed-ish wrapper over the HTTP contract. Errors carry a sentence safe to show an operator.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function detailMessage(body, status) {
  const detail = body && body.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map((item) => String(item.msg || "").replace(/^Value error,\s*/i, ""))
      .filter(Boolean)
      .join(" ");
  }
  if (status === 404) return "This record no longer exists.";
  if (status >= 500) return "The server could not complete that request.";
  return "The request was not accepted.";
}

let authHandler = null;

/** Deployments may protect writes with a password. The handler signs in and resolves true to retry. */
export function setAuthHandler(fn) {
  authHandler = fn;
}

export const login = (password) => request("/api/auth/login", { method: "POST", body: { password }, noRetry: true });

async function request(path, { method = "GET", body, timeout = 30000, noRetry = false } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  let response;
  try {
    response = await fetch(path, {
      method,
      signal: controller.signal,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error.name === "AbortError") throw new ApiError("The server took too long to respond.", 0);
    throw new ApiError("Cannot reach the SentinelWatch server.", 0);
  } finally {
    clearTimeout(timer);
  }
  if (response.status === 401 && !noRetry && authHandler && (await authHandler())) {
    return request(path, { method, body, timeout, noRetry: true });
  }
  if (!response.ok) {
    let parsed = null;
    try {
      parsed = await response.json();
    } catch {
      parsed = null;
    }
    throw new ApiError(detailMessage(parsed, response.status), response.status);
  }
  if (response.status === 204) return null;
  try {
    return await response.json();
  } catch {
    throw new ApiError("The server sent an unreadable response.", response.status);
  }
}

function query(params = {}) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") search.set(key, value);
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

const id = encodeURIComponent;

export const api = {
  overview: () => request("/api/overview"),
  system: () => request("/api/system/status"),
  dataStatus: () => request("/api/data-status"),

  areas: () => request("/api/areas"),
  area: (areaId) => request(`/api/areas/${id(areaId)}`),
  createArea: (body) => request("/api/areas", { method: "POST", body }),
  updateArea: (areaId, body) => request(`/api/areas/${id(areaId)}`, { method: "PATCH", body }),
  deleteArea: (areaId) => request(`/api/areas/${id(areaId)}`, { method: "DELETE" }),
  checkNow: (areaId) => request(`/api/areas/${id(areaId)}/check-now`, { method: "POST" }),
  pause: (areaId) => request(`/api/areas/${id(areaId)}/pause`, { method: "POST" }),
  resume: (areaId) => request(`/api/areas/${id(areaId)}/resume`, { method: "POST" }),
  replay: (areaId, body) => request(`/api/areas/${id(areaId)}/replay`, { method: "POST", body, timeout: 60000 }),
  acquisitions: (areaId) => request(`/api/areas/${id(areaId)}/acquisitions`),
  timeline: (areaId) => request(`/api/areas/${id(areaId)}/timeline`),
  run: (runId) => request(`/api/monitoring-runs/${id(runId)}`),

  alerts: (params) => request(`/api/alerts${query(params)}`),
  alert: (alertId) => request(`/api/alerts/${id(alertId)}`),
  setAlertStatus: (alertId, body) => request(`/api/alerts/${id(alertId)}/status`, { method: "PATCH", body }),
  redispatch: (alertId, body = {}) => request(`/api/alerts/${id(alertId)}/dispatch`, { method: "POST", body }),

  cases: (params) => request(`/api/cases${query(params)}`),
  case: (caseId) => request(`/api/cases/${id(caseId)}`),
  async reportFile(caseId) {
    let response;
    try {
      response = await fetch(`/api/cases/${id(caseId)}/report`);
    } catch {
      throw new ApiError("Cannot reach the SentinelWatch server.", 0);
    }
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new ApiError(detailMessage(body, response.status), response.status);
    }
    const match = /filename="?([^";]+)"?/.exec(response.headers.get("Content-Disposition") || "");
    return { blob: await response.blob(), filename: match ? match[1] : "sentinelwatch-report.pdf" };
  },

  departments: () => request("/api/departments"),
  inbox: (departmentId) => request(`/api/departments/${id(departmentId)}/inbox`),
  dispatches: (params) => request(`/api/dispatches${query(params)}`),

  zones: () => request("/api/authorised-zones"),
  createZone: (body) => request("/api/authorised-zones", { method: "POST", body }),
  deleteZone: (zoneId) => request(`/api/authorised-zones/${id(zoneId)}`, { method: "DELETE" }),

  models: () => request("/api/models"),
};

export function asList(value, ...keys) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === "object") {
    for (const key of [...keys, "items", "results", "data"]) {
      if (Array.isArray(value[key])) return value[key];
    }
  }
  return [];
}

/** Place search. Failure is an empty result; the form still works by drawing. */
export async function nominatimSearch(text, signal) {
  const params = new URLSearchParams({ q: text, format: "jsonv2", limit: "6", countrycodes: "in", addressdetails: "1" });
  try {
    const response = await fetch(`https://nominatim.openstreetmap.org/search?${params}`, {
      signal,
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return [];
    const rows = await response.json();
    return Array.isArray(rows) ? rows : [];
  } catch {
    return [];
  }
}
