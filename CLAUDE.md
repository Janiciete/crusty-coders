# CLAUDE.md — Accessible Campus Navigator (Big Red Hacks 2026)
Source of truth: `docs/project_plan.md` ("the plan", cited as §N). If this file and the plan disagree, the plan wins; record the conflict under Known issues. `[VERIFY]` = assumption to check against code/data before relying on it.

## 1. Project summary
Walking navigation for Cornell central campus that routes on each user's accessibility needs, not just distance. Users pick combinable profiles (Wheelchair, Injured, Low Vision, Night Walk, Fastest); a local Python service routes over a graph built from Cornell Facilities GIS data and explains each route against ADA standards. Supabase holds anonymous live reports, optional accounts, and saved preferences. Runs on localhost. This repo is backend only; the UI team builds against `docs/API.md`.
Pitch line: "Navigation that routes on your own definition of accessible, ends at an entrance you can use, and explains every choice. Same start and destination, different person, different route."

## 2. Stack and versions
- Python 3.11+ (pandas 3.x requires it). Use the versions pinned in requirements.txt; don't upgrade them.
- Pipeline: geopandas, shapely 2.x, networkx, pandas
- Routing service: FastAPI + uvicorn, pydantic v2, httpx, python-dotenv. CORS allows `http://localhost:5173` (UI dev server).
- Supabase: Postgres + PostGIS, RLS, Realtime; Auth (Should); Storage (Stretch). Backend client: supabase-py. The frontend uses the anon key, inserts reports, and subscribes to Realtime itself; the backend reads active reports on each route request and does not use Realtime.
- Darkness: computed in the backend with `astral` (Ithaca, America/New_York). Not suncalc.
- AI (optional, Stretch): xAI API over httpx; everything must work with XAI_API_KEY unset.
- Tests: pytest

**Data sources (read-only GET/query only):**
- Cornell GIS base `https://gis.fcs.cornell.edu/arcgis/rest/services/Production/` (no key; GeoJSON, EPSG:4326; 2,000 records/page). Layers: `WalkInventory_2026/FeatureServer/0` · `Path_of_Travel_2022/FeatureServer/1` (crosswalks) · `EntranceInventory_2025/FeatureServer/0` · `Stairs_Polygon/FeatureServer/0` · `Campus_Lighting/FeatureServer/17` · `Buildings/FeatureServer/0` · `Trip_Hazards/FeatureServer/0`
- NWS `https://api.weather.gov`, grid BGM 45,70, User-Agent header required
- USGS EPQS `https://epqs.nationalmap.gov/v1/json`, one point per request, cached; used only by `build_graph.py --elevation`
- OSM Overpass `https://overpass-api.de` (POST): backup network only, not used unless the plan's fallback (§15) is triggered

