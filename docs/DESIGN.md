# Way2Go design (P15)

## Shared design spec (as given)

**Tokens**

| Token | Hex | Use |
|---|---|---|
| `--cream` | `#FFF9EC` | Panels and sheets (the map is the page background) |
| `--blush` | `#FBB1BD` | Selected states, primary button gradient start, focus halo |
| `--peach` | `#FFD9C2` | Gradient end, route halo end, selected chip fill (light) |
| `--rosewood` | `#A8445A` | Route center line, links, selected text/icons, focus ring |
| `--ink` | `#2E2629` | All body text (warm near-black) |
| `--mist` | `#EDE5D8` | Dividers, unselected chip border, skeletons |

Gradient: `linear-gradient(135deg, var(--blush), var(--peach))`, used only on the primary button, the route halo, and progress dots -- never as a page/panel wash. Text is always `--ink` or `--rosewood`, never blush text on cream.

**Type (updated S1):** Two families, both Google Fonts, loaded in one request. `--font-display` (Bricolage Grotesque, falling back to Atkinson Hyperlegible Next then `system-ui`) is used for headings (`h1`/`h2`/`h3`), the step question, the panel/card titles ("Why this route?", "Recommended for you"), and the route's minutes (`.headline-number`). `--font-body` (Atkinson Hyperlegible Next, falling back to "Atkinson Hyperlegible" then `system-ui`) is everything else -- buttons, chips, inputs, hints, stats. Both stay sentence case; no letter-spacing tricks on display text. Scale is now a token set on `:root` instead of mixed px/rem:

| Token | Size | Use |
|---|---|---|
| `--fs-xs` | 14px (0.875rem) | floor -- estimated-tag, segmented-control labels, stat labels |
| `--fs-sm` | 15px (0.9375rem) | body default, field hints, compare-stat values |
| `--fs-base` | 16px (1rem) | buttons, chips, inputs, importance labels |
| `--fs-lg` | 17px (1.0625rem) | card titles, headline label, map-control icon glyphs |
| `--fs-xl` | 21px (1.3125rem) | reserved -- no current element sits at this step in the scale |
| `--fs-display` | 28px (1.75rem), weight 700 | step question, route minutes, results title |

Weights stay 400/600/700, body line-height 1.5. `--font-scale` on `:root` (via `html { font-size: calc(100% * var(--font-scale)) }`) still drives the A−/A+ control; every size above is in `rem` so it scales with it.

**Sizing (new, S1):** one height token per control family -- `--h-btn` (48px, primary and secondary buttons), `--h-chip` (48px, pill chips and segmented-control rows), `--h-input` (48px, text inputs), `--size-icon-btn` (44px, map controls and the A−/A+ buttons). All exceed the 44px tap-target minimum. `--sp-1..5` (0.25/0.5/1/1.5/2.5rem, unchanged from P15) remain the fine-grained internal rhythm (gaps, stack margins); `--sp-6` (1.5rem) is the one outer gutter the left panel's padding uses, so the panel keeps a single consistent edge instead of mixed paddings. [Note: the shared S1 spec block that was meant to define these exact token names/values wasn't available when this pass ran; the set above was inferred to be consistent with the existing `--cream`/`--blush`/`--peach`/`--rosewood`/`--ink`/`--mist`/`--space-*` tokens already in this file and `styles.css`, and should be reconciled against the original spec if it turns up.]

**Layout (desktop ≥720px):** a 400px-wide floating left panel, cream, 20px radius, one soft shadow, 16px from the top/left edges, over a full-bleed map. Under 720px: the panel becomes a bottom sheet (peek/half/full) with a drag handle and keyboard-operable expand button. Radius by hierarchy: panel 20px, inputs 14px, chips fully rounded, map controls fully round. **Basemap (updated S1): OpenStreetMap standard tiles** (`tile.openstreetmap.org`), softened with a `saturate(.8) brightness(1.03) sepia(.06)` CSS filter on `.leaflet-tile-pane` only (markers, the route ribbon and controls are in other Leaflet panes and are unaffected). A bottom-right "Map / Satellite" toggle swaps to Esri World Imagery tiles (`server.arcgisonline.com/.../World_Imagery/MapServer`) and back; replaces the earlier CARTO Positron basemap, which now serves an "API keys required" placeholder from carto.com and is no longer usable.

**The route:** a 12px halo stroke (SVG `linearGradient`, blush→peach, positioned in screen space, updated on zoom), a 4px rosewood core line on top, a rosewood ring (start) and door-pin (end) marker, one ~600ms stroke-dashoffset draw-on (skipped under `prefers-reduced-motion`). Comparison routes stay grey (`#9A8F93`), dashed (Fastest) or dotted (Step-free), until selected.

