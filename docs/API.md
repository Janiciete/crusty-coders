# Routing Service API

Base URL: `http://localhost:8000` (CORS allows `http://localhost:5173`, the UI dev server). All request/response bodies are JSON.

**Routes are guidance, not ADA certification.** The UI must display this (or equivalent wording) somewhere the user will see it before or alongside route results — see CLAUDE.md §7/§11 and plan §11.3.

No endpoint ever returns a Supabase key (anon or service role) or any other secret. `GET /health` reports whether Supabase is configured as a boolean only.

---

## POST /route

Computes up to `alternatives` routes from an origin to a destination, personalized to one or more profiles, and explains each one.

### Request body

```json
{
  "origin": {"building": "Keeton House"},
  "destination": {"building": "Goldwin Smith Hall"},
  "profiles": ["wheelchair"],
  "preferences": {"distance_tolerance": 2.0},
  "adjustments": ["in_a_hurry"],
  "conditions": {"darkness": "auto", "ice": "auto"},
  "alternatives": 3,
  "include_seed_reports": true
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `origin` | object | yes | Either `{"building": "<name>"}` or `{"lat": <float>, "lon": <float>}`. Exactly one form; both or neither is a 422. |
| `destination` | object | yes | Same shape as `origin`. |
| `profiles` | string[] | no | One or more of `wheelchair`, `injured`, `low_vision`, `night_walk`, `fastest`. Combining rules: union of hard limits, max of each penalty, slowest walking speed (CLAUDE.md §6). Default: `["fastest"]` when omitted/empty (so a `priorities`-only request still resolves). |
| `preferences` | object | no | Overrides for any key in CLAUDE.md §5's preferences JSON (`avoid_stairs`, `max_slope_pct`, `max_cross_slope_pct`, `min_width_ft`, `prefer_lit`, `surface_sensitivity`, `distance_tolerance`, `require_curb_cuts`, `require_accessible_entrance`, `ramp_required_above_pct`). Unknown keys are a 422. |
| `priorities` | object | no | `{chip_id: "essential"\|"important"\|"nice"}` -- see "Priorities (preference chips)" below. Unknown chip ids or levels are a 422. Applied on top of `profiles`/`preferences`, so it can only add restrictions, never loosen them. |
| `adjustments` | string[] | no | Any of `in_a_hurry`, `carrying_items`, `walking_alone`. Unknown values are a 422. |
| `conditions` | object | no | `{"darkness": "auto"\|"on"\|"off", "ice": "auto"\|"on"\|"off"}`. Defaults to `"auto"` for both (currently resolves to `false`/`false` until Prompt 7 wires in NWS + astral). |
| `alternatives` | int | no | Default 3. Clamped to at least 1. |
| `include_seed_reports` | bool | no | Default `true`. When `false`, reports with `source == "seed"` (the historical Cornell trip-hazard seed data) are filtered out **before** matching reports to edges, so seed reports never affect routing or `reports_avoided` for that request. |

A location given as a building name is matched case-insensitively: an exact match wins; otherwise a unique substring match is used; if several buildings match, the request is rejected (422) with the candidate names listed. A location given as a point is snapped to the nearest graph node; if that node is more than 100 m away, the request is rejected (422).

### Priorities (preference chips)

F1 (P13-lite): the UI's five preference chips, each with an importance level. `"essential"` is a hard limit wherever one exists (the route will never use a blocked edge, same as the equivalent `preferences` override); `"important"`/`"nice"` instead scale that penalty term in the routing cost, so the route avoids it where reasonably possible without being forbidden from using it.

| Chip id | Essential | Important | Nice |
|---|---|---|---|
| `avoid_stairs` | `avoid_stairs = true` (hard) | stair cost ×3 | stair cost ×1.5 |
| `avoid_steep` | `max_slope_pct = 8.33`, `ramp_required_above_pct = 5.0` (hard; the estimated-slope guard, CLAUDE.md §6/P4-fix2, still applies), plus `min_width_ft`/`max_cross_slope_pct` taken from the `wheelchair` preset (F3 Task 0) -- `min_width_ft` is a real hard limit; `max_cross_slope_pct` has no hard-limit check anywhere today (same as the `wheelchair` profile itself), so it only affects explanation/stat wording | slope penalty ×3 | slope penalty ×1.5 |
| `curb_cuts` | `require_curb_cuts = true` (hard) | curb-cut penalty ×3 | curb-cut penalty ×1.5 |
| `accessible_entrance` | `require_accessible_entrance = true` (hard) | no routing-cost effect -- destination entrances are already preferred accessible-first by default | same as important |
| `well_lit` | `prefer_lit = true` + lighting penalty ×5. Can't be a true hard limit (there's no per-edge "is lit" cutoff), so the response's top-level `warnings` includes a note explaining this whenever `well_lit` is `"essential"`. | `prefer_lit = true` + lighting penalty ×3 | `prefer_lit = true` + lighting penalty ×1.5 |

A request with no `profiles` and no `priorities` behaves exactly like `profiles: ["fastest"]`. The map Layers feature (a separate `/layers` endpoint showing e.g. lighting/slope overlays) was cut for time; there is no such endpoint.

### Response body

```json
{
  "conditions": {"darkness": false, "ice": false, "source": {"darkness": "auto_stub", "ice": "auto_stub"}},
  "warnings": [],
  "routes": [
    {
      "id": "route-1",
      "geometry": {"type": "LineString", "coordinates": [[-76.4858, 42.4479], [-76.4851, 42.4482]]},
      "stats": {
        "distance_ft": 4521.4,
        "est_time_min": 22.8,
        "steps_avoided": 59,
        "max_slope_pct": 4.6,
        "max_cross_slope_pct": 5.6,
        "pct_lit": 8.8,
        "destination_entrance": {
          "door_id": "2013-11",
          "building": "Goldwin Smith Hall",
          "access": "accessible",
          "auto_opener": false
        },
        "reports_avoided": 0,
        "unverified_segments": 35,
        "estimated_segments": 0
      },
      "violations": [],
      "explanation": [
        "Avoids 8 staircases (59 steps)",
        "Maximum slope: 4.6% (within the 5% ADA walkway limit)",
        "Maximum cross slope: 5.6% (exceeds the 2% ADA limit)",
        "Ends at an accessible entrance",
        "9% of the route is lit",
        "15.2 minutes longer than the shortest route",
        "Includes 35 unverified/inferred segments"
      ],
      "segments": [
        {
          "edge_id": "E004821",
          "slope_pct": 2.3,
          "slope_source": "cornell_survey",
          "cross_slope_pct": 1.1,
          "date_surveyed": "2026-06-10",
          "verified": true
        }
      ]
    }
  ]
}
```

`geometry` is always a GeoJSON `LineString` in EPSG:4326, oriented from origin to destination, built from each edge's own surveyed geometry where available (falling back to a straight line between the two endpoint nodes otherwise).

`violations` is non-empty only when the fallback rule had to relax a hard limit (`used_fallback` internally); each entry is `{"edge_id", "code", "message"}` where `message` names the specific segment and the standard it fails, e.g. `"segment E005493: stairs (0 steps), not usable with this profile's avoid-stairs limit"` or `"segment E001244: 11% slope, exceeds the 8.33% ADA ramp limit"`. The wording never says "safe"; estimated (lidar) slopes and inferred/unverified edges are called out as "estimated" / "unverified", never stated as fact.

`warnings` (top-level) can include things like:
- A destination-entrance fallback notice, e.g. `"No accessible entrance found for this destination; falling back to a non-accessible entrance and this route will carry violations."`
- A distance-tolerance notice when even the best route is beyond this profile's tolerance vs. the unconstrained shortest path, e.g. `"The best available route is 4521 ft, 2.3x the shortest unconstrained route (1960 ft) and beyond this profile's 3.0x distance tolerance; showing it anyway."`
- `"Live reports unavailable; routing without them."` when the Supabase reports read failed (`service/reports.py`'s `last_error` is set).

### Error cases (422)

| Condition | Example `detail` |
|---|---|
| Unknown profile name | `"unknown profile: 'bogus'"` |
| Unknown preference override key | `"unknown preference key: 'bogus_key'"` |
| Unknown adjustment name | `"unknown adjustment: 'bogus_adjustment'"` |
| `origin`/`destination` has both `building` and `lat`/`lon`, or neither | pydantic validation error (`"location must have either 'building' or both 'lat' and 'lon' ..."`) |
| Building name matches nothing | `"no building matches 'Totally Fake Hall'"` |
| Building name matches more than one building | `{"error": "2 buildings match 'nnex'", "candidates": ["Annex", "Annex2"]}` |
| Building has entrances, but none with any graph edge | `"building 'X' has no routable entrance"` |
| Point is more than 100 m from the path network | `"point is 143 m from the nearest path node, more than the 100 m limit"` |
| No usable route exists at all (even via fallback) between origin and destination | `"no usable route found between the given origin and destination"` |

---

## GET /health

```json
{"graph_loaded": true, "nodes": 4070, "edges": 6092, "supabase_configured": false}
```

`supabase_configured` is `true` only when `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `SUPABASE_SERVICE_ROLE_KEY` are all set to non-empty values; the values themselves are never returned.

