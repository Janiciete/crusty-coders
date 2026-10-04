"""Bottleneck finder (plan §3.2, §8.7; CLAUDE.md §6; Prompt 8).

Samples a fixed number of accessible-entrance-to-accessible-entrance trips,
compares the unconstrained ("fastest") route against the strict Wheelchair
route for each, and ranks the edges whose Wheelchair hard-limit violations
are most responsible for those trips being blocked.

Reuses `service.router.find_routes`, `service.cost.hard_limit_violations`,
and `service.profiles.resolve_preferences` as-is (CLAUDE.md working rule:
don't reimplement routing). For the per-violation "standard text" shown on
each top segment, this module also reuses `service.explain._violation_message`
(the same function `/route` uses to build its own violation explanations)
so the wording is identical everywhere in the app; this is a read-only
import, not an edit to explain.py.

Nothing here does I/O; `scripts/run_bottlenecks.py` is the only caller that
touches the filesystem.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timezone

import networkx as nx
from pyproj import Transformer

from service.conditions import Conditions
from service.cost import hard_limit_violations
from service.explain import _violation_message
from service.profiles import ESTIMATED_SLOPE_HARD_PCT, SLOPE_SOURCE_ESTIMATE, resolve_preferences
from service.router import find_routes

# CLAUDE.md §2 / service/graph_store.py: graph.pkl node x/y and edge
# geometry are in EPSG:2261 (NY State Plane Central, US survey feet); the
# API's own outputs are reprojected to EPSG:4326. Mirrored here (not
# imported from graph_store) since GraphStore also builds an STRtree and
# building index this module has no use for.
WORK_CRS = "EPSG:2261"
WGS84 = "EPSG:4326"

_to_wgs84 = Transformer.from_crs(WORK_CRS, WGS84, always_xy=True)

CROSS_SLOPE_HARD_PCT = 2.0  # ADA cross slope limit (CLAUDE.md §6); not a
# routing hard limit in cost.py today, so this is checked separately and
# reported as a secondary/informational violation only (plan §8.7 /
# this prompt's spec), never counted toward ranking or fix impact.

NOTE = "Based on sampled trips; guidance for prioritization, not an ADA audit."


# ---------------------------------------------------------------------------
# Small graph-shape helpers (mirrors service/explain.py's private helpers;
# duplicated rather than imported since those are the module's own private
# conventions, not a shared contract).
# ---------------------------------------------------------------------------


def _edge_attrs(G, edge_key) -> dict:
    a, b, k = edge_key
    data = G.get_edge_data(a, b, k)
    if data is None:
        data = G.get_edge_data(b, a, k)
    return data or {}


def _edge_id(edge_key, attrs: dict) -> str:
    a, b, k = edge_key
    return attrs.get("edge_id") or f"{a}|{b}|{k}"


def _midpoint_lonlat(G, edge_key, attrs: dict):
    a, b, _k = edge_key
    geom = attrs.get("geometry")
    if geom is not None:
        mid = geom.interpolate(0.5, normalized=True)
        x, y = mid.x, mid.y
    else:
        na, nb = G.nodes.get(a, {}), G.nodes.get(b, {})
        if "x" in na and "y" in na and "x" in nb and "y" in nb:
            x = (na["x"] + nb["x"]) / 2.0
            y = (na["y"] + nb["y"]) / 2.0
        else:
            return None, None
    lon, lat = _to_wgs84.transform(x, y)
    return lat, lon


def _cross_slope_violation(attrs: dict) -> dict | None:
    cross = attrs.get("cross_slope_pct")
    if cross is None or cross <= CROSS_SLOPE_HARD_PCT:
        return None
    return {
        "code": "cross_slope_over_max",
        "message": (
            f"{cross:.1f}% cross slope, exceeds the "
            f"{CROSS_SLOPE_HARD_PCT:.2f}% ADA cross-slope limit"
        ),
    }


def _make_compliant(attrs: dict, codes: list, prefs) -> dict:
    """Mutate `attrs` in place so none of `codes` would fire again under
    `hard_limit_violations(attrs, prefs)`; returns the original values so
    the caller can restore them afterward.

    Only touches the minimal fields needed per code (CLAUDE.md §5 field
    names), e.g. stairs -> is_stairs=False, slope_over_max -> cap slope_pct
    at the profile's limit (and set ramp=True defensively, since a capped
    slope just at the limit could otherwise still trip slope_needs_ramp).
    """
    saved: dict = {}

    def _save(key):
        if key not in saved:
            saved[key] = attrs.get(key)

    for code in codes:
        if code == "stairs":
            _save("is_stairs")
            attrs["is_stairs"] = False
        elif code == "slope_over_max":
            _save("slope_pct")
            _save("ramp")
            attrs["slope_pct"] = min(attrs.get("slope_pct") or 0.0, prefs.max_slope_pct)
            attrs["ramp"] = True
        elif code == "slope_needs_ramp":
            _save("ramp")
            attrs["ramp"] = True
        elif code == "slope_over_max_estimated":
            _save("slope_pct")
            _save("ramp")
            attrs["slope_pct"] = min(attrs.get("slope_pct") or 0.0, ESTIMATED_SLOPE_HARD_PCT)
            attrs["ramp"] = True
        elif code == "curb_cuts":
            _save("curb_cuts")
            attrs["curb_cuts"] = 2
        elif code == "width":
            _save("width_ft")
            attrs["width_ft"] = prefs.min_width_ft

    return saved


def _restore(attrs: dict, saved: dict) -> None:
    attrs.update(saved)


def _is_blocked(wroutes: list, fastest_length_ft: float) -> bool:
    if not wroutes:
        return True
    wroute = wroutes[0]
    if wroute.used_fallback:
        return True
    return wroute.length_ft > 1.5 * fastest_length_ft


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def _sample_trips(G, fastest_prefs, conditions, n_pairs: int, seed: int):
    """Sample up to n_pairs (origin, destination, fastest_route) trips.

    Pool: accessible entrance nodes with degree > 0 (CLAUDE.md §6), origin
    != destination, different buildings. A candidate pair is discarded
    (and another drawn) if the two entrances turn out to not be mutually
    reachable at all (the unconstrained/"fastest" profile has no hard
    limits, so this only happens across disconnected graph components, a
    data-connectivity gap rather than an accessibility one) -- such a pair
    isn't a meaningful "blocked vs. unconstrained" comparison.
    """
    entrances = sorted(
        n for n, d in G.nodes(data=True) if d.get("access") == "accessible" and G.degree(n) > 0
    )

    rng = random.Random(seed)
    trips = []
    if len(entrances) < 2:
        return trips

    max_attempts = max(n_pairs * 50, 2000)
    attempts = 0
    while len(trips) < n_pairs and attempts < max_attempts:
        attempts += 1
        o, d = rng.sample(entrances, 2)
        bo = G.nodes[o].get("building")
        bd = G.nodes[d].get("building")
        if not bo or not bd or bo == bd:
            continue
        fastest_routes = find_routes(G, o, [d], fastest_prefs, conditions, k=1)
        if not fastest_routes:
            continue
        trips.append((o, d, fastest_routes[0]))

    return trips


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def find_bottlenecks(
    G,
    n_pairs: int = 2000,
    seed: int = 42,
    top_n: int = 10,
    on_progress=None,
) -> dict:
    """Rank the segments that block the most sampled Wheelchair trips.

    `on_progress`, if given, is called with the number of trips processed
    so far after each one (used by scripts/run_bottlenecks.py to print
    progress every 200 pairs; this module itself never prints).
    """
    fastest_prefs = resolve_preferences(["fastest"], {}, [])
    wheelchair_prefs = resolve_preferences(["wheelchair"], {}, [])
    conditions = Conditions()  # neutral: no darkness/ice, so results are
    # purely about the graph's standing accessibility, not today's weather.

    trips = _sample_trips(G, fastest_prefs, conditions, n_pairs, seed)
    pairs_sampled = len(trips)

    blocked_trips: list[dict] = []  # {"origin","destination","fastest_length_ft"}
    edge_trip_map: dict = defaultdict(set)  # edge_key -> set(blocked_trip index)
    edge_codes: dict = {}  # edge_key -> hard-limit codes (vs. wheelchair_prefs)

    for i, (o, d, froute) in enumerate(trips):
        wroutes = find_routes(G, o, [d], wheelchair_prefs, conditions, k=1)
        if _is_blocked(wroutes, froute.length_ft):
            trip_idx = len(blocked_trips)
            blocked_trips.append(
                {"origin": o, "destination": d, "fastest_length_ft": froute.length_ft}
            )
            for ek in froute.edge_keys:
                attrs = _edge_attrs(G, ek)
                codes = hard_limit_violations(attrs, wheelchair_prefs)
                if codes:
                    edge_trip_map[ek].add(trip_idx)
                    edge_codes.setdefault(ek, codes)
        if on_progress is not None:
            on_progress(i + 1)

    ranked = sorted(
        edge_trip_map.items(), key=lambda kv: (-len(kv[1]), repr(kv[0]))
    )
    top_ranked = ranked[:top_n]

    top_segments = []
    for edge_key, trip_idx_set in top_ranked:
        attrs = _edge_attrs(G, edge_key)
        edge_id = _edge_id(edge_key, attrs)
        codes = edge_codes[edge_key]

        violations = [
            {"code": c, "message": _violation_message(attrs, c, wheelchair_prefs, edge_id)}
            for c in codes
        ]
        cross_v = _cross_slope_violation(attrs)
        if cross_v is not None:
            violations.append(cross_v)

        # --- fix impact: recompute only this edge's blocked trips, with
        # the edge treated as compliant. ---
        saved = _make_compliant(attrs, codes, wheelchair_prefs)
        unblocked = 0
        for trip_idx in trip_idx_set:
            bt = blocked_trips[trip_idx]
            wroutes2 = find_routes(
                G, bt["origin"], [bt["destination"]], wheelchair_prefs, conditions, k=1
            )
            if not _is_blocked(wroutes2, bt["fastest_length_ft"]):
                unblocked += 1
        _restore(attrs, saved)

        pct_unblocked = (unblocked / pairs_sampled * 100.0) if pairs_sampled else 0.0
        lat, lon = _midpoint_lonlat(G, edge_key, attrs)
        flags = attrs.get("flags") or []

        top_segments.append(
            {
                "edge_id": edge_id,
                "lat": lat,
                "lon": lon,
                "length_ft": attrs.get("length_ft", 0.0),
                "kind": attrs.get("kind"),
                "violations": violations,
                "trips_blocked": len(trip_idx_set),
                "pct_trips_unblocked_if_fixed": pct_unblocked,
                "slope_source": attrs.get("slope_source"),
                "estimated": attrs.get("slope_source") == SLOPE_SOURCE_ESTIMATE,
                "retagged_stair": "retagged_stair" in flags,
            }
        )

    # --- top5 combined fix impact: fix the top 5 edges together, recheck
    # every blocked trip (not just the ones attributed to those 5 edges --
    # fixing several edges at once can also clear a trip that needed more
    # than one of them fixed, or a fallback trip with no single identified
    # blocking edge). ---
    top5 = top_ranked[:5]
    if top5 and blocked_trips:
        saved_all = []
        for edge_key, _ in top5:
            attrs = _edge_attrs(G, edge_key)
            saved_all.append((attrs, _make_compliant(attrs, edge_codes[edge_key], wheelchair_prefs)))
        try:
            unblocked_combined = 0
            for bt in blocked_trips:
                wroutes2 = find_routes(
                    G, bt["origin"], [bt["destination"]], wheelchair_prefs, conditions, k=1
                )
                if not _is_blocked(wroutes2, bt["fastest_length_ft"]):
                    unblocked_combined += 1
        finally:
            for attrs, saved in saved_all:
                _restore(attrs, saved)
        top5_combined_pct = (unblocked_combined / pairs_sampled * 100.0) if pairs_sampled else 0.0
    else:
        top5_combined_pct = 0.0

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "pairs_sampled": pairs_sampled,
        "blocked_trips": len(blocked_trips),
        "top_segments": top_segments,
        "top5_combined_pct": top5_combined_pct,
        "note": NOTE,
    }
