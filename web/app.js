"use strict";

/* Way2Go frontend (P14a). Plain JS, no build step.
 *
 * Chip -> request mapping: the live POST /route API (docs/API.md) only
 * understands `profiles` + `preferences` today; `priorities` (with
 * Essential/Important/Nice-to-have levels) doesn't affect routing yet --
 * that's P13. So every request also carries a `priorities` object (ignored
 * server-side today, forward-compatible once P13 lands) while the actual
 * routing effect comes from translating Essential/Important chips into real
 * `preferences` overrides on top of the neutral "fastest" profile. See
 * docs/UI.md for the full table and the reasoning.
 */

const CONFIG = window.WAY2GO_CONFIG || { apiBase: "http://localhost:8000" };
const STORAGE_KEY = "way2go_preferences_v1";
const FONT_SCALE_KEY = "way2go_font_scale_v1";

const CHIP_CONFIG = {
  avoid_stairs: { prefKey: "avoid_stairs", essential: true, important: true },
  avoid_steep: { prefKey: "max_slope_pct", essential: 5.0, important: 8.33 },
  curb_cuts: { prefKey: "require_curb_cuts", essential: true, important: true },
  accessible_entrance: { prefKey: "require_accessible_entrance", essential: true, important: true },
  well_lit: { prefKey: "prefer_lit", essential: true, important: true },
};

const DISTANCE_TOLERANCE_BY_LEVEL = { essential: 4.0, important: 2.5 };

const STEPFREE_PREFERENCES = {
  avoid_stairs: true,
  max_slope_pct: 5.0,
  require_curb_cuts: true,
  require_accessible_entrance: true,
  distance_tolerance: 4.0,
};
const STEPFREE_PRIORITIES = {
  avoid_stairs: "essential",
  avoid_steep: "essential",
  curb_cuts: "essential",
  accessible_entrance: "essential",
};

const CODE_TO_CHIP = {
  stairs: "avoid_stairs",
  slope_over_max: "avoid_steep",
  slope_needs_ramp: "avoid_steep",
  slope_over_max_estimated: "avoid_steep",
  curb_cuts: "curb_cuts",
};

const CHIP_DISPLAY_NAME = {
  avoid_stairs: "Avoid stairs",
  avoid_steep: "Avoid steep slopes",
  curb_cuts: "Curb cuts at crossings",
  accessible_entrance: "Prioritize accessible entrances",
  well_lit: "Prefer well-lit paths",
};

const ROUTE_STYLES = {
  recommended: { color: "#0E7C7B", dashArray: null, label: "Recommended (solid)" },
  fastest: { color: "#B5660A", dashArray: "10, 8", label: "Fastest (dashed)" },
  stepfree: { color: "#6B4FBB", dashArray: "2, 6", label: "Step-free (dotted)" },
};

// Demo-zone bbox center (CLAUDE.md §6), used for the initial map view.
const DEMO_CENTER = [42.4475, -76.4848];

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  buildings: [],
  originMode: "current", // "current" | "building"
  chips: Object.fromEntries(
    Object.keys(CHIP_CONFIG).map((key) => [key, { selected: false, level: "essential" }])
  ),
  routes: null, // { recommended, fastest, stepfree } API responses, once loaded
  activeRouteKey: "recommended",
  map: null,
  routeLayer: null,
};

// ---------------------------------------------------------------------------
// localStorage (guest mode only; never accounts)
// ---------------------------------------------------------------------------

function loadPreferences() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    for (const [key, value] of Object.entries(saved)) {
      if (state.chips[key] && value && typeof value === "object") {
        state.chips[key] = { selected: !!value.selected, level: value.level || "essential" };
      }
    }
  } catch (e) {
    // localStorage unavailable (private mode, quota, etc.) -- guest mode
    // just starts fresh each time.
  }
}

function savePreferences() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state.chips));
  } catch (e) {
    // Non-fatal; preferences just won't persist this session.
  }
}

