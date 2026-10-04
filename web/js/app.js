// Way2Go app (P15): guided step flow + map-first results, built on top of
// F1's live `priorities` support and P14a's working API-calling logic
// (see js/api.js).

import { fetchBuildings, postRoute, buildRequestBody, getCurrentLocation, LocationError } from "./api.js";
import { initMap, drawRoutes, clearRoutes, recenterDemoZone } from "./map.js";
import { init as initAuth } from "./auth.js";
import { init as initLive } from "./live.js";
import { init as initFacilities } from "./facilities.js";
import { init as initAi } from "./ai.js";
import { init as initLayers } from "./layers.js";

const STORAGE_KEY = "way2go_preferences_v2";
const FONT_SCALE_KEY = "way2go_font_scale_v1";

const CHIP_IDS = ["avoid_stairs", "avoid_steep", "curb_cuts", "accessible_entrance", "well_lit"];
const CHIP_LABEL = {
  avoid_stairs: "Avoid stairs",
  avoid_steep: "Avoid steep slopes",
  curb_cuts: "Curb cuts at crossings",
  accessible_entrance: "Accessible entrances",
  well_lit: "Well-lit paths",
};
const VIOLATION_CODE_TO_CHIP = {
  stairs: "avoid_stairs",
  slope_over_max: "avoid_steep",
  slope_needs_ramp: "avoid_steep",
  slope_over_max_estimated: "avoid_steep",
  curb_cuts: "curb_cuts",
};

const state = {
  step: 1, // 1..4, or "results"
  buildings: [],
  destination: "",
  originMode: "building", // "current" | "building"
  originValue: "",
  originCoords: null,
  chips: Object.fromEntries(CHIP_IDS.map((id) => [id, { selected: false, level: "essential" }])),
  routes: null, // { recommended, fastest, stepfree }
  selectedCompare: "recommended",
};

const mapState = { layers: [], onZoom: null };
let map = null;
const routeShownCallbacks = [];

// ---------------------------------------------------------------------------
// ctx shared with P16/P17 stub modules
// ---------------------------------------------------------------------------

const ctx = {
  get map() {
    return map;
  },
  api: { fetchBuildings, postRoute, buildRequestBody },
  get request() {
    return { ...state };
  },
  reroute: () => findRoute(),
  announce,
  getPrefs: () => loadPreferences(),
  setPrefs: (prefs) => savePreferences(prefs),
  onRouteShown: (cb) => routeShownCallbacks.push(cb),
};

// ---------------------------------------------------------------------------
// localStorage (guest mode only; wrapped in try/catch)
// ---------------------------------------------------------------------------

function loadPreferences() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    return null;
  }
}

