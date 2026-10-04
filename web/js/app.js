// Way2Go app (P15/Prompt B): guided step flow + map-first results, built on
// top of F1's live `priorities` support and P14a's working API-calling
// logic (see js/api.js).
//
// Prompt B changes: destination/origin are now a building-only picker (no
// free text, no geolocation), a persistent top-left Back button replaces
// the old per-step bottom Back buttons, and results show a calm
// "doesn't fully fit" banner driven by `fit` (falling back to `violations`
// for older API responses).

import { fetchBuildings, postRoute, buildRequestBody, LocationError } from "./api.js";
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

// Popular building shortcuts shown at the top of every picker, filtered
// down to whichever of these actually exist in GET /buildings.
const POPULAR_BUILDING_NAMES = ["Goldwin Smith Hall", "Keeton House", "Olin Library"];

const state = {
  step: 1, // 1..4, "loading", "results", "error", or "results"
  buildings: [],
  destination: "",
  originValue: "",
  chips: Object.fromEntries(CHIP_IDS.map((id) => [id, { selected: false, level: "essential" }])),
  routes: null, // { recommended, fastest, stepfree }
  selectedCompare: "recommended",
};

const mapState = { layers: [], onZoom: null };
let map = null;
const routeShownCallbacks = [];

// Tracks whether any building picker is currently open, so the global Esc
// handler can leave Esc-closes-picker to the picker itself instead of also
// triggering Back.
let openPickerCount = 0;

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
// Building picker (WAI-ARIA listbox popup pattern)
// ---------------------------------------------------------------------------

function buildPickerGroups(buildings, excludeName) {
  const pool = buildings.filter((name) => name !== excludeName);
  const groups = [];

  const popular = POPULAR_BUILDING_NAMES.filter((name) => pool.includes(name));
  if (popular.length) {
    groups.push({ header: "Popular", items: popular });
  }

  const alphabetical = [...pool].sort((a, b) => a.localeCompare(b));
  let currentGroup = null;
  alphabetical.forEach((name) => {
    const letter = name.charAt(0).toUpperCase();
    if (!currentGroup || currentGroup.header !== letter) {
      currentGroup = { header: letter, items: [] };
      groups.push(currentGroup);
    }
    currentGroup.items.push(name);
  });

  return groups;
}

