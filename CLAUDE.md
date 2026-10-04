# CLAUDE.md — Accessible Campus Navigator (Big Red Hacks 2026)
Source of truth: `docs/project_plan.md` ("the plan", cited as §N). If this file and the plan disagree, the plan wins; record the conflict under Known issues. `[CONFIRM]` = team default awaiting sign-off. `[VERIFY]` = assumption to check against code/data before relying on it.

## 1. Project summary
Walking navigation for Cornell central campus that routes on each user's accessibility needs, not just distance. Users pick combinable profiles (Wheelchair, Injured, Low Vision, Night Walk, Fastest); a local Python service routes over a graph built from Cornell Facilities GIS data and explains each route against ADA standards. Supabase holds anonymous live reports (Realtime rerouting), optional accounts, and saved preferences. Runs on localhost. This repo is backend only; the UI team builds against `docs/API.md`.

## 2. Stack and versions
- Python 3.11 [CONFIRM]; pipeline: geopandas, shapely 2.x, networkx, pandas (pinned in requirements.txt)
- Routing service: FastAPI + uvicorn, pydantic v2, httpx, python-dotenv [CONFIRM]
- Supabase: Postgres + PostGIS, RLS, Realtime; Auth (Should); Storage (Stretch). Python client: supabase-py [CONFIRM]
- Sunset/sunrise: `astral` in Python [CONFIRM] (plan §3.1 names suncalc, a JavaScript library)
- Weather: NWS api.weather.gov, grid BGM 45,70, no key, User-Agent header required
- Elevation: USGS EPQS, used only by `build_graph.py --elevation`, results cached
- AI (optional, Stretch): xAI API over httpx; everything must work with XAI_API_KEY unset
- Tests: pytest

## 3. Repo layout
```
CLAUDE.md  requirements.txt  .env.example  .gitignore
pipeline/              fetch_cornell_data.py, build_graph.py (existing) [CONFIRM location]
cornell_data/          raw Cornell downloads — gitignored, NEVER edit by hand or by code
cornell_data/graph/    build outputs: edges.geojson nodes.geojson graph.pkl seed_reports.geojson build_report.txt
service/               FastAPI app: app.py profiles.py cost.py router.py explain.py conditions.py reports.py bottleneck.py ai.py
supabase/migrations/   SQL: extensions, tables, RLS policies, Realtime publication
scripts/               one-off tools: load seed reports, GPS replay, run bottleneck finder
tests/                 pytest; unit tests use a tiny hand-built fixture graph, not graph.pkl
docs/                  project_plan.md, API.md (UI contract), devpost.md
```

## 4. Run commands (from repo root)
```bash
python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env   # fill in; never commit .env
python pipeline/fetch_cornell_data.py
python pipeline/build_graph.py --elevation --bbox -76.4905 42.4430 -76.4790 42.4520
cat cornell_data/graph/build_report.txt
uvicorn service.app:app --port 8000   # loads graph.pkl once at startup
pytest -q
```
`.env` keys: SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY (service only), NWS_USER_AGENT, XAI_API_KEY (optional), GRAPH_PATH (default cornell_data/graph/graph.pkl).

