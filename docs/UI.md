# Way2Go UI (P15 redesign)

Plain HTML/CSS/JS (ES modules), no build step, no npm. Map-first,
Google-Maps-style layout per `docs/DESIGN.md`. Talks to the routing
service (`docs/API.md`) over fetch, including F1's live `priorities`
support.

## Running it

```bash
# Terminal 1: the routing service (needs a real graph.pkl -- see CLAUDE.md §4)
uvicorn service.app:app --port 8000

# Terminal 2: the UI
cp web/config.example.js web/config.js   # gitignored; edit apiBase if needed
python -m http.server 5173 -d web
```

Open `http://localhost:5173`. CORS on the service already allows this
origin.

## Config

`web/config.js` (gitignored, copy from `config.example.js`) sets
`window.WAY2GO_CONFIG.apiBase` (default `http://localhost:8000`).
`supabaseUrl`/`supabaseAnonKey` are reserved for P16/P17 and unused today.
Never put a service-role key anywhere under `web/`.

## Module map (for P16/P17, built in parallel without touching this prompt's files)

| File | Owns | Slot(s) |
|---|---|---|
| `js/app.js` | Step flow, state, the shared `ctx` object | -- |
| `js/api.js` | `GET /buildings`, `POST /route` (3-call pattern: recommended/fastest/stepfree) | -- |
| `js/map.js` | Leaflet, the gradient-ribbon route, comparison lines, zoom/recenter | -- |
| `js/auth.js` (stub) | Sign-in | `#auth-slot` (top-right), `#auth-dialog` |
| `js/live.js` (stub) | Live hazard reports, conditions | `#report-sheet`, `#conditions-slot` (inside results) |
| `js/facilities.js` (stub) | Bottleneck/facilities data (`GET /bottlenecks`) | `#facilities-panel` |
| `js/ai.js` (stub) | Natural-language preferences (optional/stretch; app works fully without it) | `#ai-slot` (step 3, above the chips) |

Every stub exports `init(ctx)`, called once at startup with
`{map, api, request, reroute(), announce(), getPrefs(), setPrefs(), onRouteShown(cb)}`.
Stubs are no-ops today; P16/P17 fill in their own `js`/`css` files and the
labelled slots in `index.html` without editing `app.js`/`api.js`/`map.js`.

## Screens / states

One page, a guided step flow in the left panel (bottom sheet under
720px) over a full-bleed map:

1. **Load:** map centered on the demo zone; "Where to?" auto-focused.
2. **Step 1 "Where to?"** → **Step 2 "Starting from"** (use my location [geolocation requested only on this click, with the privacy line] or choose a building; a >100 m point 422 shows "You're outside the mapped area. Choose a starting building.") → **Step 3 "Anything we should avoid?"** (5 pill chips, Skip = no preferences, "Use my usual preferences" one-tap if saved) → **Step 4 "How much does each one matter?"** (segmented Essential/Important/Nice per selected chip only) → **Find my route**.
3. **Loading:** "Finding a route that works for you" with a pulse, off under `prefers-reduced-motion`.
4. **Results — "Recommended for you":** headline minutes/distance, two-column stats, "Why this route?" (first 3 points + "More details"), a Recommended/Fastest/Step-free segmented compare control (each swaps the highlighted route), "Edit preferences" (→ step 3, keeps choices) / "New destination" (→ step 1, resets).
5. **No perfect route:** a banner naming exactly which selected preference each compromise breaks; an Essential chip's violation is always shown, bolded, never hidden.
6. **Error:** "We couldn't find that building. Try another name." (building-name 422) or "You're outside the mapped area. Choose a starting building." (point-too-far 422) or a generic retry message, each with its own recovery action.

## Chip → request mapping

Unlike P14a (which translated chips into `preferences` overrides because `priorities` was still a no-op), this redesign sends `priorities: {chip_id: "essential"|"important"|"nice"}` directly -- F1 (`service/profiles.py`) now interprets it. See `docs/API.md`'s priorities table for exactly what each level does server-side. The Step-free comparison card always sends a fixed `priorities` body (all four hard-limit chips at Essential) regardless of the user's own selection, per spec.

## Accessibility

Landmarks, labelled controls, visible focus (2px rosewood ring + blush halo), full keyboard flow (Tab follows visual order), `aria-live` announcements per step/result, A−/A+ text-size control in the panel footer (200%-zoom-safe relative units throughout), ≥44px touch targets, comparison routes differentiated by dash pattern + label (never color-only).

## Hidden features (per the Way2Go spec, no data tonight)

Elevators, rest stops, noise, bathrooms, a "Quietest" chip, turn-by-turn directions, and the map Layers feature (no `/layers` endpoint exists) are not in this UI. No "AI" wording appears in any visible text; the app says "well-lit", never "safe route"; the footer always reads "Routes are guidance, not ADA certification." (`js/ai.js`/`css/ai.css`/`#ai-slot` are P17-stub *filenames*, not user-facing copy -- see `docs/DESIGN.md`'s "Known tension" note.)

## Known gap

Verified against the live API on a real graph (`curl` hero-trip checks, documented in `docs/DESIGN.md`), including building-name resolution for both "Keeton" and "Goldwin Smith" via `GET /buildings`. I could not do an actual visual/keyboard click-through in a real browser this session (the Claude-in-Chrome extension wasn't connected); verification was via syntax checks (`node --check`), a static-file smoke test, and careful manual tracing of the DOM/event wiring. Someone should run the human checkpoint (keyboard-only hero trip, visual check against the spec) before relying on this as fully verified.