// Creates one building picker bound to a trigger button + inline panel.
// `getGroups()` and `getValue()` are called fresh every time the picker
// opens/re-renders, so callers can keep them reactive to app state (e.g.
// excluding the chosen destination from the origin picker).
function createPicker({ trigger, triggerText, panel, listboxEl, getGroups, getValue, onSelect, placeholder }) {
  let open = false;
  let flatItems = []; // [{ name, el }]
  let activeIndex = -1;
  let typeaheadBuffer = "";
  let typeaheadTimer = null;

  function renderList() {
    listboxEl.innerHTML = "";
    flatItems = [];
    const groups = getGroups();
    const selected = getValue();

    groups.forEach((group) => {
      const header = document.createElement("li");
      header.className = "picker-group-header";
      header.textContent = group.header;
      header.setAttribute("role", "presentation");
      listboxEl.appendChild(header);

      group.items.forEach((name) => {
        const li = document.createElement("li");
        li.className = "picker-row";
        li.id = `${listboxEl.id}-opt-${flatItems.length}`;
        li.setAttribute("role", "option");
        const isSelected = name === selected;
        li.setAttribute("aria-selected", String(isSelected));

        const check = document.createElement("span");
        check.className = "picker-row-check";
        check.setAttribute("aria-hidden", "true");
        check.textContent = isSelected ? "✓" : "";

        const label = document.createElement("span");
        label.className = "picker-row-label";
        label.textContent = name;

        li.append(check, label);
        li.addEventListener("click", () => selectItem(name));
        listboxEl.appendChild(li);
        flatItems.push({ name, el: li });
      });
    });

    const selectedIndex = flatItems.findIndex((item) => item.name === selected);
    setActive(selectedIndex >= 0 ? selectedIndex : flatItems.length ? 0 : -1, { scroll: false });
  }

  function setActive(index, { scroll = true } = {}) {
    if (activeIndex >= 0 && flatItems[activeIndex]) {
      flatItems[activeIndex].el.classList.remove("is-active");
    }
    activeIndex = index;
    if (activeIndex >= 0 && flatItems[activeIndex]) {
      const el = flatItems[activeIndex].el;
      el.classList.add("is-active");
      listboxEl.setAttribute("aria-activedescendant", el.id);
      if (scroll) el.scrollIntoView({ block: "nearest" });
    } else {
      listboxEl.removeAttribute("aria-activedescendant");
    }
  }

  function selectItem(name) {
    onSelect(name);
    close({ focusTrigger: true });
  }

  function handleOutsideClick(ev) {
    if (trigger.contains(ev.target) || panel.contains(ev.target)) return;
    close();
  }

  function openPicker() {
    if (open) return;
    open = true;
    openPickerCount += 1;
    renderList();
    trigger.setAttribute("aria-expanded", "true");
    panel.classList.add("is-open");
    panel.setAttribute("aria-hidden", "false");
    listboxEl.focus();
    document.addEventListener("click", handleOutsideClick);
  }

  function close({ focusTrigger = false } = {}) {
    if (!open) return;
    open = false;
    openPickerCount = Math.max(0, openPickerCount - 1);
    trigger.setAttribute("aria-expanded", "false");
    panel.classList.remove("is-open");
    panel.setAttribute("aria-hidden", "true");
    document.removeEventListener("click", handleOutsideClick);
    if (focusTrigger) trigger.focus();
  }

  function toggle() {
    if (open) close({ focusTrigger: true });
    else openPicker();
  }

  function typeahead(char) {
    clearTimeout(typeaheadTimer);
    typeaheadBuffer += char.toLowerCase();
    const match = flatItems.findIndex((item) => item.name.toLowerCase().startsWith(typeaheadBuffer));
    if (match >= 0) setActive(match);
    typeaheadTimer = setTimeout(() => {
      typeaheadBuffer = "";
    }, 600);
  }

  trigger.addEventListener("click", toggle);
  trigger.addEventListener("keydown", (ev) => {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(ev.key)) {
      ev.preventDefault();
      openPicker();
    }
  });

  listboxEl.addEventListener("keydown", (ev) => {
    switch (ev.key) {
      case "ArrowDown":
        ev.preventDefault();
        if (flatItems.length) setActive(Math.min(activeIndex + 1, flatItems.length - 1));
        break;
      case "ArrowUp":
        ev.preventDefault();
        if (flatItems.length) setActive(Math.max(activeIndex - 1, 0));
        break;
      case "Home":
        ev.preventDefault();
        if (flatItems.length) setActive(0);
        break;
      case "End":
        ev.preventDefault();
        if (flatItems.length) setActive(flatItems.length - 1);
        break;
      case "Enter":
        ev.preventDefault();
        if (activeIndex >= 0 && flatItems[activeIndex]) selectItem(flatItems[activeIndex].name);
        break;
      case "Escape":
        ev.preventDefault();
        ev.stopPropagation();
        close({ focusTrigger: true });
        break;
      case "Tab":
        close();
        break;
      default:
        if (ev.key.length === 1 && /\S/.test(ev.key)) {
          typeahead(ev.key);
        }
        break;
    }
  });

  function setDisplay(name) {
    triggerText.textContent = name || placeholder;
    triggerText.classList.toggle("is-placeholder", !name);
  }

  return { open: openPicker, close, toggle, setDisplay, renderList };
}

// ---------------------------------------------------------------------------
// Top-left Back button
// ---------------------------------------------------------------------------

function updateBackButton() {
  const btn = document.getElementById("back-btn");
  if (!btn) return;
  const showable = (typeof state.step === "number" && state.step > 1) || state.step === "results";
  btn.hidden = !showable;
}

function focusStepHeading() {
  const heading = document.getElementById("step-heading");
  if (heading) heading.focus();
}

function goBack() {
  let target = null;
  if (state.step === 2) target = 1;
  else if (state.step === 3) target = 2;
  else if (state.step === 4) target = 3;
  else if (state.step === "results") target = 3; // keeps chip/importance choices
  if (target === null) return;
  goToStep(target, { focusHeading: true });
}

document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  if (openPickerCount > 0) return; // the open picker handles its own Escape
  const btn = document.getElementById("back-btn");
  if (btn && !btn.hidden) goBack();
});

// ---------------------------------------------------------------------------
// Step progress dots
// ---------------------------------------------------------------------------