## GET /profiles

Returns the resolved defaults for each of the five profiles (plan §8.3), as the preference fields from CLAUDE.md §5 plus `walking_speed_ftps`. `Infinity`-valued fields (e.g. `injured`'s `max_slope_pct`, which has no hard limit) are serialized as `null`, since JSON has no `Infinity` literal.

```json
{
  "profiles": {
    "wheelchair": {
      "avoid_stairs": true,
      "max_slope_pct": 8.33,
      "max_cross_slope_pct": 2.0,
      "min_width_ft": 3.0,
      "prefer_lit": false,
      "surface_sensitivity": 2,
      "distance_tolerance": 3.0,
      "require_curb_cuts": true,
      "require_accessible_entrance": true,
      "ramp_required_above_pct": 5.0,
      "walking_speed_ftps": 3.3
    },
    "injured": {"...": "...", "max_slope_pct": null}
  }
}
```

## GET /conditions

Query params `darkness` and `ice`, each `"auto"` (default), `"on"`, or `"off"`.

```
GET /conditions?darkness=on&ice=off
```

```json
{"darkness": true, "ice": false, "source": {"darkness": "override_on", "ice": "override_off"}, "warnings": []}
```

## GET /buildings

```json
{"buildings": ["A D White House", "Alice H Cook House", "...", "Goldwin Smith Hall"]}
```

Alphabetically sorted building names drawn from the entrance nodes in the loaded graph (including buildings whose entrances currently have no routable edges; `/route` will reject those with a 422 if actually requested).

## GET /bottlenecks

Serves `cornell_data/graph/bottlenecks.json` (produced by the Prompt 8 bottleneck finder) verbatim if it exists. If the file is missing (as it is until P8 runs):

```json
{"available": false}
```

---

## Notes for the UI team

- Never send coordinates in a URL query string; `POST /route` is the only place lat/lon travel, and only in the body.
- `reports_avoided` and anything else derived from live reports will read `0`/empty until Prompt 6 lands (`service/reports.py` is still a stub that returns `[]`/`{}`); this is expected, not a bug.
- `slope_source: "unknown"` is expected on most edges until Prompt 2 (lidar fill) runs; it never implies an ADA slope violation (unknown slopes are never hard-limited), only a generic "unverified" cost penalty and the `unverified_segments` count.
- Always show the estimated/unverified labeling from `explanation` and `segments[].verified` — don't infer "safe" from the absence of a violation. Per CLAUDE.md §7/§11: use "well-lit route", never "safe route"; no crime data; routes are guidance, not ADA certification.
