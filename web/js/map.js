// Way2Go map rendering (P15/S1): OpenStreetMap standard basemap (with an
// Esri World Imagery satellite toggle), the signature gradient-ribbon
// route, grey dashed/dotted comparison routes, and bottom-right
// zoom/recenter/basemap controls.

const DEMO_CENTER = [42.4475, -76.4848]; // CLAUDE.md §6 demo-zone bbox center

const GRADIENT_ID = "way2go-route-gradient";

const COMPARISON_STYLE = {
  fastest: { color: "#9A8F93", dashArray: "10, 8" }, // dashed
  stepfree: { color: "#9A8F93", dashArray: "2, 6" }, // dotted
};

const prefersReducedMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// [VERIFY] (S1): both tile URLs confirmed 200 via curl this session.
const OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const SATELLITE_TILE_URL =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const SATELLITE_ATTRIBUTION =
  "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community";

export function initMap(containerId) {
  const map = L.map(containerId, { zoomControl: false }).setView(DEMO_CENTER, 16);

  const streetLayer = L.tileLayer(OSM_TILE_URL, {
    maxZoom: 19,
    attribution: OSM_ATTRIBUTION,
  }).addTo(map);
  const satelliteLayer = L.tileLayer(SATELLITE_TILE_URL, {
    maxZoom: 19,
    attribution: SATELLITE_ATTRIBUTION,
  });

  addMapControls(map, { streetLayer, satelliteLayer });
  return map;
}

function addMapControls(map, basemaps) {
  const control = L.control({ position: "bottomright" });
  control.onAdd = () => {
    const div = L.DomUtil.create("div", "map-controls");
    div.innerHTML = `
      <button type="button" class="map-control-btn" id="map-zoom-in" aria-label="Zoom in">+</button>
      <button type="button" class="map-control-btn" id="map-zoom-out" aria-label="Zoom out">&minus;</button>
      <button type="button" class="map-control-btn" id="map-recenter" aria-label="Recenter map">&#9678;</button>
      <button type="button" class="map-control-btn" id="map-basemap-toggle" aria-label="Switch to satellite view" aria-pressed="false">&#9733;</button>
    `;
    L.DomEvent.disableClickPropagation(div);
    div.querySelector("#map-zoom-in").addEventListener("click", () => map.zoomIn());
    div.querySelector("#map-zoom-out").addEventListener("click", () => map.zoomOut());
    div.querySelector("#map-recenter").addEventListener("click", () => map.setView(DEMO_CENTER, 16));

    const toggleBtn = div.querySelector("#map-basemap-toggle");
    let satelliteOn = false;
    toggleBtn.addEventListener("click", () => {
      satelliteOn = !satelliteOn;
      if (satelliteOn) {
        map.removeLayer(basemaps.streetLayer);
        basemaps.satelliteLayer.addTo(map);
        toggleBtn.setAttribute("aria-label", "Switch to map view");
        toggleBtn.setAttribute("aria-pressed", "true");
      } else {
        map.removeLayer(basemaps.satelliteLayer);
        basemaps.streetLayer.addTo(map);
        toggleBtn.setAttribute("aria-label", "Switch to satellite view");
        toggleBtn.setAttribute("aria-pressed", "false");
      }
    });
    return div;
  };
  control.addTo(map);
}

function startRingIcon() {
  return L.divIcon({
    className: "way2go-marker way2go-marker-start",
    html: '<svg width="18" height="18" viewBox="0 0 18 18"><circle cx="9" cy="9" r="6" fill="#FFF9EC" stroke="#A8445A" stroke-width="3"/></svg>',
    iconSize: [18, 18],
    iconAnchor: [9, 9],
  });
}

function endPinIcon() {
  return L.divIcon({
    className: "way2go-marker way2go-marker-end",
    html:
      '<svg width="28" height="36" viewBox="0 0 28 36">' +
      '<path d="M14 0C6.3 0 0 6.3 0 14c0 10 14 22 14 22s14-12 14-22C28 6.3 21.7 0 14 0z" fill="#A8445A"/>' +
      '<rect x="10.5" y="9" width="7" height="10" rx="1" fill="#FFF9EC"/>' +
      '<circle cx="15.3" cy="14" r="0.9" fill="#A8445A"/>' +
      "</svg>",
    iconSize: [28, 36],
    iconAnchor: [14, 34],
  });
}

