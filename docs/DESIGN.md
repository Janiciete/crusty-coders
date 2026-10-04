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

**Type:** Atkinson Hyperlegible Next (Google Fonts), falling back to "Atkinson Hyperlegible" then `system-ui`. Scale 15/17/21/28px, weights 400/600/700, body line-height 1.5, sentence case everywhere. The step question and the route's minutes are 28px/700.

**Layout (desktop ≥720px):** a 400px-wide floating left panel, cream, 20px radius, one soft shadow, 16px from the top/left edges, over a full-bleed map. Under 720px: the panel becomes a bottom sheet (peek/half/full) with a drag handle and keyboard-operable expand button. Radius by hierarchy: panel 20px, inputs 14px, chips fully rounded, map controls fully round. Basemap: CARTO Positron light.

**The route:** a 12px halo stroke (SVG `linearGradient`, blush→peach, positioned in screen space, updated on zoom), a 4px rosewood core line on top, a rosewood ring (start) and door-pin (end) marker, one ~600ms stroke-dashoffset draw-on (skipped under `prefers-reduced-motion`). Comparison routes stay grey (`#9A8F93`), dashed (Fastest) or dotted (Step-free), until selected.

**Copy:** plain, warm, sentence case. Steps: "Where to?" / "Starting from" / "Anything we should avoid?" / "How much does each one matter?". Buttons: "Continue", "Back", "Skip", "Find my route" (no trailing arrows). Stats as a two-column label/value list, not "a · b · c" strings. Errors say what to do. Privacy line: "Used for this route only. Never saved."

## [VERIFY] checks (run live before building)

- **Font:** `fonts.googleapis.com/css2?family=Atkinson+Hyperlegible+Next:wght@400;600;700&display=swap` → **200**. Confirmed available; loaded directly (the "Atkinson Hyperlegible" / `system-ui` fallback chain in the CSS stack is defense-in-depth, not because the Next family is missing).
- **Basemap:** `https://{a,b}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png` → **200**. Attribution used: "© OpenStreetMap contributors © CARTO".
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