function savePreferences(chips) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(chips));
  } catch (e) {
    // Non-fatal; just won't persist this session.
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

function announce(text) {
  document.getElementById("aria-live-region").textContent = text;
}

// ---------------------------------------------------------------------------
// Step progress dots
// ---------------------------------------------------------------------------

function renderStepProgress() {
  const progress = document.getElementById("step-progress");
  const isStepScreen = typeof state.step === "number";
  progress.hidden = !isStepScreen;
  if (!isStepScreen) return;
  progress.querySelectorAll(".progress-dot").forEach((dot) => {
    const n = Number(dot.dataset.step);
    dot.classList.toggle("is-complete", n < state.step);
    dot.classList.toggle("is-current", n === state.step);
  });
}

// ---------------------------------------------------------------------------
// Step-action buttons (shared layout; content differs per step)
// ---------------------------------------------------------------------------

function renderStepActions(container, { showBack, showSkip, continueLabel, onBack, onSkip, onContinue, continueDisabled, continuePrimary }) {
  const actions = document.createElement("div");
  actions.className = "step-actions";

  if (showBack) {
    const back = document.createElement("button");
    back.type = "button";
    back.className = "choice-button";
    back.textContent = "Back";
    back.addEventListener("click", onBack);
    actions.appendChild(back);
  }

  const rightGroup = document.createElement("div");
  rightGroup.style.display = "flex";
  rightGroup.style.gap = "var(--space-2)";
  rightGroup.style.marginLeft = "auto";

  if (showSkip) {
    const skip = document.createElement("button");
    skip.type = "button";
    skip.className = "link-button";
    skip.textContent = "Skip";
    skip.addEventListener("click", onSkip);
    rightGroup.appendChild(skip);
  }

  const cont = document.createElement("button");
  cont.type = "button";
  cont.className = continuePrimary || !showBack ? "primary-button" : "choice-button";
  cont.textContent = continueLabel || "Continue";
  cont.disabled = Boolean(continueDisabled);
  cont.addEventListener("click", onContinue);
  rightGroup.appendChild(cont);

  actions.appendChild(rightGroup);
  container.appendChild(actions);
  return cont;
}

// ---------------------------------------------------------------------------
// Step 1: destination
// ---------------------------------------------------------------------------

function renderStep1() {
  const content = document.getElementById("step-content");
  content.innerHTML = "";
  content.appendChild(document.getElementById("tpl-step-destination").content.cloneNode(true));

  const input = document.getElementById("destination-input");
  const hint = document.getElementById("destination-hint");
  input.value = state.destination;

  function updateHint() {
    const value = input.value.trim();
    if (!value) {
      hint.textContent = "";
      return;
    }
    const matches = state.buildings.some((b) => b.toLowerCase().includes(value.toLowerCase()));
    hint.textContent = matches ? "" : "No matching building yet -- check the spelling, or keep typing.";
  }

  const continueBtn = renderStepActions(content, {
    showBack: false,
    continueLabel: "Continue",
    continueDisabled: !state.destination.trim(),
    onContinue: () => {
      state.destination = input.value.trim();
      goToStep(2);
    },
  });

  input.addEventListener("input", () => {
    continueBtn.disabled = !input.value.trim();
    updateHint();
  });
  updateHint();
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && input.value.trim()) {
      state.destination = input.value.trim();
      goToStep(2);
    }
  });

  input.focus();
  announce("Where to? Type a building name.");
}

// ---------------------------------------------------------------------------
// Step 2: origin
// ---------------------------------------------------------------------------

function renderStep2() {
  const content = document.getElementById("step-content");
  content.innerHTML = "";
  content.appendChild(document.getElementById("tpl-step-origin").content.cloneNode(true));

  const useLocationBtn = document.getElementById("origin-use-location");
  const useBuildingBtn = document.getElementById("origin-use-building");
  const note = document.getElementById("geolocation-note");
  const buildingField = document.getElementById("origin-building-field");
  const originInput = document.getElementById("origin-input");
  const errorEl = document.getElementById("origin-error");

  originInput.value = state.originValue;

  function applyMode() {
    useLocationBtn.setAttribute("aria-pressed", String(state.originMode === "current"));
    useBuildingBtn.setAttribute("aria-pressed", String(state.originMode === "building"));
    note.hidden = state.originMode !== "current";
    buildingField.hidden = state.originMode !== "building";
    updateContinueState();
  }

  let continueBtn;
  function updateContinueState() {
    if (!continueBtn) return;
    const ready = state.originMode === "current" || Boolean(originInput.value.trim());
    continueBtn.disabled = !ready;
  }

  useLocationBtn.addEventListener("click", () => {
    state.originMode = "current";
    applyMode();
  });
  useBuildingBtn.addEventListener("click", () => {
    state.originMode = "building";
    applyMode();
    originInput.focus();
  });
  originInput.addEventListener("input", () => {
    state.originValue = originInput.value;
    updateContinueState();
  });

  continueBtn = renderStepActions(content, {
    showBack: true,
    continueLabel: "Continue",
    onBack: () => goToStep(1),
    onContinue: async () => {
      errorEl.hidden = true;
      if (state.originMode === "current") {
        try {
          state.originCoords = await getCurrentLocation();
        } catch (e) {
          errorEl.textContent = "We couldn't get your location. Try choosing a building instead.";
          errorEl.hidden = false;
          return;
        }
      } else {
        state.originValue = originInput.value.trim();
        state.originCoords = null;
      }
      goToStep(3);
    },
  });

  applyMode();
  announce("Starting from. Use your location, or choose a building.");
}

// ---------------------------------------------------------------------------
// Step 3: chips
// ---------------------------------------------------------------------------

