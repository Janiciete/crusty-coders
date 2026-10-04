// Way2Go map layers (S4 L1): optional, quiet overlays on top of the hero
// route -- accessible entrances, steep slopes, dark segments, step-free
// paths, and reports. All off by default; the route stays the visual
// focus (thin strokes, small markers, muted tones only -- never the
// route's blush/peach gradient).
//
// This module injects its own stylesheet at runtime (css/layers.css) so
// index.html never needs editing, and plugs into app.js the same way the
// P16/P17 stub modules do: one `init(ctx)` call.

const CONFIG = window.WAY2GO_CONFIG || { apiBase: "http://localhost:8000" };

const LAYER_DEFS = [
  { id: "accessible_entrances", icon: "♿", label: "Accessible entrances" },
  { id: "slopes", icon: "↗", label: "Steep slopes (dashed = estimated)" },
  { id: "dark_segments", icon: "\u{1F319}", label: "Dark segments (hatched)" },
  { id: "step_free_paths", icon: "⟿", label: "Step-free paths" },
  { id: "reports", icon: "⚠", label: "Reports" },
];

function ensureStylesheet() {
  if (document.getElementById("layers-stylesheet")) return;
  const link = document.createElement("link");
  link.id = "layers-stylesheet";
  link.rel = "stylesheet";
  link.href = "css/layers.css";
  document.head.appendChild(link);
}

// ---------------------------------------------------------------------------
// Leaflet rendering: thin strokes, small markers, muted tones only.
// ---------------------------------------------------------------------------

function entrancesLayer(geojson) {
  return L.geoJSON(geojson, {
    pointToLayer: (feature, latlng) =>
      L.circleMarker(latlng, {
        radius: 5,
        color: "#2E2629",
        weight: 1,
        opacity: 0.7,
        fillColor: "#2E2629",
        fillOpacity: 0.35,
      }).bindTooltip(
        `Accessible entrance${feature.properties.auto_opener ? " (automatic door)" : ""} -- ${
          feature.properties.building || "unknown building"
        }`
      ),
  });
}

function slopesLayer(geojson) {
  return L.geoJSON(geojson, {
    style: (feature) => ({
      color: "#A8445A",
      weight: 2,
      opacity: 0.35,
      dashArray: feature.properties.estimated ? "6, 4" : null,
    }),
    onEachFeature: (feature, layer) => {
      const estimated = feature.properties.estimated ? " (estimated from lidar)" : "";
      layer.bindTooltip(`${feature.properties.slope_pct.toFixed(1)}% slope${estimated}`);
    },
  });
}

function darkSegmentsLayer(geojson) {
  return L.geoJSON(geojson, {
    style: () => ({
      color: "#2E2629",
      weight: 3,
      opacity: 0.45,
      dashArray: "2, 5", // a light hatch-like tick pattern, not a solid wash
    }),
    onEachFeature: (feature, layer) => {
      layer.bindTooltip(`Dark segment (${feature.properties.lit_per_100ft.toFixed(0)} lit/100ft)`);
    },
  });
}

function stepFreeLayer(geojson) {
  return L.geoJSON(geojson, {
    style: () => ({
      color: "#2E2629",
      weight: 2,
      opacity: 0.25,
    }),
  });
}

function reportsLayer(geojson) {
  return L.geoJSON(geojson, {
    pointToLayer: (feature, latlng) => {
      const historical = Boolean(feature.properties.historical);
      const icon = L.divIcon({
        className: "layers-report-marker",
        html: `<span class="layers-report-icon${historical ? " is-historical" : ""}">&#9888;</span>`,
        iconSize: [20, 20],
        iconAnchor: [10, 10],
      });
      const marker = L.marker(latlng, { icon, keyboard: false });
      const label = historical
        ? "Historical (Cornell inventory)"
        : `${feature.properties.report_type || "report"} -- ${feature.properties.status || ""}`;
      marker.bindTooltip(label);
      return marker;
    },
  });
}