function renderStepProgress() {
  const progress = document.getElementById("step-progress");
  const isStepScreen = typeof state.step === "number";
  progress.hidden = !isStepScreen;
  updateBackButton();
  if (!isStepScreen) return;
  progress.querySelectorAll(".progress-dot").forEach((dot) => {
    const n = Number(dot.dataset.step);
    dot.classList.toggle("is-complete", n < state.step);
    dot.classList.toggle("is-current", n === state.step);
  });
}

// ---------------------------------------------------------------------------
// Step-action buttons (shared layout; content differs per step). Back now
// lives in the persistent top-left button, so this is just Continue/Skip.
// ---------------------------------------------------------------------------

function renderStepActions(container, { showSkip, continueLabel, onSkip, onContinue, continueDisabled, continuePrimary }) {
  const actions = document.createElement("div");
  actions.className = "step-actions";

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
  cont.className = continuePrimary ? "primary-button" : "choice-button";
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

function renderStep1(options = {}) {
  const content = document.getElementById("step-content");
  content.innerHTML = "";
  content.appendChild(document.getElementById("tpl-step-destination").content.cloneNode(true));

  const trigger = document.getElementById("destination-picker-trigger");
  const triggerText = document.getElementById("destination-picker-text");
  const panel = document.getElementById("destination-picker-panel");
  const listboxEl = document.getElementById("destination-picker-listbox");
  // auth.js's saved-places "fillDestination" sets this element's `.value`
  // and dispatches an "input" event on it; kept for that contract even
  // though it's no longer a visible text field.
  const hiddenInput = document.getElementById("destination-input");

  let continueBtn;

  function applySelection(name) {
    state.destination = name || "";
    picker.setDisplay(state.destination);
    hiddenInput.value = state.destination;
    if (continueBtn) continueBtn.disabled = !state.destination;
  }

  const picker = createPicker({
    trigger,
    triggerText,
    panel,
    listboxEl,
    placeholder: "Choose a building",
    getGroups: () => buildPickerGroups(state.buildings, null),
    getValue: () => state.destination,
    onSelect: (name) => applySelection(name),
  });

  applySelection(state.destination);

  hiddenInput.addEventListener("input", () => {
    const name = hiddenInput.value;
    if (name && state.buildings.includes(name)) {
      applySelection(name);
    }
  });

  continueBtn = renderStepActions(content, {
    continueLabel: "Continue",
    continueDisabled: !state.destination,
    onContinue: () => goToStep(2),
  });

  if (options.focusHeading) {
    focusStepHeading();
  } else {
    trigger.focus();
  }
  announce("Where to? Choose a building.");
}

// ---------------------------------------------------------------------------
// Step 2: origin
// ---------------------------------------------------------------------------

function renderStep2(options = {}) {
  const content = document.getElementById("step-content");
  content.innerHTML = "";
  content.appendChild(document.getElementById("tpl-step-origin").content.cloneNode(true));

  const trigger = document.getElementById("origin-picker-trigger");
  const triggerText = document.getElementById("origin-picker-text");
  const panel = document.getElementById("origin-picker-panel");
  const listboxEl = document.getElementById("origin-picker-listbox");
  const errorEl = document.getElementById("origin-error");

  let continueBtn;

  function applySelection(name) {
    state.originValue = name || "";
    picker.setDisplay(state.originValue);
    if (continueBtn) continueBtn.disabled = !state.originValue;
  }

  const picker = createPicker({
    trigger,
    triggerText,
    panel,
    listboxEl,
    placeholder: "Choose a building",
    getGroups: () => buildPickerGroups(state.buildings, state.destination),
    getValue: () => state.originValue,
    onSelect: (name) => applySelection(name),
  });

  // The origin picker excludes the chosen destination; if a previously
  // selected origin happens to equal it (stale state), clear it.
  if (state.originValue === state.destination) {
    state.originValue = "";
  }
  applySelection(state.originValue);

  continueBtn = renderStepActions(content, {
    continueLabel: "Continue",
    continueDisabled: !state.originValue,
    onContinue: () => {
      errorEl.hidden = true;
      goToStep(3);
    },
  });

  if (options.focusHeading) {
    focusStepHeading();
  } else {
    trigger.focus();
  }
  announce("Starting from. Choose a building.");
}

// ---------------------------------------------------------------------------
// Step 3: chips
// ---------------------------------------------------------------------------

function renderStep3(options = {}) {
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
    showSkip: true,
    continueLabel: "Continue",
    onSkip: () => {
      CHIP_IDS.forEach((id) => (state.chips[id].selected = false));
      goToStep(4);
    },
    onContinue: () => goToStep(4),
  });

  if (options.focusHeading) focusStepHeading();
  announce("Anything we should avoid? Select any that apply, or skip.");
}

