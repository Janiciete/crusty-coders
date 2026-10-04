"""Live reports (Supabase-backed). Implements CLAUDE.md §5 / §6 (Prompt 6).

`get_active_reports` reads the `active_reports` Supabase view with the
**anon** key (least privilege; the service key is reserved for the seed
loader). `edge_effects_for` and `report_edges` spatially match reports to
graph edges within ~20 m (CLAUDE.md §6) and translate them into either a
hard "remove" or a cost-penalty multiplier, per the effects table below.

Spatial matching note: `graph.pkl` node `x`/`y` and edge `geometry` are
left in the pipeline's working CRS (EPSG:2261, NY State Plane Central, US
feet) -- only the exported `edges.geojson`/`nodes.geojson` GeoDataFrames
are reprojected to WGS84 before the graph itself is pickled. This is the
same assumption service/graph_store.py documents and relies on for point
snapping / route geometry output, so it's taken as confirmed here rather
than re-flagged [VERIFY]. Active-report lat/lon (WGS84, from the
`active_reports` view) are reprojected into that same CRS before matching.
"""

from __future__ import annotations

import os
import time

from pyproj import Transformer
from shapely import LineString, Point, STRtree

GRAPH_CRS = "EPSG:2261"
WGS84 = "EPSG:4326"

# Matches service/graph_store.py:FT_PER_METER for consistency across the
# codebase (negligible difference vs. the US survey foot EPSG:2261 is
# technically defined in, well under the 20 m match radius's precision).
_FEET_PER_METER = 3.280839895
MATCH_RADIUS_M = 20.0
MATCH_RADIUS_FT = MATCH_RADIUS_M * _FEET_PER_METER

_to_graph_crs = Transformer.from_crs(WGS84, GRAPH_CRS, always_xy=True)

# Report types that physically block an edge (CLAUDE.md §6). A seed-sourced
# report of one of these types never hard-removes (seed data is historical,
# not a live confirmed closure); it instead falls back to the heaviest
# defined penalty multiplier below. In practice this branch shouldn't fire
# today -- the seed loader (scripts/load_seed_reports.py) only ever writes
# type="trip_hazard" for seed rows -- but the rule is stated generically in
# CLAUDE.md §6, so it's implemented defensively.
REMOVE_TYPES = {"blocked_path", "construction"}
SEED_REMOVE_FALLBACK_MULTIPLIER = 3.0  # team default (not in the plan); same severity as "ice", the heaviest penalty below.

# Report types that only penalize (multiply) edge cost. Kept in one dict
# per CLAUDE.md §6's "Penalty weights live in one dict" convention, extended
# here to cover live-report multipliers specifically.
PENALTY_MULTIPLIERS = {
    "ice": 3.0,
    "too_steep": 2.0,
    "too_dark": 1.5,
    "trip_hazard": 1.5,
}

# Report types with no routing effect at all (still appear in report_edges
# for the reports_avoided stat).
NO_EFFECT_TYPES = {"too_loud", "crowded"}

# Set by get_active_reports() if the Supabase read fails, so app.py can add
# the warning "Live reports unavailable; routing without them." Reset to
# None on the next successful read.
last_error: str | None = None

_CACHE_TTL_SECONDS = 5.0
_cache_reports: list | None = None
_cache_time: float = 0.0
_client = None
_client_env: tuple | None = None


def _report_field(r, key):
    if isinstance(r, dict):
        return r.get(key)
    return getattr(r, key, None)


def _client_or_none():
    """Lazily build the anon-key client, rebuilding it if SUPABASE_URL/
    SUPABASE_ANON_KEY change (e.g. test monkeypatching) rather than reusing
    a stale client tied to old env values."""
    global _client, _client_env
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_ANON_KEY")
    if not url or not key:
        return None
    if _client is None or _client_env != (url, key):
        from supabase import create_client

        _client = create_client(url, key)
        _client_env = (url, key)
    return _client