const LAYER_RENDERERS = {
  accessible_entrances: entrancesLayer,
  slopes: slopesLayer,
  dark_segments: darkSegmentsLayer,
  step_free_paths: stepFreeLayer,
  reports: reportsLayer,
};

// ---------------------------------------------------------------------------
// Dialog
// ---------------------------------------------------------------------------

function buildDialog(onToggle) {
  const dialog = document.createElement("dialog");
  dialog.id = "layers-dialog";
  dialog.className = "layers-dialog";
  dialog.setAttribute("aria-label", "Map layers");

  const heading = document.createElement("h2");
  heading.className = "layers-dialog-heading";
  heading.textContent = "Map layers";
  dialog.appendChild(heading);

  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "layers-dialog-close";
  closeBtn.setAttribute("aria-label", "Close map layers");
  closeBtn.textContent = "×";
  closeBtn.addEventListener("click", () => dialog.close());
  dialog.appendChild(closeBtn);

  const list = document.createElement("div");
  list.className = "layers-list";

  LAYER_DEFS.forEach((def) => {
    const row = document.createElement("label");
    row.className = "layers-row";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "layers-checkbox";
    checkbox.dataset.layerId = def.id;

    const icon = document.createElement("span");
    icon.className = "layers-row-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = def.icon;

    const text = document.createElement("span");
    text.className = "layers-row-text";
    text.textContent = def.label;

    const status = document.createElement("span");
    status.className = "layers-row-status";
    status.dataset.statusFor = def.id;
    status.setAttribute("role", "status");

    row.append(checkbox, icon, text, status);
    checkbox.addEventListener("change", () => onToggle(def.id, checkbox, status));
    list.appendChild(row);
  });

  dialog.appendChild(list);
  return dialog;
}

// ---------------------------------------------------------------------------
// init
// ---------------------------------------------------------------------------

export function init(ctx) {
  ensureStylesheet();

  const cache = {}; // layer id -> geojson (fetched once, reused on re-toggle)
  const activeGroups = {}; // layer id -> L.Layer currently on the map

  function onToggle(id, checkbox, status) {
    const map = ctx.map;
    if (!checkbox.checked) {
      if (activeGroups[id] && map) {
        map.removeLayer(activeGroups[id]);
        delete activeGroups[id];
      }
      status.textContent = "";
      return;
    }

    if (cache[id]) {
      const renderer = LAYER_RENDERERS[id];
      activeGroups[id] = renderer(cache[id]).addTo(map);
      status.textContent = "";
      return;
    }

    status.textContent = "Loading…";
    fetch(`${CONFIG.apiBase}/layers/${id}`)
      .then((res) => {
        if (!res.ok) throw new Error(`layer fetch failed (${res.status})`);
        return res.json();
      })
      .then((geojson) => {
        cache[id] = geojson;
        if (!checkbox.checked) return; // user toggled off while loading
        const renderer = LAYER_RENDERERS[id];
        activeGroups[id] = renderer(geojson).addTo(map);
        status.textContent = "";
      })
      .catch(() => {
        checkbox.checked = false;
        status.textContent = "Layer unavailable";
      });
  }

  const btn = document.createElement("button");
  btn.type = "button";
  btn.id = "layers-toggle-btn";
  btn.className = "layers-toggle-btn";
  btn.setAttribute("aria-label", "Map layers");
  btn.setAttribute("aria-haspopup", "dialog");
  btn.textContent = "⧉"; // squared-outline-ish glyph standing in for a "layers" icon

  const dialog = buildDialog(onToggle);

  dialog.addEventListener("close", () => {
    btn.focus();
  });

  document.body.appendChild(btn);
  document.body.appendChild(dialog);

  btn.addEventListener("click", () => {
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    const firstCheckbox = dialog.querySelector(".layers-checkbox");
    if (firstCheckbox) firstCheckbox.focus();
  });
}
