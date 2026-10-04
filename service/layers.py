"""Map overlay layers (S4 L1): optional, quiet GeoJSON overlays the UI can
toggle on top of the hero route -- accessible entrances, steep slopes, dark
segments, step-free paths, and live/seed reports.

`GET /layers` lists the available layer names; `GET /layers/{name}` returns
one layer as a GeoJSON FeatureCollection in EPSG:4326. Every layer except
"reports" is built once per GraphStore and cached on the store instance
itself (`store._layers_cache`), so repeated requests are free and each
distinct store (e.g. a different fixture per test) gets its own cache
rather than leaking across tests. "reports" always reflects the current
`active_reports` table, so it is never cached.

This module only reads from the existing GraphStore / service.reports --
it does not touch graph_store.py's loading or build its own coordinate
pipeline beyond the same WORK_CRS -> WGS84 reprojection graph_store.py
already documents (pipeline/build_graph.py's WORK_CRS, EPSG:2261).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pyproj import Transformer

from service.cost import hard_limit_violations
from service.graph_store import WGS84, WORK_CRS, GraphStore, get_store
from service.profiles import SLOPE_SOURCE_ESTIMATE, PROFILE_PRESETS
from service.reports import get_active_reports

router = APIRouter()

_to_wgs84 = Transformer.from_crs(WORK_CRS, WGS84, always_xy=True)


def _lonlat(x: float, y: float) -> list[float]:
    lon, lat = _to_wgs84.transform(x, y)
    return [lon, lat]


def _iter_edges(G):
    """Yield (u, v, data) for every edge, Graph or MultiGraph alike."""
    if G.is_multigraph():
        for u, v, _k, data in G.edges(keys=True, data=True):
            yield u, v, data
    else:
        for u, v, data in G.edges(data=True):
            yield u, v, data


def _edge_linestring(store: GraphStore, u, v, data) -> dict | None:
    """GeoJSON LineString (EPSG:4326) for one edge, using its own geometry
    if present, else a straight line between the two node points. Returns
    None if neither is available (e.g. a fixture graph with no x/y)."""
    geom = data.get("geometry")
    if geom is not None:
        coords = [_lonlat(x, y) for x, y in geom.coords]
        return {"type": "LineString", "coordinates": coords}

    G = store.graph
    ux, uy = G.nodes[u].get("x"), G.nodes[u].get("y")
    vx, vy = G.nodes[v].get("x"), G.nodes[v].get("y")
    if ux is None or uy is None or vx is None or vy is None:
        return None
    return {"type": "LineString", "coordinates": [_lonlat(ux, uy), _lonlat(vx, vy)]}


def _report_field(r, key):
    if isinstance(r, dict):
        return r.get(key)
    return getattr(r, key, None)


# ---------------------------------------------------------------------------
# Layer builders
# ---------------------------------------------------------------------------


def _build_accessible_entrances(store: GraphStore) -> dict:
    features = []
    for _n, data in store.graph.nodes(data=True):
        if data.get("access") != "accessible":
            continue
        x, y = data.get("x"), data.get("y")
        if x is None or y is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": _lonlat(x, y)},
                "properties": {
                    "building": data.get("building"),
                    "auto_opener": bool(data.get("auto_opener")),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _build_slopes(store: GraphStore) -> dict:
    features = []
    for u, v, data in _iter_edges(store.graph):
        slope = data.get("slope_pct")
        if slope is None or slope <= 5.0:
            continue
        geom = _edge_linestring(store, u, v, data)
        if geom is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "slope_pct": slope,
                    "estimated": data.get("slope_source") == SLOPE_SOURCE_ESTIMATE,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _build_dark_segments(store: GraphStore) -> dict:
    threshold = store.dark_threshold
    features = []
    for u, v, data in _iter_edges(store.graph):
        lit = data.get("lit_per_100ft")
        if lit is None or lit > threshold:
            continue
        geom = _edge_linestring(store, u, v, data)
        if geom is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geom,
                "properties": {"lit_per_100ft": lit},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _build_step_free_paths(store: GraphStore) -> dict:
    wheelchair = PROFILE_PRESETS["wheelchair"]
    features = []
    for u, v, data in _iter_edges(store.graph):
        if hard_limit_violations(data, wheelchair):
            continue
        geom = _edge_linestring(store, u, v, data)
        if geom is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geom,
                "properties": {},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _build_reports(store: GraphStore) -> dict:
    features = []
    for r in get_active_reports():
        lat = _report_field(r, "lat")
        lon = _report_field(r, "lon")
        if lat is None or lon is None:
            continue
        source = _report_field(r, "source")
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "id": _report_field(r, "id"),
                    "report_type": _report_field(r, "type"),
                    "status": _report_field(r, "status"),
                    "historical": source == "seed",
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


# Order here is the order `GET /layers` lists them in.
LAYER_BUILDERS = {
    "accessible_entrances": _build_accessible_entrances,
    "slopes": _build_slopes,
    "dark_segments": _build_dark_segments,
    "step_free_paths": _build_step_free_paths,
    "reports": _build_reports,
}

# "reports" reflects live/seed data and must never be cached; every other
# layer is derived purely from the (immutable, process-wide) graph.
UNCACHED_LAYERS = {"reports"}


@router.get("/layers")
def list_layers():
    return {"layers": list(LAYER_BUILDERS.keys())}


@router.get("/layers/{name}")
def get_layer(name: str, store: GraphStore = Depends(get_store)):
    builder = LAYER_BUILDERS.get(name)
    if builder is None:
        raise HTTPException(404, detail=f"unknown layer: {name!r}")

    if name in UNCACHED_LAYERS:
        return builder(store)

    cache = getattr(store, "_layers_cache", None)
    if cache is None:
        cache = {}
        store._layers_cache = cache
    if name not in cache:
        cache[name] = builder(store)
    return cache[name]