function loadFontScale() {
  try {
    const raw = localStorage.getItem(FONT_SCALE_KEY);
    if (raw) document.documentElement.style.setProperty("--font-scale", raw);
  } catch (e) {
    // ignore
  }
}

function saveFontScale(scale) {
  try {
    localStorage.setItem(FONT_SCALE_KEY, String(scale));
  } catch (e) {
    // ignore
  }
}

// ---------------------------------------------------------------------------
// API client
// ---------------------------------------------------------------------------

class LocationError extends Error {}

async function fetchBuildings() {
  try {
    const res = await fetch(`${CONFIG.apiBase}/buildings`);
    if (!res.ok) return [];
    const body = await res.json();
    return body.buildings || [];
  } catch (e) {
    return [];
  }
}

async function postRoute(body) {
  const res = await fetch(`${CONFIG.apiBase}/route`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 422) {
    const detail = await res.json().catch(() => ({}));
    throw new LocationError(typeof detail.detail === "string" ? detail.detail : "location not found");
  }
  if (!res.ok) {
    throw new Error(`route request failed (${res.status})`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Request building
// ---------------------------------------------------------------------------

function buildPreferencesAndPriorities() {
  const preferences = {};
  const priorities = {};
  let maxTolerance = null;

  for (const [key, chipState] of Object.entries(state.chips)) {
    if (!chipState.selected) continue;
    priorities[key] = chipState.level;

    if (chipState.level === "essential" || chipState.level === "important") {
      const cfg = CHIP_CONFIG[key];
      preferences[cfg.prefKey] = cfg[chipState.level];
      const tolerance = DISTANCE_TOLERANCE_BY_LEVEL[chipState.level];
      if (maxTolerance === null || tolerance > maxTolerance) maxTolerance = tolerance;
    }
  }

  if (maxTolerance !== null) preferences.distance_tolerance = maxTolerance;
  return { preferences, priorities };
}

function currentOriginDestination() {
  const destinationValue = document.getElementById("destination-input").value.trim();
  if (!destinationValue) throw new LocationError("destination is required");
  const destination = { building: destinationValue };

  let origin;
  if (state.originMode === "building") {
    const originValue = document.getElementById("origin-input").value.trim();
    if (!originValue) throw new LocationError("origin is required");
    origin = { building: originValue };
  } else {
    origin = null; // filled in by getCurrentLocation() in findRoute()
  }
  return { origin, destination };
}

function buildRequestBody(kind, origin, destination) {
  const base = {
    origin,
    destination,
    conditions: { darkness: "auto", ice: "auto" },
    include_seed_reports: true,
  };

  if (kind === "fastest") {
    return { ...base, profiles: ["fastest"] };
  }
  if (kind === "stepfree") {
    return {
      ...base,
      profiles: ["fastest"],
      preferences: STEPFREE_PREFERENCES,
      priorities: STEPFREE_PRIORITIES,
    };
  }
  const { preferences, priorities } = buildPreferencesAndPriorities();
  return { ...base, profiles: ["fastest"], preferences, priorities };
}

function getCurrentLocation() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error("geolocation not supported"));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude }),
      () => reject(new Error("geolocation failed")),
      { timeout: 10000 }
    );
  });
}

// ---------------------------------------------------------------------------
// Screen / state transitions
// ---------------------------------------------------------------------------

function showResultsSection() {
  document.querySelector(".results").hidden = false;
}

function setLoading() {
  showResultsSection();
  document.getElementById("loading-state").hidden = false;
  document.getElementById("error-state").hidden = true;
  document.getElementById("result-state").hidden = true;
  announce("Finding a route that works for you…");
}

function setError(message) {
  showResultsSection();
  document.getElementById("loading-state").hidden = true;
  document.getElementById("error-state").hidden = false;
  document.getElementById("result-state").hidden = true;
  document.querySelector("#error-state p").textContent = message;
  announce(message);
}

