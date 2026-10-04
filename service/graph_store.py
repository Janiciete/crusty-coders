"""Loads graph.pkl once at startup and builds the lookup structures the API
needs (Prompt 4 task 1): a building-name index of entrance nodes, a
nearest-node lookup (STRtree) for point origins/destinations, and the dark
threshold (cost.compute_dark_threshold).

This module owns graph I/O and generic graph-shaped lookups only.
Request-level policy (which entrance to prefer, the >100 m cutoff, building
name disambiguation rules) lives in app.py; this module just exposes the
primitives (match_building, nearest_node, route_geometry, edge_id_for).

CRS: build_graph.py (P1) does all metric work in EPSG:2261 (NY State Plane
Central, US feet -- see pipeline/build_graph.py:WORK_CRS) and stores node
`x`/`y` and edge `geometry` in that CRS, even though the published
edges.geojson/nodes.geojson are reprojected to EPSG:4326. graph.pkl itself
is NOT reprojected, so this module is the one place that knows about
EPSG:2261 <-> EPSG:4326 conversion for the live API (route geometry out,
point snapping in).
"""

from __future__ import annotations

import math
import os
import pickle
from pathlib import Path

import networkx as nx
from pyproj import Transformer
from shapely.geometry import Point
from shapely.strtree import STRtree

from service.cost import compute_dark_threshold

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GRAPH_PATH = REPO_ROOT / "cornell_data" / "graph" / "graph.pkl"

WORK_CRS = "EPSG:2261"  # must match pipeline/build_graph.py:WORK_CRS
WGS84 = "EPSG:4326"

FT_PER_METER = 3.280839895
M_PER_FT = 1.0 / FT_PER_METER


class BuildingNotFoundError(Exception):
    """Raised by GraphStore.match_building when no building matches."""

    def __init__(self, query: str):
        self.query = query
        super().__init__(f"no building matches {query!r}")


class BuildingAmbiguousError(Exception):
    """Raised by GraphStore.match_building when >1 building matches."""

    def __init__(self, query: str, candidates: list[str]):
        self.query = query
        self.candidates = candidates
        super().__init__(f"{len(candidates)} buildings match {query!r}: {candidates}")


def _graph_path() -> Path:
    env = os.getenv("GRAPH_PATH") or ""
    return Path(env) if env else DEFAULT_GRAPH_PATH