def get_active_reports() -> list:
    """Read the `active_reports` view with the anon key, cached 5 s.

    Never raises: on any error (missing config, network failure, etc.)
    `last_error` is set and [] is returned.
    """
    global last_error, _cache_reports, _cache_time

    now = time.monotonic()
    if _cache_reports is not None and now - _cache_time < _CACHE_TTL_SECONDS:
        return _cache_reports

    try:
        client = _client_or_none()
        if client is None:
            raise RuntimeError("SUPABASE_URL / SUPABASE_ANON_KEY not configured")
        result = client.table("active_reports").select("*").execute()
        fetched = result.data or []
    except Exception as e:  # noqa: BLE001 -- must never raise into app.py
        last_error = str(e)
        return []

    last_error = None
    _cache_reports = fetched
    _cache_time = now
    return fetched


def _edge_geometry(u, v, attrs, G):
    geom = attrs.get("geometry")
    if geom is not None:
        return geom
    ux, uy = G.nodes[u].get("x"), G.nodes[u].get("y")
    vx, vy = G.nodes[v].get("x"), G.nodes[v].get("y")
    if None in (ux, uy, vx, vy):
        return None
    return LineString([(ux, uy), (vx, vy)])


def _build_edge_tree(G):
    """Returns (STRtree over edge geometries, parallel list of edge_keys),
    or (None, []) if G has no edges with resolvable geometry.

    edge_key matches router._edge_key's (a, b, key_or_0) convention so the
    resulting dict plugs directly into router.find_routes(edge_effects=...).
    """
    from service.router import _edge_items, _edge_key

    geoms = []
    edge_keys = []
    for u, v, key, attrs in _edge_items(G):
        geom = _edge_geometry(u, v, attrs, G)
        if geom is None:
            continue
        geoms.append(geom)
        edge_keys.append(_edge_key(u, v, key))

    if not geoms:
        return None, []
    return STRtree(geoms), edge_keys


def _match_reports_to_edges(G, reports) -> dict[str, list]:
    matches: dict[str, list] = {}
    if not reports:
        return matches

    tree, edge_keys = _build_edge_tree(G)
    if tree is None:
        return matches

    for r in reports:
        rid = _report_field(r, "id")
        lat = _report_field(r, "lat")
        lon = _report_field(r, "lon")
        if rid is None or lat is None or lon is None:
            continue
        x, y = _to_graph_crs.transform(lon, lat)
        idx = tree.query(Point(x, y), predicate="dwithin", distance=MATCH_RADIUS_FT)
        matches[rid] = [edge_keys[i] for i in idx]

    return matches


def report_edges(G, reports) -> dict[str, list]:
    """Map each report's id to the list of edge_keys it touches (~20 m).

    Used by explain.py to compute `reports_avoided`: a report counts as
    avoided by route B (relative to route A) if its edge list intersects
    A's edges but not B's. Includes every active report regardless of
    routing effect (even too_loud/crowded), since this stat is informational.
    """
    return _match_reports_to_edges(G, reports)


def _apply_multiplier(effects: dict, edge_key, multiplier: float) -> None:
    current = effects.get(edge_key)
    if current == "remove":
        return  # remove always wins, regardless of processing order.
    if current is None or multiplier > current:
        effects[edge_key] = multiplier


def edge_effects_for(G, reports) -> dict:
    """Map edge_key -> "remove" or a penalty multiplier (float).

    Effects table (CLAUDE.md §6): blocked_path/construction remove the
    edge (seed-sourced reports of these types penalize instead, see
    SEED_REMOVE_FALLBACK_MULTIPLIER); ice/too_steep/too_dark/trip_hazard
    penalize it by a fixed multiplier; too_loud/crowded have no effect.
    "remove" beats any multiplier; among multipliers on the same edge, the
    largest wins.
    """
    report_edge_map = _match_reports_to_edges(G, reports)
    effects: dict = {}

    for r in reports:
        rid = _report_field(r, "id")
        rtype = _report_field(r, "type")
        source = _report_field(r, "source")
        edge_keys = report_edge_map.get(rid, [])
        if not edge_keys:
            continue

        if rtype in REMOVE_TYPES:
            if source == "seed":
                for ek in edge_keys:
                    _apply_multiplier(effects, ek, SEED_REMOVE_FALLBACK_MULTIPLIER)
            else:
                for ek in edge_keys:
                    effects[ek] = "remove"
        elif rtype in PENALTY_MULTIPLIERS:
            multiplier = PENALTY_MULTIPLIERS[rtype]
            for ek in edge_keys:
                _apply_multiplier(effects, ek, multiplier)
        # NO_EFFECT_TYPES (too_loud, crowded) and unknown types: no effect.

    return effects