function renderStep3() {
  const content = document.getElementById("step-content");
  content.innerHTML = "";
  content.appendChild(document.getElementById("tpl-step-chips").content.cloneNode(true));

  const usualBtn = document.getElementById("use-usual-prefs");
  const saved = loadPreferences();
  if (saved) {
    usualBtn.hidden = false;
    usualBtn.addEventListener("click", () => {
      state.chips = saved;
      renderStep3();
    });
  }

  content.querySelectorAll(".pill-chip").forEach((chip) => {
    const id = chip.dataset.chip;
    chip.setAttribute("aria-pressed", String(state.chips[id].selected));
    chip.addEventListener("click", () => {
      state.chips[id].selected = !state.chips[id].selected;
      chip.setAttribute("aria-pressed", String(state.chips[id].selected));
    });
  });

  renderStepActions(content, {
    showBack: true,
    showSkip: true,
    continueLabel: "Continue",
    onBack: () => goToStep(2),
    onSkip: () => {
      CHIP_IDS.forEach((id) => (state.chips[id].selected = false));
      goToStep(4);
    },
    onContinue: () => goToStep(4),
  });

  announce("Anything we should avoid? Select any that apply, or skip.");
}

// ---------------------------------------------------------------------------
// Step 4: importance
// ---------------------------------------------------------------------------

function renderStep4() {
  const content = document.getElementById("step-content");
  content.innerHTML = "";
  content.appendChild(document.getElementById("tpl-step-importance").content.cloneNode(true));

  const list = document.getElementById("importance-list");
  const selectedChips = CHIP_IDS.filter((id) => state.chips[id].selected);

  if (selectedChips.length === 0) {
    const p = document.createElement("p");
    p.className = "field-hint";
    p.textContent = "No preferences selected -- we'll show the fastest route.";
    list.appendChild(p);
  }

  selectedChips.forEach((id) => {
    const row = document.getElementById("tpl-importance-row").content.cloneNode(true);
    row.querySelector(".importance-chip-label").textContent = CHIP_LABEL[id];
    const radios = row.querySelectorAll('input[type="radio"]');
    radios.forEach((radio) => {
      radio.name = `importance-${id}`;
      radio.checked = radio.value === state.chips[id].level;
      radio.addEventListener("change", () => {
        if (radio.checked) state.chips[id].level = radio.value;
      });
    });
    list.appendChild(row);
  });

  renderStepActions(content, {
    showBack: true,
    continuePrimary: true,
    continueLabel: "Find my route",
    onBack: () => goToStep(3),
    onContinue: () => {
      savePreferences(state.chips);
      findRoute();
    },
  });

  announce("How much does each one matter?");
}

// ---------------------------------------------------------------------------
// Loading / Error
// ---------------------------------------------------------------------------

function renderLoading() {
  const content = document.getElementById("step-content");
  content.innerHTML = "";
  content.appendChild(document.getElementById("tpl-loading").content.cloneNode(true));
  announce("Finding a route that works for you");
}