class GraphStore:
    """Holds the loaded graph plus the indexes built on top of it."""

    def __init__(self, graph: nx.Graph, path: Path | str | None = None):
        self.graph = graph
        self.path = Path(path) if path else None
        self.dark_threshold: float = compute_dark_threshold(graph)

        # building name (canonical, original case) -> entrance node ids.
        self.building_index: dict[str, list] = {}
        self._lower_to_canonical: dict[str, str] = {}
        for n, data in graph.nodes(data=True):
            building = data.get("building")
            if not building:
                continue
            self.building_index.setdefault(building, []).append(n)
            self._lower_to_canonical.setdefault(building.strip().lower(), building)

        # Nearest-node lookup. Only built if every node carries x/y (the
        # hand-built unit-test fixtures in tests/conftest.py do not, by
        # design -- they test routing logic, not geometry). Point-based
        # origins/destinations are unsupported against such a graph; the
        # caller gets a clear RuntimeError rather than a crash deep in
        # shapely.
        self._node_order: list = []
        self._strtree: STRtree | None = None
        self._to_work = Transformer.from_crs(WGS84, WORK_CRS, always_xy=True)
        self._to_wgs84 = Transformer.from_crs(WORK_CRS, WGS84, always_xy=True)

        coords = []
        for n, data in graph.nodes(data=True):
            x, y = data.get("x"), data.get("y")
            if x is None or y is None:
                continue
            self._node_order.append(n)
            coords.append(Point(x, y))
        if coords and len(coords) == graph.number_of_nodes():
            self._strtree = STRtree(coords)

    # ------------------------------------------------------------------
    # Building name resolution
    # ------------------------------------------------------------------
    def match_building(self, query: str) -> str:
        """Case-insensitive match: exact match wins, else a unique substring
        match. Raises BuildingNotFoundError / BuildingAmbiguousError.
        """
        q = (query or "").strip().lower()
        if not q:
            raise BuildingNotFoundError(query)

        if q in self._lower_to_canonical:
            return self._lower_to_canonical[q]

        matches = sorted(
            {canon for lower, canon in self._lower_to_canonical.items() if q in lower}
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise BuildingAmbiguousError(query, matches)
        raise BuildingNotFoundError(query)

    # ------------------------------------------------------------------
    # Point snapping
    # ------------------------------------------------------------------
    def nearest_node(self, lat: float, lon: float):
        """Returns (node_id, distance_m) for the graph node nearest to
        (lat, lon), or raises RuntimeError if the graph has no coordinates.
        """
        if self._strtree is None:
            raise RuntimeError(
                "graph has no node x/y coordinates; cannot snap a point to it"
            )
        x, y = self._to_work.transform(lon, lat)
        idx = self._strtree.nearest(Point(x, y))
        node_id = self._node_order[idx]
        nx_, ny_ = self.graph.nodes[node_id]["x"], self.graph.nodes[node_id]["y"]
        dist_ft = math.hypot(nx_ - x, ny_ - y)
        return node_id, dist_ft * M_PER_FT

    # ------------------------------------------------------------------
    # Geometry / edge id helpers (task 6)
    # ------------------------------------------------------------------
    def edge_data(self, edge_key) -> dict:
        a, b, k = edge_key
        data = self.graph.get_edge_data(a, b, k)
        if data is None:
            data = self.graph.get_edge_data(b, a, k)
        return data or {}

    def edge_id_for(self, edge_key) -> str:
        a, b, k = edge_key
        data = self.edge_data(edge_key)
        return data.get("edge_id") or f"{a}|{b}|{k}"

    def _to_lonlat(self, x: float, y: float):
        lon, lat = self._to_wgs84.transform(x, y)
        return [lon, lat]

    def route_geometry(self, route) -> dict:
        """GeoJSON LineString for a router.RouteResult, in EPSG:4326,
        oriented in travel order. Uses each edge's own geometry if present
        (it always is in the real graph); falls back to a straight line
        between the two node points otherwise (e.g. unit-test fixtures).
        """
        coords: list[tuple[float, float]] = []
        node_path = route.node_path
        for i, edge_key in enumerate(route.edge_keys):
            cur, nxt = node_path[i], node_path[i + 1]
            data = self.edge_data(edge_key)
            cur_xy = (self.graph.nodes[cur]["x"], self.graph.nodes[cur]["y"])
            nxt_xy = (self.graph.nodes[nxt]["x"], self.graph.nodes[nxt]["y"])
            geom = data.get("geometry")
            if geom is not None:
                seg = list(geom.coords)
                if _dist(seg[0], cur_xy) > _dist(seg[-1], cur_xy):
                    seg = list(reversed(seg))
            else:
                seg = [cur_xy, nxt_xy]

            if coords and _dist(coords[-1], seg[0]) < 1e-6:
                seg = seg[1:]
            coords.extend(seg)

        lonlat = [self._to_lonlat(x, y) for x, y in coords]
        return {"type": "LineString", "coordinates": lonlat}


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def build_store(graph: nx.Graph, path: Path | str | None = None) -> GraphStore:
    """Build a GraphStore from an already-loaded graph (used by tests to
    inject the fixture graph; see conftest.py's main_graph/main_multigraph).
    """
    return GraphStore(graph, path=path)


def load_graph_store(path: Path | str | None = None) -> GraphStore:
    """Load graph.pkl from GRAPH_PATH (or the given path) and build a store."""
    p = Path(path) if path else _graph_path()
    with open(p, "rb") as f:
        graph = pickle.load(f)
    return GraphStore(graph, path=p)


_STORE_CACHE: GraphStore | None = None


def get_store() -> GraphStore:
    """FastAPI dependency: the process-wide GraphStore, loaded once lazily.

    Tests override this with `app.dependency_overrides[get_store] = ...`
    rather than relying on the real cache.
    """
    global _STORE_CACHE
    if _STORE_CACHE is None:
        _STORE_CACHE = load_graph_store()
    return _STORE_CACHE
