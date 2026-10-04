# Way2Go UI (P14a)

Plain HTML/CSS/JS, no build step, no npm. Talks to the routing service
(`docs/API.md`) over fetch and renders a [Leaflet](https://leafletjs.com/)
map loaded from unpkg.

## Running it

```bash
# Terminal 1: the routing service (needs a real graph.pkl -- see CLAUDE.md §4)
uvicorn service.app:app --port 8000

# Terminal 2: the UI
cp web/config.example.js web/config.js   # gitignored; edit apiBase if needed
python -m http.server 5173 -d web
```

Open `http://localhost:5173`. CORS on the service already allows this origin
(`service/app.py`'s `CORSMiddleware`).

## Config

`web/config.js` (gitignored, copy from `config.example.js`) sets
`window.WAY2GO_CONFIG.apiBase` (default `http://localhost:8000`).
`supabaseUrl`/`supabaseAnonKey` are reserved for a later prompt (live-report
UI) and unused by P14a. Never put a service-role key anywhere under `web/`
-- it ships to the browser.

## Screens / states

One page, a small state machine in `app.js`:

1. **Empty** -- initial load: map centered on the demo-zone bbox, the
   destination/origin/chip panel visible, no results section shown.
2. **Selecting** -- same panel. Chips are toggle buttons
   (`aria-pressed`); selecting one reveals an Essential/Important/Nice to
   have radio group (default Essential). "Find My Route" enables once a
   destination is typed (and an origin building, if not using current
   location).
3. **Loading** -- "Finding a route that works for you…" with a CSS
   spin animation, replaced by a static state under
   `prefers-reduced-motion: reduce`. Shown while 3 `/route` calls
   (Recommended, Fastest, Step-free) run in parallel.
4. **Result** -- map draws the selected comparison card's route; a summary
   card (minutes, distance, stairs avoided, max slope with an "estimated"
   tag, % lit, destination entrance accessibility/auto-door); a "Why this
   route?" card (first 3-4 `explanation` lines, "Show details" expands the
   rest plus the per-segment list); a 3-card comparison row (⭐ Recommended /
   ⚡ Fastest / ♿ Step-free) -- clicking a card redraws that route. The three
   route lines differ by both color and dash pattern (solid / dashed /
   dotted), never color alone.
5. **No perfect route** -- a banner on top of Result when the selected
   route has `violations` or certain `warnings` (destination-entrance
   fallback, distance-tolerance). Each line names the specific chip it
   breaks; a chip set to Essential is always shown, bolded, never hidden.
6. **Error** -- "We couldn't find that location." (any `/route` 422, or a
   failed/denied geolocation lookup) with a "Search again" button.

## Hidden features (per the Way2Go spec, no data tonight)

Elevators, rest stops, noise, bathrooms, a "Quietest" chip, and
turn-by-turn directions are not in this UI. No "AI" wording appears
anywhere; the app says "well-lit route", never "safe route"; the footer
always reads "Routes are guidance, not ADA certification."

## Chip -> request mapping (assumption, pending P13)

The live API today only understands `profiles` (presets) and
`preferences` (direct overrides of CLAUDE.md §5's keys) -- it doesn't
understand `priorities`/importance levels yet (that's P13). So every
request:

- always uses `profiles: ["fastest"]` as the neutral base (not a named
  preset like `wheelchair`), so chips compose independently of a preset's
  unrelated defaults;
- translates each **Essential or Important** chip into a real
  `preferences` override (Nice to have chips apply no override today):

  | Chip | Essential | Important | Nice to have |
  |---|---|---|---|
  | Avoid stairs | `avoid_stairs: true` | `avoid_stairs: true` | none |
  | Avoid steep slopes | `max_slope_pct: 5.0` | `max_slope_pct: 8.33` | none |
  | Curb cuts at crossings | `require_curb_cuts: true` | `require_curb_cuts: true` | none |
  | Prioritize accessible entrances | `require_accessible_entrance: true` | `require_accessible_entrance: true` | none |
  | Prefer well-lit paths | `prefer_lit: true` | `prefer_lit: true` | none |

  When any chip is Essential or Important, `preferences.distance_tolerance`
  is also set to the max across selected chips (Essential -> 4.0,
  Important -> 2.5), so the hard limits above have room to find a
  compliant route instead of immediately falling back.
- always also sends every selected chip (any level) as
  `priorities: {<key>: "essential"|"important"|"nice_to_have"}` --
  inert today (pydantic's `RouteRequest` ignores unknown fields rather
  than rejecting them), meaningful once P13 wires it up.

With nothing selected, the request is `{profiles: ["fastest"]}` with no
preferences -- identical to the Fastest comparison call, so the result
equals Fastest, by construction.

The **Step-free** comparison card always sends a fixed body regardless of
the user's own chip selection: `preferences: {avoid_stairs: true,
max_slope_pct: 5.0, require_curb_cuts: true,
require_accessible_entrance: true, distance_tolerance: 4.0}` with all four
marked Essential in `priorities`, per the prompt's spec for that card.

## Other notable decisions

- The "max slope... estimated" tag on the summary card uses
  `stats.estimated_segments > 0` as a proxy for "the reported max slope
  came from a lidar estimate" -- the API doesn't attribute the specific
  max-slope value to a `slope_source` directly, only per-segment in
  `segments[]` (which the "Show details" panel does show exactly).
- The Fastest/Step-free comparison cards label a route "uses stairs" when
  `steps_avoided === 0`, since the API has no direct "stairs used"
  boolean; this matches the documented hero-trip behavior (Fastest's
  `steps_avoided` is 0 specifically because it takes the stair shortcut
  instead of avoiding it -- CLAUDE.md §9 Known issues, P4 entry) but is an
  approximation for routes with no stairs anywhere nearby either.
- Building typeahead uses a native `<input list><datalist>` against
  `GET /buildings`, not a custom ARIA combobox -- simplest option with
  built-in keyboard support, a deliberate scope tradeoff for this prompt.
- Only one comparison route is drawn on the map at a time (whichever card
  is selected); a legend isn't rendered as a separate always-visible
  element beyond the card icons/labels and the line's own color+dash
  style, since only one line is ever on screen at once.

## Known gap

This was built and reviewed without a live backend: this machine has no
`cornell_data/graph/graph.pkl` (gitignored, built on another machine per
CLAUDE.md's P1 Known issue), so `uvicorn service.app:app` can't actually
load a graph here. The request/response shapes were verified against
`docs/API.md` and the real example payloads/numbers recorded in CLAUDE.md
§9's Known issues (from P4/P4-fix2's real test runs), not a live click-through.
Someone with a real `graph.pkl` should run the human checkpoint below.