function ensureGradientDef(map) {
  const svg = map.getPane("overlayPane").querySelector("svg");
  if (!svg) return null;
  let defs = svg.querySelector("defs");
  if (!defs) {
    defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    svg.insertBefore(defs, svg.firstChild);
  }
  let gradient = defs.querySelector(`#${GRADIENT_ID}`);
  if (!gradient) {
    gradient = document.createElementNS("http://www.w3.org/2000/svg", "linearGradient");
    gradient.setAttribute("id", GRADIENT_ID);
    gradient.setAttribute("gradientUnits", "userSpaceOnUse");
    const stop1 = document.createElementNS("http://www.w3.org/2000/svg", "stop");
    stop1.setAttribute("offset", "0%");
    stop1.setAttribute("stop-color", "#FBB1BD");
    const stop2 = document.createElementNS("http://www.w3.org/2000/svg", "stop");
    stop2.setAttribute("offset", "100%");
    stop2.setAttribute("stop-color", "#FFD9C2");
    gradient.append(stop1, stop2);
    defs.appendChild(gradient);
  }
  return gradient;
}

function positionGradient(map, gradient, latLngs) {
  if (!gradient || !latLngs.length) return;
  const start = map.latLngToLayerPoint(latLngs[0]);
  const end = map.latLngToLayerPoint(latLngs[latLngs.length - 1]);
  gradient.setAttribute("x1", start.x);
  gradient.setAttribute("y1", start.y);
  gradient.setAttribute("x2", end.x);
  gradient.setAttribute("y2", end.y);
}

function drawOnAnimate(path) {
  if (!path || prefersReducedMotion()) return;
  try {
    const length = path.getTotalLength();
    path.style.transition = "none";
    path.setAttribute("stroke-dasharray", `${length}`);
    path.setAttribute("stroke-dashoffset", `${length}`);
    requestAnimationFrame(() => {
      path.style.transition = "stroke-dashoffset 600ms ease-out";
      path.setAttribute("stroke-dashoffset", "0");
    });
  } catch (e) {
    // SVG path APIs unavailable (e.g. Canvas renderer fallback) -- skip the animation.
  }
}

/**
 * Draw the three routes. `selectedKey` gets the gradient-halo + rosewood
 * core treatment and start/end markers; the other two are muted grey,
 * dashed (fastest) or dotted (stepfree), per the design spec.
 */
export function drawRoutes(map, mapState, routesByKey, selectedKey) {
  clearRoutes(map, mapState);

  for (const key of ["fastest", "stepfree", "recommended"]) {
    const response = routesByKey[key];
    if (!response) continue;
    const route = response.routes[0];
    const latLngs = route.geometry.coordinates.map(([lon, lat]) => [lat, lon]);

    if (key === selectedKey) {
      const halo = L.polyline(latLngs, { color: `url(#${GRADIENT_ID})`, weight: 12, opacity: 0.9 }).addTo(map);
      const core = L.polyline(latLngs, { color: "#A8445A", weight: 4 }).addTo(map);
      mapState.layers.push(halo, core);

      const gradient = ensureGradientDef(map);
      positionGradient(map, gradient, latLngs);
      mapState.onZoom = () => positionGradient(map, gradient, latLngs);
      map.on("zoomend", mapState.onZoom);

      const startMarker = L.marker(latLngs[0], { icon: startRingIcon(), keyboard: false }).addTo(map);
      const endMarker = L.marker(latLngs[latLngs.length - 1], { icon: endPinIcon(), keyboard: false }).addTo(map);
      mapState.layers.push(startMarker, endMarker);

      const corePath = core.getElement ? core.getElement() : null;
      drawOnAnimate(corePath);

      map.fitBounds(halo.getBounds(), { padding: [40, 40] });
    } else {
      const style = COMPARISON_STYLE[key];
      const line = L.polyline(latLngs, { color: style.color, weight: 3, dashArray: style.dashArray, opacity: 0.8 }).addTo(
        map
      );
      mapState.layers.push(line);
    }
  }
}

export function clearRoutes(map, mapState) {
  if (mapState.onZoom) {
    map.off("zoomend", mapState.onZoom);
    mapState.onZoom = null;
  }
  mapState.layers.forEach((layer) => map.removeLayer(layer));
  mapState.layers = [];
}

export function recenterDemoZone(map) {
  map.setView(DEMO_CENTER, 16);
}