function renderError(message, onRetry) {
  const content = document.getElementById("step-content");
  content.innerHTML = "";
  content.appendChild(document.getElementById("tpl-error").content.cloneNode(true));
  document.getElementById("error-message").textContent = message;
  document.getElementById("error-retry-btn").addEventListener("click", onRetry);
  announce(message);
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

function priorities() {
  const out = {};
  CHIP_IDS.forEach((id) => {
    if (state.chips[id].selected) out[id] = state.chips[id].level;
  });
  return out;
}

function destinationLocation() {
  return { building: state.destination };
}

function originLocation() {
  if (state.originMode === "current" && state.originCoords) return state.originCoords;
  return { building: state.originValue };
}

async function findRoute() {
  state.step = "loading";
  renderStepProgress();
  renderLoading();

  const origin = originLocation();
  const destination = destinationLocation();
  const prefs = priorities();

  try {
    const [recommended, fastest, stepfree] = await Promise.all([
      postRoute(buildRequestBody("recommended", origin, destination, prefs)),
      postRoute(buildRequestBody("fastest", origin, destination, prefs)),
      postRoute(buildRequestBody("stepfree", origin, destination, prefs)),
    ]);
    state.routes = { recommended, fastest, stepfree };
    state.selectedCompare = "recommended";
    state.step = "results";
    renderStepProgress();
    renderResults();
  } catch (e) {
    state.step = "error";
    renderStepProgress();
    let message = "Something went wrong finding a route. Please try again.";
    let retryStep = 1;
    if (e instanceof LocationError) {
      if (/m from the nearest path node/.test(e.message)) {
        message = "You're outside the mapped area. Choose a starting building.";
        retryStep = 2;
      } else {
        message = "We couldn't find that building. Try another name.";
      }
    }
    renderError(message, () => goToStep(retryStep));
  }
}

function destinationEntranceText(stats) {
  const entrance = stats.destination_entrance;
  if (!entrance) return "Unknown";
  const base =
    entrance.access === "accessible"
      ? "Accessible"
      : entrance.access === "not_accessible"
      ? "Not accessible"
      : "Unverified";
  return entrance.auto_opener ? `${base}, automatic door` : base;
}

function renderResults() {
  const content = document.getElementById("step-content");
  content.innerHTML = "";
  content.appendChild(document.getElementById("tpl-results").content.cloneNode(true));

  const compareRadios = content.querySelectorAll('input[name="compare"]');
  compareRadios.forEach((radio) => {
    radio.checked = radio.value === state.selectedCompare;
    radio.addEventListener("change", () => {
      if (radio.checked) {
        state.selectedCompare = radio.value;
        renderSelectedRoute();
      }
    });
  });

  document.getElementById("show-details-btn").addEventListener("click", (ev) => {
    const btn = ev.currentTarget;
    const details = document.getElementById("explanation-details");
    const expanded = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!expanded));
    details.hidden = expanded;
    btn.textContent = expanded ? "More details" : "Show less";
  });

  document.getElementById("edit-preferences-btn").addEventListener("click", () => goToStep(3));
  document.getElementById("new-destination-btn").addEventListener("click", () => {
    state.destination = "";
    state.routes = null;
    clearRoutes(map, mapState);
    goToStep(1);
  });

  renderSelectedRoute();
  routeShownCallbacks.forEach((cb) => {
    try {
      cb(state.routes, state.selectedCompare);
    } catch (e) {
      // P17 stub errors must never break the core flow.
    }
  });
}

function renderSelectedRoute() {
  const key = state.selectedCompare;
  const response = state.routes[key];
  const route = response.routes[0];
  const stats = route.stats;

  document.getElementById("headline-minutes").textContent = stats.est_time_min.toFixed(1);
  document.getElementById("headline-distance").textContent = `${Math.round(stats.distance_ft)} ft`;

  const statGrid = document.getElementById("summary-stats");
  statGrid.innerHTML = "";
  const estimated = (stats.estimated_segments || 0) > 0;
  const rows = [
    ["Stairs avoided", `${stats.steps_avoided}`],
    ["Max slope", `${stats.max_slope_pct.toFixed(1)}%${estimated ? ' <span class="estimated-tag">estimated</span>' : ""}`],
    ["Well-lit", `${stats.pct_lit.toFixed(0)}%`],
    ["Entrance", destinationEntranceText(stats)],
  ];
  rows.forEach(([label, html]) => {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.innerHTML = html;
    statGrid.append(dt, dd);
  });

  const shortList = document.getElementById("explanation-list-short");
  const fullList = document.getElementById("explanation-list-full");
  const segmentList = document.getElementById("segment-list");
  shortList.innerHTML = "";
  fullList.innerHTML = "";
  segmentList.innerHTML = "";
  (route.explanation || []).slice(0, 3).forEach((line) => {
    const li = document.createElement("li");
    li.textContent = line;
    shortList.appendChild(li);
  });
  (route.explanation || []).forEach((line) => {
    const li = document.createElement("li");
    li.textContent = line;
    fullList.appendChild(li);
  });
  (route.segments || []).forEach((seg) => {
    const li = document.createElement("li");
    const dateLabel = seg.verified && seg.date_surveyed ? `surveyed ${seg.date_surveyed}` : "estimated";
    li.textContent = `${seg.edge_id}: ${dateLabel}`;
    segmentList.appendChild(li);
  });
  document.getElementById("explanation-details").hidden = true;
  document.getElementById("show-details-btn").setAttribute("aria-expanded", "false");
  document.getElementById("show-details-btn").textContent = "More details";

  renderNoPerfectRoute(response, route);
  renderCompareStats();
  drawRoutes(map, mapState, state.routes, key);

  announce(
    `Recommended for you: ${stats.est_time_min.toFixed(1)} minutes, ${Math.round(stats.distance_ft)} feet, ` +
      `${stats.steps_avoided} stairs avoided.`
  );
}