function setResult() {
  showResultsSection();
  document.getElementById("loading-state").hidden = true;
  document.getElementById("error-state").hidden = true;
  document.getElementById("result-state").hidden = false;
}

function announce(text) {
  document.getElementById("aria-live-region").textContent = text;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function formatSlope(stats) {
  const estimated = (stats.estimated_segments || 0) > 0;
  return `${stats.max_slope_pct.toFixed(1)}%${estimated ? ' <span class="estimated-tag">estimated</span>' : ""}`;
}

function destinationEntranceText(stats) {
  const entrance = stats.destination_entrance;
  if (!entrance) return "Entrance accessibility unknown";
  const accessText =
    entrance.access === "accessible"
      ? "Accessible entrance"
      : entrance.access === "not_accessible"
      ? "Not an accessible entrance"
      : "Entrance accessibility unverified";
  return entrance.auto_opener ? `${accessText}, automatic door` : accessText;
}

function renderSummary(route) {
  const stats = route.stats;
  const dl = document.querySelector("#summary-card .summary-stats");
  dl.innerHTML = "";
  const rows = [
    ["Time", `${stats.est_time_min.toFixed(1)} min`],
    ["Distance", `${Math.round(stats.distance_ft)} ft`],
    ["Stairs avoided", `${stats.steps_avoided}`],
    ["Max slope", formatSlope(stats)],
    ["Well-lit", `${stats.pct_lit.toFixed(0)}%`],
    ["Destination", destinationEntranceText(stats)],
  ];
  for (const [label, html] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.innerHTML = html;
    dl.append(dt, dd);
  }
}

function renderExplanation(route) {
  const shortList = document.getElementById("explanation-list-short");
  const fullList = document.getElementById("explanation-list-full");
  const segmentList = document.getElementById("segment-list");
  shortList.innerHTML = "";
  fullList.innerHTML = "";
  segmentList.innerHTML = "";

  const lines = route.explanation || [];
  lines.slice(0, 4).forEach((line) => {
    const li = document.createElement("li");
    li.textContent = line;
    shortList.appendChild(li);
  });
  lines.forEach((line) => {
    const li = document.createElement("li");
    li.textContent = line;
    fullList.appendChild(li);
  });

  (route.segments || []).forEach((seg) => {
    const li = document.createElement("li");
    const dateLabel = seg.verified && seg.date_surveyed ? `surveyed ${seg.date_surveyed}` : "estimated";
    li.textContent = `${seg.edge_id}: ${seg.slope_pct != null ? seg.slope_pct.toFixed(1) + "% slope, " : ""}${dateLabel}`;
    segmentList.appendChild(li);
  });

  document.getElementById("explanation-details").hidden = true;
  document.getElementById("show-details-btn").setAttribute("aria-expanded", "false");
  document.getElementById("show-details-btn").textContent = "Show details";
}

function renderNoPerfectRoute(fullResponse, route) {
  const banner = document.getElementById("no-perfect-route-banner");
  const list = document.getElementById("compromise-list");
  list.innerHTML = "";

  const compromises = [];

  (route.violations || []).forEach((v) => {
    const chipKey = CODE_TO_CHIP[v.code];
    const chipName = chipKey ? CHIP_DISPLAY_NAME[chipKey] : "a route standard";
    const isEssential = chipKey && state.chips[chipKey].selected && state.chips[chipKey].level === "essential";
    compromises.push({ text: `${chipName}: ${v.message}`, essential: isEssential });
  });

  (fullResponse.warnings || []).forEach((w) => {
    if (w.includes("accessible entrance")) {
      compromises.push({ text: `Prioritize accessible entrances: ${w}`, essential: state.chips.accessible_entrance.selected });
    } else if (w.includes("distance tolerance")) {
      compromises.push({ text: w, essential: false });
    }
  });

  if (compromises.length === 0) {
    banner.hidden = true;
    return;
  }

  // Essential violations are never hidden -- list them first.
  compromises.sort((a, b) => (b.essential ? 1 : 0) - (a.essential ? 1 : 0));
  compromises.forEach((c) => {
    const li = document.createElement("li");
    li.textContent = c.text;
    if (c.essential) li.style.fontWeight = "700";
    list.appendChild(li);
  });
  banner.hidden = false;
}

function renderCompareCards() {
  const keyMap = { recommended: "recommended", fastest: "fastest", stepfree: "stepfree" };
  document.querySelectorAll(".compare-card").forEach((card) => {
    const key = card.dataset.routeKey;
    const response = state.routes[keyMap[key]];
    const route = response.routes[0];
    const dl = card.querySelector(".compare-stats");
    dl.innerHTML = "";
    const rows = [
      ["min", `${route.stats.est_time_min.toFixed(1)}`],
      ["stairs", `${route.stats.steps_avoided === 0 ? "uses stairs" : "0 stairs"}`],
      ["slope", `${route.stats.max_slope_pct.toFixed(1)}%`],
    ];
    rows.forEach(([label, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      dl.append(dt, dd);
    });
    card.setAttribute("aria-pressed", String(key === state.activeRouteKey));
  });
}

function drawRoute(key) {
  const keyMap = { recommended: "recommended", fastest: "fastest", stepfree: "stepfree" };
  const response = state.routes[keyMap[key]];
  const route = response.routes[0];
  const style = ROUTE_STYLES[key];

  if (state.routeLayer) {
    state.map.removeLayer(state.routeLayer);
  }

  const latLngs = route.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
  state.routeLayer = L.polyline(latLngs, {
    color: style.color,
    weight: 5,
    dashArray: style.dashArray,
  }).addTo(state.map);
  state.map.fitBounds(state.routeLayer.getBounds(), { padding: [24, 24] });
}

function selectRoute(key) {
  state.activeRouteKey = key;
  const keyMap = { recommended: "recommended", fastest: "fastest", stepfree: "stepfree" };
  const response = state.routes[keyMap[key]];
  const route = response.routes[0];

  renderSummary(route);
  renderExplanation(route);
  renderNoPerfectRoute(response, route);
  renderCompareCards();
  drawRoute(key);
  announce(
    `${key === "recommended" ? "Recommended" : key === "fastest" ? "Fastest" : "Step-free"} route: ` +
      `${route.stats.est_time_min.toFixed(1)} minutes, ${Math.round(route.stats.distance_ft)} feet, ` +
      `${route.stats.steps_avoided} stairs avoided.`
  );
}

// ---------------------------------------------------------------------------
// Map
// ---------------------------------------------------------------------------

function initMap() {
  state.map = L.map("map").setView(DEMO_CENTER, 16);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(state.map);
}

// ---------------------------------------------------------------------------
// Main route-finding flow
// ---------------------------------------------------------------------------

async function findRoute() {
  document.getElementById("find-route-btn").disabled = true;
  setLoading();

  try {
    const { destination } = currentOriginDestination();
    let origin;
    if (state.originMode === "current") {
      origin = await getCurrentLocation().catch(() => {
        throw new LocationError("We couldn't get your current location. Try entering a starting building instead.");
      });
    } else {
      ({ origin } = currentOriginDestination());
    }

    const [recommended, fastest, stepfree] = await Promise.all([
      postRoute(buildRequestBody("recommended", origin, destination)),
      postRoute(buildRequestBody("fastest", origin, destination)),
      postRoute(buildRequestBody("stepfree", origin, destination)),
    ]);

    state.routes = { recommended, fastest, stepfree };
    setResult();
    selectRoute("recommended");
  } catch (e) {
    if (e instanceof LocationError) {
      setError(e.message === "destination is required" || e.message === "origin is required"
        ? "We couldn't find that location."
        : e.message);
    } else {
      setError("We couldn't find that location.");
    }
  } finally {
    document.getElementById("find-route-btn").disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function updateFindButtonEnabled() {
  const destination = document.getElementById("destination-input").value.trim();
  const origin = document.getElementById("origin-input").value.trim();
  const ready = Boolean(destination) && (state.originMode === "current" || Boolean(origin));
  document.getElementById("find-route-btn").disabled = !ready;
}

function wireChips() {
  document.querySelectorAll(".chip-card").forEach((card) => {
    const key = card.dataset.chip;
    const toggle = card.querySelector(".chip-toggle");
    const importanceGroup = card.querySelector(".importance-group");

    const chipState = state.chips[key];
    toggle.setAttribute("aria-pressed", String(chipState.selected));
    importanceGroup.hidden = !chipState.selected;
    card.querySelector(`input[name="importance-${key}"][value="${chipState.level}"]`).checked = true;

    toggle.addEventListener("click", () => {
      chipState.selected = !chipState.selected;
      toggle.setAttribute("aria-pressed", String(chipState.selected));
      importanceGroup.hidden = !chipState.selected;
      savePreferences();
    });

    importanceGroup.addEventListener("change", (ev) => {
      chipState.level = ev.target.value;
      savePreferences();
    });
  });
}

function wireOrigin() {
  const originInput = document.getElementById("origin-input");
  const changeBtn = document.getElementById("origin-change-btn");
  const note = document.getElementById("geolocation-note");

  function applyMode() {
    if (state.originMode === "current") {
      originInput.disabled = true;
      originInput.value = "";
      originInput.placeholder = "Current location";
      changeBtn.textContent = "Use a building instead";
      note.hidden = false;
    } else {
      originInput.disabled = false;
      originInput.placeholder = "Search for a building…";
      changeBtn.textContent = "Use current location instead";
      note.hidden = true;
    }
    updateFindButtonEnabled();
  }

  changeBtn.addEventListener("click", () => {
    state.originMode = state.originMode === "current" ? "building" : "current";
    applyMode();
  });

  originInput.addEventListener("input", updateFindButtonEnabled);
  applyMode();
}

function wireTextSize() {
  loadFontScale();
  document.getElementById("font-decrease").addEventListener("click", () => {
    const current = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--font-scale")) || 1;
    const next = Math.max(0.85, current - 0.1);
    document.documentElement.style.setProperty("--font-scale", String(next));
    saveFontScale(next);
  });
  document.getElementById("font-increase").addEventListener("click", () => {
    const current = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--font-scale")) || 1;
    const next = Math.min(1.6, current + 0.1);
    document.documentElement.style.setProperty("--font-scale", String(next));
    saveFontScale(next);
  });
}

function wireCompareCards() {
  document.querySelectorAll(".compare-card").forEach((card) => {
    card.addEventListener("click", () => selectRoute(card.dataset.routeKey));
  });
}

function wireShowDetails() {
  const btn = document.getElementById("show-details-btn");
  const details = document.getElementById("explanation-details");
  btn.addEventListener("click", () => {
    const expanded = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!expanded));
    details.hidden = expanded;
    btn.textContent = expanded ? "Show details" : "Hide details";
  });
}

async function init() {
  loadPreferences();
  wireChips();
  wireOrigin();
  wireTextSize();
  wireCompareCards();
  wireShowDetails();
  initMap();

  document.getElementById("destination-input").addEventListener("input", updateFindButtonEnabled);
  document.getElementById("find-route-btn").addEventListener("click", findRoute);
  document.getElementById("search-again-btn").addEventListener("click", () => {
    document.querySelector(".results").hidden = true;
  });

  state.buildings = await fetchBuildings();
  const datalist = document.getElementById("buildings-list");
  datalist.innerHTML = "";
  state.buildings.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    datalist.appendChild(option);
  });
}

document.addEventListener("DOMContentLoaded", init);
