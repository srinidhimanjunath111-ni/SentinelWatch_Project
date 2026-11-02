// Hash routing. Screens are deep-linkable and the browser back button is the navigation model.

const ROUTES = [
  [/^\/?$/, "operations"],
  [/^\/operations$/, "operations"],
  [/^\/case\/([^/]+)$/, "case", ["id"]],
  [/^\/site\/([^/]+)$/, "site", ["id"]],
  [/^\/add-site$/, "add-site"],
  [/^\/alerts$/, "alerts"],
  [/^\/departments$/, "departments"],
  [/^\/departments\/([^/]+)$/, "departments", ["id"]],
  [/^\/zones$/, "zones"],
  [/^\/models$/, "models"],
];

export function parseRoute(hash = window.location.hash) {
  const raw = hash.replace(/^#/, "");
  const [path, search = ""] = raw.split("?");
  for (const [pattern, name, keys = []] of ROUTES) {
    const match = path.match(pattern);
    if (match) {
      const params = {};
      keys.forEach((key, index) => {
        params[key] = decodeURIComponent(match[index + 1]);
      });
      return { name, params, query: new URLSearchParams(search), path };
    }
  }
  return { name: "not-found", params: {}, query: new URLSearchParams(search), path };
}

let suppressNext = false;

/** Update the query string for the current screen without re-mounting it. */
export function navigateQuery(values) {
  const { path } = parseRoute();
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value) search.set(key, value);
  const text = search.toString();
  const target = `#${path || "/"}${text ? `?${text}` : ""}`;
  if (target !== window.location.hash) {
    suppressNext = true;
    window.location.hash = target;
  }
}

export function startRouter(onRoute) {
  window.addEventListener("hashchange", () => {
    if (suppressNext) {
      suppressNext = false;
      return;
    }
    onRoute(parseRoute());
  });
  onRoute(parseRoute());
}