**Copy:** plain, warm, sentence case. Steps: "Where to?" / "Starting from" / "Anything we should avoid?" / "How much does each one matter?". Buttons: "Continue", "Back", "Skip", "Find my route" (no trailing arrows). Stats as a two-column label/value list, not "a · b · c" strings. Errors say what to do. Privacy line: "Used for this route only. Never saved."

## [VERIFY] checks (run live before building)

- **Font (S1):** `fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,200..800&family=Atkinson+Hyperlegible+Next:wght@400;600;700&display=swap` → **200** (curl, this session). Both families load in one request; the "Atkinson Hyperlegible" / `system-ui` fallback chains are defense-in-depth.
- **Basemap (S1, superseded):** the P15 CARTO Positron URL (`{a,b}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png`) now serves an "API keys required" placeholder from carto.com -- no longer usable. Replaced with:
  - OSM standard tiles: `https://tile.openstreetmap.org/{z}/{x}/{y}.png` → **200** (curl, this session). Attribution: "© OpenStreetMap contributors".
  - Esri World Imagery (satellite toggle): `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}` → **200** (curl, this session). Attribution: "Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community". [VERIFY] this exact attribution string against Esri's current terms before a public (non-demo) deploy -- it's the commonly published text but wasn't independently re-confirmed beyond the tile request succeeding.
- **Contrast:** `--rosewood #A8445A` on `--cream #FFF9EC` ≈ 4.7:1 (passes 4.5:1 for body text); `--ink #2E2629` on cream ≈ 13:1. Neither token needed darkening.

## Step 0 live-backend check (hero trip, run against the real graph)

| Request | distance_ft | steps_avoided | max_slope_pct |
|---|---|---|---|
| No priorities (Fastest) | 1959.6 | 0 (uses stairs) | 21.6% |
| `avoid_stairs: essential` | 2578.4 | 59 | 19.9% |
| all four essential | 4263.2 | 59 | 9.4% |

The all-four-essential number (4263 ft) is lower than the `wheelchair` preset's own route (~7551 ft, from earlier P4-fix2 testing) -- confirmed as a real, traceable gap: F1's priority-chip table never sets `max_cross_slope_pct`/`min_width_ft` the way the `wheelchair` preset does, so the priorities combo is a strictly looser hard-constraint set. This is backend/F1 scope, not fixed here (per this prompt's explicit instruction); the UI shows whatever the live API returns rather than a hardcoded number.

## File structure

```
web/
  index.html          map-first shell; labelled slots for P16/P17
  css/styles.css       tokens, layout, panel/steps/chips/results, a11y
  css/auth.css         stub (P16)
  css/live.css         stub (P17)
  css/ai.css           stub (P17)
  js/app.js            state machine, ctx object, step flow, localStorage
  js/api.js            fetch wrappers (getBuildings/postRoute, F1 priorities)
  js/map.js            Leaflet init, gradient-ribbon rendering, markers
  js/auth.js           stub: export function init(ctx) {}
  js/live.js           stub: export function init(ctx) {}
  js/facilities.js     stub: export function init(ctx) {}
  js/ai.js             stub: export function init(ctx) {}
  config.example.js    unchanged shape
  config.js            gitignored
```

`app.js` builds one `ctx` object (`{map, api, request, reroute(), announce(), getPrefs(), setPrefs(), onRouteShown(cb)}`) and calls each stub module's `init(ctx)` once at startup. `index.html` contains `#auth-slot`/`#auth-dialog`, `#report-sheet`, `#ai-slot` (step 3, above the chips), `#conditions-slot` (results panel), `#facilities-panel` -- all empty, labelled, `hidden`, ready for P16/P17 to fill in without touching this prompt's files again.

## What was removed (one element per screen, per "Chanel's rule")

- **Search step:** dropped the separate "From: Current location · change" row P14a showed above the fold -- folded into its own step 2 instead, so the map-first layout only ever shows one focal control on load.
- **Chips step:** dropped the always-visible importance radios under every chip (P14a's layout) -- importance now only appears in its own step 4, for *selected* chips only.
- **Results:** dropped the separate plain-text "Show details" styling from P14a in favor of one expandable region ("More details") that folds in both the rest of the explanation *and* the per-segment list, instead of two.

## Known tension: the `AI` acceptance-criteria grep

F2's acceptance criteria require `grep -rni "safe route\|\bAI\b" web/` to be empty, but F2's own Task 1 explicitly mandates creating `web/js/ai.js`, `web/css/ai.css`, and an `#ai-slot` element (for P17's stretch natural-language-preferences feature). Those filenames/ids are code identifiers, never rendered as visible text, but they do make the literal grep command non-empty (matching `ai.js`, `ai-slot`, `initAi`, etc.). No user-facing "AI" copy exists anywhere in the app -- flagging this as a literal-grep-vs-required-filenames tension rather than silently renaming away from Task 1's mandated names.
