// Leaflet setup shared by every screen. Tiles are the one thing allowed to come over the network;
// when they fail the map stays a clean dark surface with geometry still drawn on it.

const L = () => window.L;

const IMAGERY_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const LABELS_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}";
const ATTRIBUTION = "Imagery &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community";
const BLANK_TILE = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";

export const INDIA_BOUNDS = [[6.5, 68], [35.8, 97.5]];

export const HEALTH_COLOURS = {
  ALERT: "#f0564f",
  ERROR: "#fab219",
  OK: "#3ecf8e",
  CLOUDED: "#9fb4c7",
  PAUSED: "#8b95a1",
};

export function createMap(container, options = {}) {
  const leaflet = L();
  if (!leaflet) throw new Error("Map library failed to load.");
  const { labels = true, interactive = true, ...rest } = options;
  const map = leaflet.map(container, {
    zoomControl: interactive,
    attributionControl: true,
    dragging: interactive,
    scrollWheelZoom: interactive,
    doubleClickZoom: interactive,
    boxZoom: interactive,
    keyboard: interactive,
    touchZoom: interactive,
    zoomSnap: 0.25,
    worldCopyJump: false,
    minZoom: 3,
    maxZoom: 19,
    preferCanvas: false,
    ...rest,
  });
  map.attributionControl.setPrefix(false);
  const tileOptions = {
    maxNativeZoom: 18,
    maxZoom: 19,
    errorTileUrl: BLANK_TILE,
    crossOrigin: false,
    updateWhenIdle: false,
    keepBuffer: 3,
  };
  const imagery = leaflet.tileLayer(IMAGERY_URL, { ...tileOptions, attribution: ATTRIBUTION });
  imagery.addTo(map);
  if (labels) {
    // Place names sit above the imagery but under AOI vectors.
    map.createPane("labels");
    map.getPane("labels").style.zIndex = 350;
    map.getPane("labels").style.pointerEvents = "none";
    leaflet.tileLayer(LABELS_URL, { ...tileOptions, pane: "labels" }).addTo(map);
  }
  let errors = 0;
  let loads = 0;
  imagery.on("tileerror", () => {
    errors += 1;
    if (errors > 6 && loads === 0) container.classList.add("map-offline");
  });
  imagery.on("tileload", (event) => {
    // A failed tile "loads" the blank placeholder, which says nothing about the network.
    if (event.tile && event.tile.src === BLANK_TILE) return;
    loads += 1;
    errors = 0;
    container.classList.remove("map-offline");
  });
  return map;
}

export function geometryBounds(geometry) {
  const leaflet = L();
  if (!leaflet || !geometry || !geometry.type) return null;
  try {
    const bounds = leaflet.geoJSON(geometry).getBounds();
    return bounds.isValid() ? bounds : null;
  } catch {
    return null;
  }
}

/** [west, south, east, north] to Leaflet bounds. */
export function bboxBounds(bbox) {
  if (!Array.isArray(bbox) || bbox.length !== 4 || bbox.some((value) => !Number.isFinite(Number(value)))) return null;
  return L().latLngBounds([bbox[1], bbox[0]], [bbox[3], bbox[2]]);
}

export function areaBounds(area) {
  if (!area) return null;
  return geometryBounds(area.geometry) || bboxBounds(area.bbox) || null;
}

/** Safe wrapper: fit to bounds now, with no animation, tolerating a hidden container. */
export function fitTo(map, bounds, { padding = [24, 24], maxZoom = 15 } = {}) {
  if (!bounds || !bounds.isValid || !bounds.isValid()) return;
  map.invalidateSize({ animate: false });
  map.fitBounds(bounds, { padding, maxZoom, animate: false });
}

export function flyTo(map, bounds, { padding = [40, 40], maxZoom = 14 } = {}) {
  if (!bounds || !bounds.isValid || !bounds.isValid()) return;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) map.fitBounds(bounds, { padding, maxZoom, animate: false });
  else map.flyToBounds(bounds, { padding, maxZoom, duration: 1.1, easeLinearity: 0.3 });
}

export function polygonStyle(colour, extra = {}) {
  return { color: colour, weight: 2, opacity: 0.95, fillColor: colour, fillOpacity: 0.14, ...extra };
}

/** Round-area in km² on a sphere, used for the AOI size check. */
export function geodesicAreaKm2(geometry) {
  if (!geometry) return 0;
  const R = 6378137;
  const rad = (deg) => (deg * Math.PI) / 180;
  const ringArea = (ring) => {
    let total = 0;
    for (let i = 0; i < ring.length - 1; i += 1) {
      const [lon1, lat1] = ring[i];
      const [lon2, lat2] = ring[i + 1];
      total += rad(lon2 - lon1) * (2 + Math.sin(rad(lat1)) + Math.sin(rad(lat2)));
    }
    return Math.abs((total * R * R) / 2);
  };
  const polygonArea = (rings) => rings.reduce((sum, ring, index) => sum + (index === 0 ? ringArea(ring) : -ringArea(ring)), 0);
  const metres = geometry.type === "Polygon"
    ? polygonArea(geometry.coordinates)
    : geometry.type === "MultiPolygon"
      ? geometry.coordinates.reduce((sum, polygon) => sum + polygonArea(polygon), 0)
      : 0;
  return metres / 1e6;
}

/** East-west and north-south extent of a geometry, close to what a local UTM grid will measure. */
export function geometrySpanKm(geometry) {
  const bounds = geometryBounds(geometry);
  if (!bounds) return { widthKm: 0, heightKm: 0 };
  const midLat = (bounds.getSouth() + bounds.getNorth()) / 2;
  return {
    widthKm: (bounds.getEast() - bounds.getWest()) * 111.32 * Math.cos((midLat * Math.PI) / 180),
    heightKm: (bounds.getNorth() - bounds.getSouth()) * 110.9,
  };
}

/**
 * A draw control restyled to the console: toolbar buttons call these methods, with no default
 * Leaflet.draw chrome. Returns {start(kind), clear(), setGeometry(g), geometry(), destroy()}.
 */
export function createDrawer(map, { colour = "#7fb2ff", onChange }) {
  const leaflet = L();
  const items = new leaflet.FeatureGroup().addTo(map);
  let handler = null;
  const shapeOptions = { color: colour, weight: 2, fillColor: colour, fillOpacity: 0.16, opacity: 1 };

  const stop = () => {
    if (handler) {
      handler.disable();
      handler = null;
    }
  };
  const emitChange = () => {
    const layers = items.getLayers();
    onChange(layers.length ? layers[0].toGeoJSON().geometry : null);
  };
  const onCreated = (event) => {
    items.clearLayers();
    items.addLayer(event.layer);
    handler = null;
    emitChange();
  };
  map.on(leaflet.Draw.Event.CREATED, onCreated);

  return {
    start(kind) {
      stop();
      handler = kind === "rectangle"
        ? new leaflet.Draw.Rectangle(map, { shapeOptions, showArea: false, metric: true })
        : new leaflet.Draw.Polygon(map, { shapeOptions, allowIntersection: false, showArea: false, metric: true, showLength: false });
      handler.enable();
    },
    stop,
    clear() {
      stop();
      items.clearLayers();
      onChange(null);
    },
    setGeometry(geometry) {
      items.clearLayers();
      if (geometry) {
        const layer = leaflet.geoJSON(geometry, { style: () => shapeOptions });
        layer.eachLayer((child) => items.addLayer(child));
      }
      onChange(geometry || null);
    },
    destroy() {
      stop();
      map.off(leaflet.Draw.Event.CREATED, onCreated);
      items.remove();
    },
  };
}