function renderCompareStats() {
  const grid = document.getElementById("compare-stats");
  grid.innerHTML = "";
  const labels = { recommended: "Recommended", fastest: "Fastest", stepfree: "Step-free" };
  ["recommended", "fastest", "stepfree"].forEach((key) => {
    const route = state.routes[key].routes[0];
    const div = document.createElement("div");
    const dt = document.createElement("dt");
    dt.textContent = labels[key];
    const dd = document.createElement("dd");
    dd.textContent = `${route.stats.est_time_min.toFixed(1)} min, ${route.stats.steps_avoided === 0 ? "uses stairs" : "0 stairs"}`;
    div.append(dt, dd);
    grid.appendChild(div);
  });
}

function renderNoPerfectRoute(fullResponse, route) {
  const banner = document.getElementById("no-perfect-route-banner");
  const list = document.getElementById("compromise-list");
  list.innerHTML = "";

  const compromises = [];
  (route.violations || []).forEach((v) => {
    const chipId = VIOLATION_CODE_TO_CHIP[v.code];
    const label = chipId ? CHIP_LABEL[chipId] : "A route standard";
    const isEssential = chipId && state.chips[chipId].selected && state.chips[chipId].level === "essential";
    compromises.push({ text: `${label}: ${v.message}`, essential: isEssential });
  });
  (fullResponse.warnings || []).forEach((w) => {
    if (w.includes("accessible entrance")) {
      compromises.push({
        text: `Accessible entrances: ${w}`,
        essential: state.chips.accessible_entrance.selected && state.chips.accessible_entrance.level === "essential",
      });
    } else if (w.includes("distance tolerance")) {
      compromises.push({ text: w, essential: false });
    }
  });

  if (compromises.length === 0) {
    banner.hidden = true;
    return;
  }
  compromises.sort((a, b) => (b.essential ? 1 : 0) - (a.essential ? 1 : 0));
  compromises.forEach((c) => {
    const li = document.createElement("li");
    li.textContent = c.text;
    if (c.essential) li.style.fontWeight = "700";
    list.appendChild(li);
  });
  banner.hidden = false;
}

// ---------------------------------------------------------------------------
// Step router
// ---------------------------------------------------------------------------

function goToStep(n) {
  state.step = n;
  renderStepProgress();
  const renderers = { 1: renderStep1, 2: renderStep2, 3: renderStep3, 4: renderStep4 };
  renderers[n]();
}

// ---------------------------------------------------------------------------
// Text size + bottom-sheet expand button
// ---------------------------------------------------------------------------

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

function wireSheetExpand() {
  const states = ["peek", "half", "full"];
  let index = 1; // start at "half"
  const panel = document.getElementById("panel");
  panel.dataset.sheetState = states[index];
  document.getElementById("sheet-expand-btn").addEventListener("click", () => {
    index = (index + 1) % states.length;
    panel.dataset.sheetState = states[index];
  });
}

// ---------------------------------------------------------------------------
// Buildings typeahead data
// ---------------------------------------------------------------------------

async function loadBuildingsList() {
  state.buildings = await fetchBuildings();
  const datalist = document.getElementById("buildings-list");
  datalist.innerHTML = "";
  state.buildings.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    datalist.appendChild(option);
  });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  map = initMap("map");
  wireTextSize();
  wireSheetExpand();
  await loadBuildingsList();
  goToStep(1);

  initAuth(ctx);
  initLive(ctx);
  initFacilities(ctx);
  initAi(ctx);
  initLayers(ctx);
}

document.addEventListener("DOMContentLoaded", init);