## 3. Repo layout
```
CLAUDE.md  README.md  requirements.txt  .env.example  .gitignore
pipeline/              fetch_cornell_data.py, build_graph.py (from the planning chat; added by a human)
cornell_data/          raw Cornell downloads — gitignored, NEVER edit by hand or by code
cornell_data/graph/    build outputs: edges.geojson nodes.geojson graph.pkl seed_reports.geojson build_report.txt bottlenecks.json
service/               FastAPI app: app.py profiles.py cost.py router.py explain.py conditions.py reports.py bottleneck.py ai.py
supabase/migrations/   SQL: extensions, tables, RLS policies, triggers, Realtime publication
scripts/               one-off tools: load seed reports, run bottleneck finder, GPS replay
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
`.env` keys: SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY (service only), NWS_USER_AGENT, XAI_API_KEY (optional), GRAPH_PATH. Treat an empty value as unset: `os.getenv("GRAPH_PATH") or "cornell_data/graph/graph.pkl"`.

## 5. Data contracts
**Edge attributes (§8.1, produced by build_graph.py — do not rename):**
length_ft · kind (sidewalk / crosswalk / snap / door approach / inferred — confirmed exact strings, lowercase, space not underscore in "door approach") · travel (preferred, preferred_candidate, stair, steep, service_route, other, unlabeled, needs_check) · is_stairs · steps · landings · rail_side · slope_pct · slope_source (cornell_survey | usgs_lidar_1m_estimate | unknown — confirmed: "unknown" is used for every unsurveyed edge until P2's --elevation/lidar fill runs, not only segments <20 ft; after P2 runs, usgs_lidar_1m_estimate should replace "unknown" on segments ≥20 ft and "unknown" should remain only for the <20 ft case per plan §5.2) · cross_slope_pct · width_ft · surface · defect_level (0–3) · ramp · handrail · curb_cuts (0/1/2, crosswalks) · surveyed · date_surveyed (YYYY-MM-DD) · lit_fixtures · lit_per_100ft · lit_watts (47% filled; don't score on it) · inferred · verified · flags
**Entrance node attributes:** door_id · access (accessible | not_accessible | unknown) · auto_opener · threshold_ok · clear_width_in · access_control (open | card | key) · building. `unknown` is never treated as accessible.

**Preferences JSON (§7.2), stored in `profiles.preferences` or on-device for guests:**
`{"avoid_stairs": bool, "max_slope_pct": float, "max_cross_slope_pct": float, "min_width_ft": float, "prefer_lit": bool, "surface_sensitivity": int 0–3, "distance_tolerance": float ≥1.0 (max length ratio vs. shortest), "require_curb_cuts": bool, "require_accessible_entrance": bool, "ramp_required_above_pct": float (default 5.0)}`
The last three keys are neutral routing preferences added beyond §7.2 so Wheelchair limits can be stored without a label. Profile names are never stored in Supabase; profiles are presets that fill these keys.

**Supabase tables (§7.2):**
- `profiles(user_id uuid pk → auth.users, preferences jsonb, created_at, updated_at)` — RLS: owner only
- `saved_places(id, user_id, name, location geography(Point,4326))` — RLS: owner only
- `reports(id, type, location geography(Point,4326), note, photo_path, created_at, expires_at, confirmations int, status pending|confirmed|expired, source user|seed)` — RLS: public select, anon insert; no user id column. Triggers round location to ~10 m and set expires_at/status.
- `report_confirmations(report_id, user_hash, created_at)` — RLS: insert only; unique(report_id, user_hash); raw user id never stored

**Route API (confirmed with UI team; documented in docs/API.md):**
`POST /route` body: `{"origin": {"lat","lon"} | {"building"}, "destination": {"building"} | {"lat","lon"}, "profiles": ["injured","low_vision"], "preferences": {…optional overrides}, "adjustments": ["in_a_hurry"…], "conditions": {"darkness": "auto|on|off", "ice": "auto|on|off"}, "alternatives": 3}`
Response: `{"conditions": {darkness, ice, source}, "warnings": [], "routes": [{"id", "geometry": GeoJSON LineString, "stats": {distance_ft, est_time_min, steps_avoided, max_slope_pct, max_cross_slope_pct, pct_lit, destination_entrance {door_id, building, access, auto_opener}, reports_avoided, unverified_segments, estimated_segments}, "violations": [] (fallback only), "explanation": ["…"], "segments": [{edge_id, slope_pct, slope_source, cross_slope_pct, date_surveyed, verified}]}]}`
Other endpoints: `GET /health`, `GET /profiles` (defaults), `GET /conditions`, `GET /buildings`, `GET /bottlenecks`.

**Service module contracts (so prompts can run in parallel):**
- `service/conditions.py`: `get_conditions(overrides: dict) -> Conditions` (darkness, ice, source, warnings). Stub returns all off until Prompt 7.
- `service/reports.py`: `get_active_reports() -> list[Report]` (reads Supabase view `active_reports`: id, type, lat, lon, status, source, confirmations, created_at, expires_at) and `edge_effects_for(G, reports) -> dict[edge_key, "remove" | float]`. Stubs return [] and {} until Prompt 6.
- Only Prompt 4 creates `service/app.py`. Prompts 6–8 plug in through their own modules and do not edit app.py.

## 6. Domain constants
- Demo-zone bbox (lon/lat): W −76.4905, S 42.4430, E −76.4790, N 42.4520. Metric work in EPSG:32618 (UTM 18N) [VERIFY].
- ADA: walkway slope ≤5%, ramp slope ≤8.33%, cross slope ≤2%, clear width ≥36 in (3 ft)
- Profile keys: wheelchair, injured, low_vision, night_walk, fastest (defaults §8.3). Demo hero trip: Keeton House → Goldwin Smith Hall.
- Ice: NWS hourly ≤34°F or snow/ice in forecast. Darkness: between sunset and sunrise. Both user-overridable.
- Report types: blocked_path, ice, construction, too_steep, too_dark, too_loud, crowded. "Nothing, just me" is never stored.
- Expiry: ice 6 h, blocked_path 24 h, construction 7 d, too_dark/too_loud/crowded 2 h, too_steep 7 d
- Effects: blocked_path/construction remove edge; ice/too_steep/too_dark penalize edge. Report-to-edge match: ~20 m.

**Team defaults (tunable; not in the plan — change here, not in code comments):**
- Combining profiles: union of hard limits, max of each penalty, slowest walking speed.
- Walking speed (ft/s): fastest, low_vision, night_walk 4.3 · wheelchair 3.3 · injured 2.5.
- Adjustments: in_a_hurry → distance_tolerance capped at 1.1 (never relaxes hard limits) · carrying_items → stair penalty ×2 where stairs are allowed · walking_alone → prefer_lit = true.
- Slope hard limits apply only to surveyed slopes (`cornell_survey`). Estimated slopes get a heavy penalty plus an "estimated" label; unknown slopes get the unverified penalty.
- Dark segment: lit_per_100ft in the bottom quartile of demo-zone edges (computed from data). pct_lit = share of route length not dark.
- Ice multiplies slope penalties ×2 and unverified penalties ×1.5. Darkness turns on the lighting penalty for every profile.
- Penalty weights live in one dict in `service/profiles.py`.
- Alternatives: re-route with the previous route's edges ×1.5 cost, up to 3, drop any with >80% length overlap.
- Origins: building name or point; a point >100 m from the graph returns HTTP 422 with a clear message.
- Confirmation: a user who sees an existing same-type report within 15 m sends a confirmation (report_confirmations, distinct user_hash) instead of a new report; status → confirmed at 2 (reporter + 1). Two separate same-type reports within 15 m and 60 min also confirm (independence not verifiable; known limitation).
- user_hash: SHA-256 of a random per-device ID generated in the browser; never derived from an account id.
- Seed reports: status confirmed, source seed, no expiry, penalize only (never remove edges), labeled "historical". `POST /route` accepts `"include_seed_reports": bool` (default true).
- Bottleneck finder: 2,000 sampled accessible-entrance pairs (seed 42); a trip is blocked if the Wheelchair route is missing or >1.5× the unconstrained length.
- No strict Wheelchair route: fallback rule returns the best route with `violations` and warnings; never silently drops limits.
- Sanity numbers (§4–5): 2,341 demo-zone Walk Inventory segments (edge count differs after splitting); largest connected component ~97%; 354 accessible entrances; 119 demo-zone crosswalks (288 total); 404 stair segments, 370 matched to polygons; 107 re-tagged as stairs; 209 segments >5%, 73 >8.33%.

## 7. Privacy, security, ethics (§11) — non-negotiable
- DO store routing preferences only. DON'T store disability labels, profile names, diagnoses, or medical info.
- DON'T store or log user location or traces. Coordinates go in POST bodies, never URL query strings; never log request bodies.
- DO round report locations to ~10 m (4 decimal places), enforced by a DB trigger. DON'T store user ids on reports.
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
4. Never modify files in `cornell_data/` except outputs written by the pipeline scripts.
5. Add any new dependency to requirements.txt (pinned) and name it in your report.
6. Write pytest tests for logic; run `pytest -q` before reporting.
7. Don't invent field names, API behavior, or data facts. Mark guesses `[VERIFY]` and list them.
8. Never run git commit, git push, or any history-changing git command. Humans review and commit.
9. Finish with: files changed · test results · deviations and why · assumptions · open questions. Then update Status below.

## 9. Status
- [x] P0 Repo skeleton and smoke test
- [x] P1 Run pipeline on real data (no --elevation) — Must
- [ ] P2 Lidar slope fill + build validation — Must (cuttable)
- [x] P3 Routing core on fixture graph — Must
- [ ] P4 Route API, stats, explanations, entrance destinations — Must
- [x] P5 Supabase schema, RLS, triggers, Realtime — Must (migrations written; not yet applied to a live project — human checkpoint pending)
- [ ] P6 Live reports in routing + seed report loader — Must (seed: Should)
- [ ] P7 Weather and darkness conditions — Should
- [ ] P8 Bottleneck finder — Should
- [ ] P9 xAI natural-language preferences — Stretch, dropped (2-person team)
- [ ] P10 GPS replay trace for slowdown demo — Stretch, dropped (2-person team)
- [ ] P11 Honest Devpost write-up — Must

**Known issues** (append; mark resolved with date/time)
- Graph outputs are gitignored; teammates must share graph.pkl out of band or rebuild.
- NWS requires a User-Agent header; requests fail without it.
- Libe Slope may leave no strict Wheelchair route for the demo trip; check at the P4 checkpoint.
- Deleting the auth user itself needs the admin API; `delete_my_data()` removes profile and saved places only.
- Report inserts can't prove independence (no user id by design); acceptable for the demo, disclose in Devpost.
- Resolved 00:05: preference keys extended (§5); missing plan values set as team defaults (§6).
- Resolved 01:30 (P1): fetch_cornell_data.py and build_graph.py written and run on real demo-zone data (no --elevation). graph.pkl: MultiGraph, 4070 nodes, 6092 edges, largest component 95.2% of length (plan ~97%, ≥90% required — passes). Accessible entrances 326 (plan 354, within 300–400 band). Crosswalks in zone 119 (exact match). Stair retagging (718) and slope>5%/8.33% edge counts (323/101) run higher than the plan's per-segment figures (107 / 209 / 73) because (a) touch-point splitting turns one physical segment into several edges that each inherit the same attributes, and (b) the stair safety retag check was deliberately widened to cover every edge kind, not just sidewalks, since the "no edge on a stair polygon has is_stairs=False" requirement is unqualified by kind — see pipeline/build_graph.py. graph.pkl is gitignored; share out of band per above.
- NEW (P1): slope_source is "unknown" (not usgs_lidar_1m_estimate) for all unsurveyed edges until P2 (--elevation) runs; routing/cost logic should treat "unknown" as the unverified-penalty case, same as it will treat the <20 ft case post-P2.
- NEW (P1): 10 entrances (of 675 kept after dropping exit-only/sealed) are >100 ft from the path network and remain unattached (0 edges) in graph.pkl; not routable. Worth checking for the demo route.
- NEW (P1): graph.pkl is a MultiGraph (not a simple Graph) — routing code must iterate edges with `G.edges(keys=True, data=True)` since multiple edges can exist between the same node pair (e.g. parallel short stair-tread segments).
- Resolved 02:45 (P3, routing core): built against a hand-built fixture graph (tests/conftest.py); 20 tests pass (`pytest -q tests/test_routing.py`). Graph-specific details (edge `kind` strings, surface values, Graph-vs-MultiGraph) are isolated in service/profiles.py and service/router.py's top blocks, marked [VERIFY after P1] — now that P1 has landed, these should be reconciled against the real `kind`/`slope_source` values confirmed above and the confirmed MultiGraph output. Penalty weights live in service/profiles.py:PENALTY_WEIGHTS. distance_tolerance is resolved into preferences but not yet enforced by router.py — open question for P4. Human checkpoint still needed: compare PROFILE_PRESETS against plan §8.3 line by line before commit.
- Resolved 02:45 (P5, Supabase): migrations written (supabase/migrations/0001-0004.sql) covering profiles/saved_places/reports/report_confirmations, RLS on all four tables, location-rounding + expiry + confirmation triggers, active_reports view, Realtime publication on reports, and delete_my_data(). Not yet pasted into a live Supabase project — human must apply via supabase/README.md, verify RLS/Realtime in the dashboard, fill .env, and rerun `pytest -q tests/test_supabase_integration.py`. [VERIFY] auth.role() inside the before-insert trigger correctly distinguishes service_role (seed) from anon/authenticated (user) inserts — confirm once the integration test runs live.
