"""FastAPI app (Prompt 4 task 4). Only this module builds app.py, per
CLAUDE.md §5: "Prompts 6-8 plug in through their own modules and do not edit
app.py."

Endpoints (CLAUDE.md §5): POST /route, GET /health, GET /profiles,
GET /conditions, GET /buildings, GET /bottlenecks.

Privacy (CLAUDE.md §7/§11): never log request bodies or coordinates, never
return the Supabase service role key (or any key) in a response.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

load_dotenv()  # stack list (CLAUDE.md §2) names python-dotenv for the routing
# service specifically; app.py is the service's entrypoint (`uvicorn
# service.app:app`), so it's the one place that should load .env -- no other
# service module does this (they just read os.getenv directly).

from service import ai as ai_module
from service import explain, reports as reports_module
from service.conditions import get_conditions
from service.graph_store import (
    BuildingAmbiguousError,
    BuildingNotFoundError,
    GraphStore,
    get_store,
)
from service.layers import router as layers_router
from service.profiles import PROFILE_PRESETS, resolve_preferences
from service.reports import edge_effects_for, get_active_reports, report_edges
from service.router import find_routes

REPO_ROOT = Path(__file__).resolve().parent.parent
BOTTLENECKS_PATH = REPO_ROOT / "cornell_data" / "graph" / "bottlenecks.json"

POINT_SNAP_LIMIT_M = 100.0

app = FastAPI(title="Accessible Campus Navigator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(layers_router)
app.include_router(ai_module.router)


# ---------------------------------------------------------------------------
# Request models (CLAUDE.md §5 + task's include_seed_reports addition)
# ---------------------------------------------------------------------------


class LocationIn(BaseModel):
    building: str | None = None
    lat: float | None = None
    lon: float | None = None

    @model_validator(mode="after")
    def _check_shape(self):
        has_point = self.lat is not None and self.lon is not None
        has_building = bool(self.building)
        if has_point == has_building:  # both set, or neither set
            raise ValueError(
                "location must have either 'building' or both 'lat' and 'lon' (not both, not neither)"
            )
        return self


class RouteRequest(BaseModel):
    origin: LocationIn
    destination: LocationIn
    # F1 (P13-lite): profiles is no longer required -- a priorities-only
    # request (no named profile) defaults to "fastest" in resolve_preferences.
    profiles: list[str] = Field(default_factory=list)
    preferences: dict | None = None
    # F1 (P13-lite): {chip_id: "essential"|"important"|"nice"} -- see
    # docs/API.md for the chip table.
    priorities: dict | None = None
    adjustments: list[str] = Field(default_factory=list)
    conditions: dict | None = None
    alternatives: int = 3
    include_seed_reports: bool = True


# ---------------------------------------------------------------------------
# Small JSON-safety helper (math.inf isn't valid JSON)
# ---------------------------------------------------------------------------


def _safe_num(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isinf(v):
        return None
    return v


# ---------------------------------------------------------------------------
# Endpoint-resolution helpers (task 2)
# ---------------------------------------------------------------------------


def _report_source(r):
    return r.get("source") if isinstance(r, dict) else getattr(r, "source", None)


def _resolve_building_or_point(store: GraphStore, loc: LocationIn) -> tuple[list, bool]:
    """Returns (candidate_node_ids, is_building). Raises HTTPException(422)
    on any resolution failure (ambiguous/unknown building, point too far).

    For a building, returns ALL of its routable (degree > 0) entrance node
    ids, unfiltered by accessibility -- callers apply the
    accessible-entrance preference themselves (it differs for origin vs.
    destination, and for destination it also depends on
    require_accessible_entrance).
    """
    if loc.building:
        try:
            canonical = store.match_building(loc.building)
        except BuildingNotFoundError as e:
            raise HTTPException(422, detail=f"no building matches {e.query!r}")
        except BuildingAmbiguousError as e:
            raise HTTPException(
                422,
                detail={
                    "error": f"{len(e.candidates)} buildings match {e.query!r}",
                    "candidates": e.candidates,
                },
            )
        all_entrances = store.building_index.get(canonical, [])
        routable = [n for n in all_entrances if store.graph.degree(n) > 0]
        if not routable:
            raise HTTPException(
                422, detail=f"building {canonical!r} has no routable entrance"
            )
        return routable, True

    try:
        node, dist_m = store.nearest_node(loc.lat, loc.lon)
    except RuntimeError as e:
        raise HTTPException(500, detail=str(e))
    if node is None or dist_m > POINT_SNAP_LIMIT_M:
        raise HTTPException(
            422,
            detail=(
                f"point is {dist_m:.0f} m from the nearest path node, "
                f"more than the {POINT_SNAP_LIMIT_M:.0f} m limit"
            ),
        )
    return [node], False


def _pick_origin_entrance(G, routable_nodes: list) -> list:
    """A single representative entrance for a building used as an origin.

    Deterministic preference order: accessible > unknown > not_accessible,
    tie-broken by door_id (then node id) so repeated calls are stable. The
    spec (CLAUDE.md §5) only requires the *destination* to respect
    require_accessible_entrance; origin building resolution just needs one
    reasonable, routable starting point -- see the design note in the P4
    report for why a single node (not a multi-origin search) was chosen.
    """

    def rank(n):
        access = G.nodes[n].get("access")
        order = {"accessible": 0, "unknown": 1, "not_accessible": 2}.get(access, 1)
        return (order, str(G.nodes[n].get("door_id") or n))

    return [sorted(routable_nodes, key=rank)[0]]


def _filter_destination_entrances(G, routable_nodes: list, prefs) -> tuple[list, list[str]]:
    """Destinations: accessible-only when required; otherwise prefer
    accessible, then unknown; fall back (with a warning) rather than
    returning nothing. Nodes with no edges are already excluded by the
    caller (see _resolve_building_or_point)."""
    accessible = [n for n in routable_nodes if G.nodes[n].get("access") == "accessible"]
    if prefs.require_accessible_entrance:
        if accessible:
            return accessible, []
        return routable_nodes, [
            "No accessible entrance found for this destination; "
            "falling back to a non-accessible entrance and this route will carry violations."
        ]

    if accessible:
        return accessible, []
    unknown = [n for n in routable_nodes if G.nodes[n].get("access") == "unknown"]
    if unknown:
        return unknown, []
    return routable_nodes, [
        "No accessible or unverified-access entrance found for this destination; "
        "routing to a non-accessible entrance."
    ]


def _enforce_distance_tolerance(routes: list, baseline_length_ft: float, tolerance: float, warnings: list) -> list:
    """Enforce distance_tolerance in the app layer (router.py does not).

    `routes` is assumed sorted best-first (router.find_routes returns them
    in that order). The best route is never dropped, even if it alone
    exceeds the tolerance (there may be no shorter accessible option);
    subsequent alternatives longer than tolerance x baseline are dropped.
    """
    if not routes:
        return routes
    threshold = baseline_length_ft * tolerance
    kept = [routes[0]]
    if routes[0].length_ft > threshold + 1e-6 and baseline_length_ft > 0:
        warnings.append(
            f"The best available route is {routes[0].length_ft:.0f} ft, "
            f"{routes[0].length_ft / baseline_length_ft:.1f}x the shortest unconstrained "
            f"route ({baseline_length_ft:.0f} ft) and beyond this profile's "
            f"{tolerance:.1f}x distance tolerance; showing it anyway."
        )
    for r in routes[1:]:
        if r.length_ft <= threshold + 1e-6:
            kept.append(r)
    return kept


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health(store: GraphStore = Depends(get_store)):
    supabase_configured = bool(
        os.getenv("SUPABASE_URL")
        and os.getenv("SUPABASE_ANON_KEY")
        and os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )
    return {
        "graph_loaded": True,
        "nodes": store.graph.number_of_nodes(),
        "edges": store.graph.number_of_edges(),
        "supabase_configured": supabase_configured,
    }


@app.get("/profiles")
def profiles_endpoint():
    out = {}
    for name, p in PROFILE_PRESETS.items():
        out[name] = {
            "avoid_stairs": p.avoid_stairs,
            "max_slope_pct": _safe_num(p.max_slope_pct),
            "max_cross_slope_pct": _safe_num(p.max_cross_slope_pct),
            "min_width_ft": p.min_width_ft,
            "prefer_lit": p.prefer_lit,
            "surface_sensitivity": p.surface_sensitivity,
            "distance_tolerance": p.distance_tolerance,
            "require_curb_cuts": p.require_curb_cuts,
            "require_accessible_entrance": p.require_accessible_entrance,
            "ramp_required_above_pct": _safe_num(p.ramp_required_above_pct),
            "walking_speed_ftps": p.walking_speed_ftps,
        }
    return {"profiles": out}


@app.get("/conditions")
def conditions_endpoint(darkness: str = "auto", ice: str = "auto"):
    c = get_conditions({"darkness": darkness, "ice": ice})
    return {
        "darkness": c.darkness,
        "ice": c.ice,
        "source": c.source,
        "warnings": c.warnings,
    }


@app.get("/buildings")
def buildings_endpoint(store: GraphStore = Depends(get_store)):
    return {"buildings": sorted(store.building_index.keys())}


@app.get("/bottlenecks")
def bottlenecks_endpoint():
    if not BOTTLENECKS_PATH.exists():
        return {"available": False}
    with open(BOTTLENECKS_PATH) as f:
        return json.load(f)


@app.post("/route")
def route_endpoint(req: RouteRequest, store: GraphStore = Depends(get_store)):
    try:
        prefs = resolve_preferences(req.profiles, req.preferences, req.adjustments, req.priorities)
    except ValueError as e:
        raise HTTPException(422, detail=str(e))

    conditions = get_conditions(req.conditions or {})
    warnings: list[str] = list(conditions.warnings)

    # F1 (P13-lite): "well_lit" at "essential" can't be a hard limit (no
    # per-edge "is lit" cutoff exists) -- resolve_preferences instead applies
    # the heaviest lighting penalty scale; surface that tradeoff here.
    if req.priorities and req.priorities.get("well_lit") == "essential":
        warnings.append(
            "Well-lit paths can't be guaranteed as a hard requirement; "
            "routing with the strongest preference for lit paths instead."
        )

    # --- reports: seed filtering (task 4), then edge effects (task 5) ---
    active_reports = get_active_reports()
    if not req.include_seed_reports:
        active_reports = [r for r in active_reports if _report_source(r) != "seed"]
    edge_effects = edge_effects_for(store.graph, active_reports)
    report_edge_map = report_edges(store.graph, active_reports)
    if reports_module.last_error:
        warnings.append("Live reports unavailable; routing without them.")

    # --- resolve origin (single representative entrance, or a snapped point) ---
    origin_pool, origin_is_building = _resolve_building_or_point(store, req.origin)
    origin_candidates = (
        _pick_origin_entrance(store.graph, origin_pool) if origin_is_building else origin_pool
    )
    origin_node = origin_candidates[0]

    # --- resolve destination (candidate set shared between this profile and
    # the neutral "fastest" baseline used for steps_avoided / reports_avoided
    # / time-diff / distance_tolerance) ---
    dest_pool, dest_is_building = _resolve_building_or_point(store, req.destination)
    if dest_is_building:
        dest_candidates, dest_warnings = _filter_destination_entrances(
            store.graph, dest_pool, prefs
        )
        fastest_dest_candidates, _ = _filter_destination_entrances(
            store.graph, dest_pool, PROFILE_PRESETS["fastest"]
        )
    else:
        dest_candidates = dest_pool
        fastest_dest_candidates = dest_pool
        dest_warnings = []
    warnings.extend(dest_warnings)

    alternatives = max(1, req.alternatives or 3)

    # Neutral baseline route: always the raw "fastest" preset (no caller
    # overrides/adjustments), so steps_avoided/reports_avoided/time-diff and
    # distance_tolerance all compare against the same unconstrained shortest
    # path, not a route the caller's own overrides already reshaped.
    fastest_routes = find_routes(
        store.graph,
        origin_node,
        fastest_dest_candidates,
        PROFILE_PRESETS["fastest"],
        conditions,
        edge_effects=edge_effects,
        k=1,
    )
    fastest_route = fastest_routes[0] if fastest_routes else None

    routes = find_routes(
        store.graph,
        origin_node,
        dest_candidates,
        prefs,
        conditions,
        edge_effects=edge_effects,
        k=alternatives,
    )
    if not routes:
        raise HTTPException(
            422, detail="no usable route found between the given origin and destination"
        )

    baseline_length = fastest_route.length_ft if fastest_route is not None else routes[0].length_ft
    routes = _enforce_distance_tolerance(routes, baseline_length, prefs.distance_tolerance, warnings)

    route_outputs = []
    for i, r in enumerate(routes):
        stats = explain.compute_stats(
            store.graph, r, fastest_route, prefs, store.dark_threshold, report_edge_map
        )
        route_outputs.append(
            {
                "id": f"route-{i + 1}",
                "geometry": store.route_geometry(r),
                "stats": stats,
                "violations": explain.build_violations(store.graph, r, prefs),
                "explanation": explain.build_explanation(
                    store.graph, r, stats, prefs, fastest_route
                ),
                "segments": explain.build_segments(store.graph, r),
            }
        )

    return {
        "conditions": {
            "darkness": conditions.darkness,
            "ice": conditions.ice,
            "source": conditions.source,
        },
        "warnings": warnings,
        "routes": route_outputs,
    }