// ---------------------------------------------------------------------------
// Step 4: importance
// ---------------------------------------------------------------------------

function renderStep4(options = {}) {
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
    continuePrimary: true,
    continueLabel: "Find my route",
    onContinue: () => {
      savePreferences(state.chips);
      findRoute();
    },
  });

  if (options.focusHeading) focusStepHeading();
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
    // A successful response -- even one describing a route that doesn't
    // fully fit the user's preferences (`fit.status === "partial"`, or
    // legacy `violations`) -- is still a normal result, never routed to
    // the error screen. Only network/server failures land in the catch
    // block below.
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
    if (e instanceof LocationError) {
      message = "We couldn't find that building. Try choosing another one.";
    }
    renderError(message, () => goToStep(1));
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

  const fitInfo = renderFitBanner(response, route);
  renderCompareStats();
  drawRoutes(map, mapState, state.routes, key);

  let announceText =
    `Recommended for you: ${stats.est_time_min.toFixed(1)} minutes, ${Math.round(stats.distance_ft)} feet, ` +
    `${stats.steps_avoided} stairs avoided.`;
  if (fitInfo.isPartial) {
    announceText += ` This route doesn't fully fit your preferences: ${fitInfo.reasons.join(". ")}`;
  }
  announce(announceText);
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

// Builds the "doesn't fully fit" banner for the currently displayed route.
// Prefers the live `fit` field (`{status: "ok"|"partial", reasons: [...]}`);
// falls back to the older `violations`/`warnings` shape when `fit` is
// absent, so this keeps working against an API that hasn't landed the
// `fit` field yet. Returns { isPartial, reasons } for the caller's
// aria-live announcement.
function renderFitBanner(fullResponse, route) {
  const banner = document.getElementById("fit-banner");
  const list = document.getElementById("fit-reasons-list");
  list.innerHTML = "";

  let isPartial = false;
  let reasons = [];

  if (route.fit && typeof route.fit.status === "string") {
    isPartial = route.fit.status === "partial";
    reasons = Array.isArray(route.fit.reasons) ? route.fit.reasons : [];
  } else {
    // Fallback for an API response with no `fit` field: build the same
    // banner out of `violations` (hard-limit misses) and the subset of
    // `warnings` that describe a compromise (entrance access, distance
    // tolerance), same logic the earlier "no perfect route" banner used.
    (route.violations || []).forEach((v) => {
      const chipId = VIOLATION_CODE_TO_CHIP[v.code];
      const label = chipId ? CHIP_LABEL[chipId] : "A route standard";
      reasons.push(`${label}: ${v.message}`);
    });
    (fullResponse.warnings || []).forEach((w) => {
      if (w.includes("accessible entrance") || w.includes("distance tolerance")) {
        reasons.push(w);
      }
    });
    isPartial = reasons.length > 0;
  }

  if (!isPartial || reasons.length === 0) {
    banner.hidden = true;
    return { isPartial: false, reasons: [] };
  }

  reasons.forEach((reason) => {
    const li = document.createElement("li");
    li.textContent = reason;
    list.appendChild(li);
  });
  banner.hidden = false;
  return { isPartial: true, reasons };
}

// ---------------------------------------------------------------------------
// Step router
// ---------------------------------------------------------------------------

function goToStep(n, options = {}) {
  state.step = n;
  renderStepProgress();
  const renderers = { 1: renderStep1, 2: renderStep2, 3: renderStep3, 4: renderStep4 };
  renderers[n](options);
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

function wireBackButton() {
  const btn = document.getElementById("back-btn");
  btn.addEventListener("click", goBack);
}

// ---------------------------------------------------------------------------
// Buildings data (no more <datalist> -- the picker reads state.buildings
// directly)
// ---------------------------------------------------------------------------

async function loadBuildingsList() {
  state.buildings = await fetchBuildings();
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  map = initMap("map");
  wireTextSize();
  wireSheetExpand();
  wireBackButton();
  await loadBuildingsList();
  goToStep(1);

  initAuth(ctx);
  initLive(ctx);
  initFacilities(ctx);
  initAi(ctx);
  initLayers(ctx);
}

document.addEventListener("DOMContentLoaded", init);