## 5. Data contracts
**Edge attributes (§8.1, produced by build_graph.py — do not rename):**
length_ft · kind (sidewalk / crosswalk / snap / door approach / inferred; exact strings [VERIFY]) · travel (preferred, preferred_candidate, stair, steep, service_route, other, unlabeled, needs_check) · is_stairs · steps · landings · rail_side · slope_pct · slope_source (cornell_survey | usgs_lidar_1m_estimate; value for "unknown" <20 ft segments [VERIFY]) · cross_slope_pct · width_ft · surface · defect_level (0–3) · ramp · handrail · curb_cuts (0/1/2, crosswalks) · surveyed · date_surveyed (YYYY-MM-DD) · lit_fixtures · lit_per_100ft · lit_watts (47% filled; don't score on it) · inferred · verified · flags
**Entrance node attributes:** door_id · access (accessible | not_accessible | unknown) · auto_opener · threshold_ok · clear_width_in · access_control (open | card | key) · building. `unknown` is never treated as accessible.

**Preferences JSON (§7.2), stored in `profiles.preferences` or on-device for guests:**
`{"avoid_stairs": bool, "max_slope_pct": float, "max_cross_slope_pct": float, "min_width_ft": float, "prefer_lit": bool, "surface_sensitivity": int 0–3 [CONFIRM type], "distance_tolerance": float ≥1.0, max length ratio vs. shortest [CONFIRM type]}`

**Supabase tables (§7.2):**
- `profiles(user_id uuid pk → auth.users, preferences jsonb, created_at, updated_at)` — RLS: owner only
- `saved_places(id, user_id, name, location geography(Point,4326))` — RLS: owner only
- `reports(id, type, location geography(Point,4326) rounded ~10 m, note, photo_path, created_at, expires_at, confirmations int, status pending|confirmed|expired, source user|seed)` — RLS: public select, anon insert; no user id column
- `report_confirmations(report_id, user_hash, created_at)` — RLS: insert only; unique(report_id, user_hash); raw user id never stored

**Route API (proposed, [CONFIRM] with UI team; documented in docs/API.md):**
`POST /route` body: `{"origin": {"lat","lon"} | {"building"}, "destination": {"building"} | {"lat","lon"}, "profiles": ["injured","low_vision"], "preferences": {…optional overrides}, "adjustments": ["in_a_hurry"…], "conditions": {"darkness": "auto|on|off", "ice": "auto|on|off"}, "alternatives": 3}`
Response: `{"conditions": {darkness, ice, source}, "warnings": [], "routes": [{"id", "geometry": GeoJSON LineString, "stats": {distance_ft, est_time_min, steps_avoided, max_slope_pct, max_cross_slope_pct, pct_lit, destination_entrance {door_id, building, access, auto_opener}, reports_avoided, unverified_segments, estimated_segments}, "violations": [] (fallback only), "explanation": ["…"], "segments": [{edge_id, slope_pct, slope_source, cross_slope_pct, date_surveyed, verified}]}]}`
Other endpoints: `GET /health`, `GET /profiles` (defaults), `GET /conditions`, `GET /buildings`, `GET /bottlenecks`.

## 6. Domain constants
- Demo-zone bbox (lon/lat): W −76.4905, S 42.4430, E −76.4790, N 42.4520
- ADA: walkway slope ≤5%, ramp slope ≤8.33%, cross slope ≤2%, clear width ≥36 in (3 ft)
- Profile keys: wheelchair, injured, low_vision, night_walk, fastest (defaults §8.3; combining = union of hard limits, max of penalties [CONFIRM])
- Adjustments: in_a_hurry, carrying_items, walking_alone (effects not defined in plan; see Known issues)
- Ice: NWS hourly ≤34°F or snow/ice in forecast. Darkness: between sunset and sunrise. Both user-overridable.
- Report types: blocked_path, ice, construction, too_steep, too_dark, too_loud, crowded. "Nothing, just me" is never stored.
- Expiry: ice 6 h, blocked_path 24 h, construction 7 d, too_dark/too_loud/crowded 2 h, too_steep unspecified
- Confirmation: ≥2 independent same-type reports within ~15 m in a short window. Report-to-edge match: ~20 m.
- Effects: blocked_path/construction remove edge; ice/too_steep/too_dark penalize edge.
- Sanity numbers (§4–5): 2,341 demo-zone Walk Inventory segments (edge count differs after splitting); largest connected component ~97%; 354 accessible entrances; 119 demo-zone crosswalks (288 total); 404 stair segments, 370 matched to polygons; 107 re-tagged as stairs; 209 segments >5%, 73 >8.33%.

## 7. Privacy, security, ethics (§11) — non-negotiable
- DO store routing preferences only. DON'T store disability labels, diagnoses, or medical info.
- DON'T store or log user location or traces. Coordinates go in POST bodies, never URL query strings; never log request bodies.
- DO round report locations to ~10 m (4 decimal places) before insert. DON'T store user ids on reports.
- DO enable RLS on every table. DON'T send the service role key to the browser or expose it in any response.
- DON'T commit keys: secrets live in `.env` (gitignored); `.env.example` has placeholders only.
- DO label estimated data (lidar slopes, inferred edges, crowds) as estimated in every API response and explanation.
- DO say "well-lit route". DON'T say "safe route". No crime data. Routes are guidance, not ADA certification.
- Cornell GIS: read-only GET/query requests only.
- AI (xAI) never computes routes; validate its JSON against allowed keys/ranges before use; app works fully without it.
- Seed reports are labeled historical (source = seed), never presented as live.

## 8. Working rules for Claude Code
1. Read this file and every file you will touch before editing.
2. For multi-file or risky changes, propose a plan and wait for approval.
3. Edit only the files the current prompt allows. Never touch frontend code.
4. Never modify files in `cornell_data/` except outputs written by build_graph.py.
5. Add any new dependency to requirements.txt and name it in your report.
6. Write pytest tests for logic; run `pytest -q` before reporting.
7. Don't invent field names, API behavior, or data facts. Mark guesses `[VERIFY]` and list them.
8. Finish with: files changed · test results · deviations and why · assumptions · open questions. Then update Status below.

## 9. Status
- [x] Prompt 0: repo setup (structure, .env.example, .gitignore, requirements.txt, smoke test)
- [ ] Prompts 1–N: filled in after roadmap approval

**Known issues** (append; mark resolved with date/time)
- build_graph.py tested only on synthetic data; first real run may fail.
- suncalc is JavaScript; Python service uses astral unless changed.
- NWS requires a User-Agent header; requests fail without it.
- Wheelchair hard limits (ramp rule for 5–8.33%, 2 curb cuts, accessible-entrance destination) are not expressible with the §7.2 preference keys.
- Plan gives no expiry for too_steep, no confirmation time window, no per-profile walking speed, no effects for adjustments.
- Libe Slope and lidar-estimated slopes may leave no strict wheelchair route; fallback rule (§8.3) must warn.