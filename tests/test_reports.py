"""Tests for service/reports.py (Prompt 6): spatial matching of live/seed
reports to graph edges, the remove-vs-penalty effects table, and the
get_active_reports() cache/error path.

Graph fixtures here are custom (not tests/conftest.py's main_graph/etc,
which have no coordinates at all and can't be edited per this prompt's
file-scope constraints). Nodes carry x/y in service.reports.GRAPH_CRS
(EPSG:2261 feet); edges rely on reports.py's fallback to a straight
node-to-node LineString when no explicit "geometry" attr is set, so no
shapely geometry needs to be hand-built here.
"""

from __future__ import annotations

import math
import os
import pickle
import time
from pathlib import Path

import networkx as nx
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from service import reports as reports_module
from service.reports import (
    MATCH_RADIUS_M,
    _to_graph_crs,
    edge_effects_for,
    get_active_reports,
    report_edges,
)
from service.router import _edge_key

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = REPO_ROOT / "cornell_data" / "graph" / "graph_snapshot_p4.pkl"

SUPABASE_URL = os.getenv("SUPABASE_URL") or None
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or None
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or None

# A point well inside the demo-zone bbox (same point used by
# tests/test_supabase_integration.py).
LON0, LAT0 = -76.4850, 42.4480

_METERS_PER_DEGREE_LAT = 111_320.0


def _offset_lonlat(lon: float, lat: float, east_m: float = 0.0, north_m: float = 0.0):
    d_lat = north_m / _METERS_PER_DEGREE_LAT
    d_lon = east_m / (_METERS_PER_DEGREE_LAT * math.cos(math.radians(lat)))
    return lon + d_lon, lat + d_lat


def _add_node_latlon(G, node_id, lon, lat, **attrs):
    x, y = _to_graph_crs.transform(lon, lat)
    G.add_node(node_id, x=x, y=y, **attrs)


@pytest.fixture()
def line_graph() -> nx.Graph:
    """A single 200 ft edge C-D running east, for radius-boundary tests."""
    G = nx.Graph()
    c_lon, c_lat = LON0, LAT0
    d_lon, d_lat = _offset_lonlat(LON0, LAT0, east_m=60.0)  # ~200 ft east
    _add_node_latlon(G, "C", c_lon, c_lat)
    _add_node_latlon(G, "D", d_lon, d_lat)
    G.add_edge("C", "D", length_ft=200.0)
    G._midpoint_lonlat = _offset_lonlat(LON0, LAT0, east_m=30.0)
    return G


def _report(rid, rtype, lon, lat, source="user"):
    return {"id": rid, "type": rtype, "lat": lat, "lon": lon, "source": source}


def test_radius_boundary_near_matches_far_does_not(line_graph):
    mid_lon, mid_lat = line_graph._midpoint_lonlat
    assert MATCH_RADIUS_M == 20.0

    near_lon, near_lat = _offset_lonlat(mid_lon, mid_lat, north_m=10.0)  # within 20 m
    far_lon, far_lat = _offset_lonlat(mid_lon, mid_lat, north_m=30.0)  # outside 20 m

    near = _report("near", "blocked_path", near_lon, near_lat)
    far = _report("far", "blocked_path", far_lon, far_lat)

    matches = report_edges(line_graph, [near, far])
    cd_key = _edge_key("C", "D", None)

    assert matches["near"] == [cd_key]
    assert matches["far"] == []  # still present, just unmatched

    effects = edge_effects_for(line_graph, [near, far])
    assert effects == {cd_key: "remove"}


@pytest.fixture()
def two_node_graph() -> nx.Graph:
    """A single 100 ft edge A-B, for the effects-table tests."""
    G = nx.Graph()
    b_lon, b_lat = _offset_lonlat(LON0, LAT0, east_m=30.0)  # ~100 ft east
    _add_node_latlon(G, "A", LON0, LAT0)
    _add_node_latlon(G, "B", b_lon, b_lat)
    G.add_edge("A", "B", length_ft=100.0)
    G._mid_lonlat = _offset_lonlat(LON0, LAT0, east_m=15.0)
    return G


def _on_edge_report(G, rid, rtype, source="user"):
    lon, lat = G._mid_lonlat
    return _report(rid, rtype, lon, lat, source=source)


def test_user_blocked_path_removes_edge(two_node_graph):
    r = _on_edge_report(two_node_graph, "r1", "blocked_path")
    effects = edge_effects_for(two_node_graph, [r])
    assert effects == {_edge_key("A", "B", None): "remove"}


def test_user_construction_removes_edge(two_node_graph):
    r = _on_edge_report(two_node_graph, "r1", "construction")
    effects = edge_effects_for(two_node_graph, [r])
    assert effects == {_edge_key("A", "B", None): "remove"}


def test_seed_blocked_path_never_removes(two_node_graph):
    r = _on_edge_report(two_node_graph, "r1", "blocked_path", source="seed")
    effects = edge_effects_for(two_node_graph, [r])
    assert effects == {_edge_key("A", "B", None): 3.0}


@pytest.mark.parametrize(
    "rtype,expected",
    [("ice", 3.0), ("too_steep", 2.0), ("too_dark", 1.5), ("trip_hazard", 1.5)],
)
def test_penalty_multipliers(two_node_graph, rtype, expected):
    r = _on_edge_report(two_node_graph, "r1", rtype, source="seed" if rtype == "trip_hazard" else "user")
    effects = edge_effects_for(two_node_graph, [r])
    assert effects == {_edge_key("A", "B", None): expected}


@pytest.mark.parametrize("rtype", ["too_loud", "crowded"])
def test_no_effect_types_have_no_cost_effect_but_are_still_tracked(two_node_graph, rtype):
    r = _on_edge_report(two_node_graph, "r1", rtype)
    effects = edge_effects_for(two_node_graph, [r])
    assert effects == {}

    matches = report_edges(two_node_graph, [r])
    assert matches["r1"] == [_edge_key("A", "B", None)]


def test_remove_beats_multiplier_regardless_of_order(two_node_graph):
    ice = _on_edge_report(two_node_graph, "ice-r", "ice")
    blocked = _on_edge_report(two_node_graph, "blocked-r", "blocked_path")
    ekey = _edge_key("A", "B", None)

    assert edge_effects_for(two_node_graph, [ice, blocked]) == {ekey: "remove"}
    assert edge_effects_for(two_node_graph, [blocked, ice]) == {ekey: "remove"}


def test_largest_multiplier_wins_regardless_of_order(two_node_graph):
    dark = _on_edge_report(two_node_graph, "dark-r", "too_dark")
    ice = _on_edge_report(two_node_graph, "ice-r", "ice")
    ekey = _edge_key("A", "B", None)

    assert edge_effects_for(two_node_graph, [dark, ice]) == {ekey: 3.0}
    assert edge_effects_for(two_node_graph, [ice, dark]) == {ekey: 3.0}


def test_no_reports_short_circuits_without_touching_graph():
    assert edge_effects_for(object(), []) == {}
    assert report_edges(object(), []) == {}


# ---------------------------------------------------------------------------
# get_active_reports(): cache + error path
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    def __init__(self, data, calls):
        self._data = data
        self._calls = calls

    def select(self, *_args, **_kwargs):
        return self

    def execute(self):
        self._calls.append(1)
        return _FakeResult(self._data)


class _FakeClient:
    def __init__(self, data, calls):
        self._data = data
        self._calls = calls

    def table(self, _name):
        return _FakeTable(self._data, self._calls)


@pytest.fixture(autouse=True)
def _reset_reports_module_state():
    reports_module._cache_reports = None
    reports_module._cache_time = 0.0
    reports_module._client = None
    reports_module._client_env = None
    reports_module.last_error = None
    yield
    reports_module._cache_reports = None
    reports_module._cache_time = 0.0
    reports_module._client = None
    reports_module._client_env = None
    reports_module.last_error = None


def test_get_active_reports_success_and_cache(monkeypatch):
    calls = []
    fake_data = [{"id": "x", "type": "ice", "lat": LAT0, "lon": LON0}]
    monkeypatch.setattr(reports_module, "_client_or_none", lambda: _FakeClient(fake_data, calls))

    first = get_active_reports()
    second = get_active_reports()

    assert first == fake_data
    assert second == fake_data
    assert len(calls) == 1  # second call served from the 5 s cache
    assert reports_module.last_error is None


def test_get_active_reports_cache_expires(monkeypatch):
    import time

    calls = []
    fake_data = [{"id": "x"}]
    monkeypatch.setattr(reports_module, "_client_or_none", lambda: _FakeClient(fake_data, calls))

    get_active_reports()
    assert len(calls) == 1

    reports_module._cache_time = time.monotonic() - 100  # force expiry
    get_active_reports()
    assert len(calls) == 2


def test_get_active_reports_error_path_returns_empty_and_sets_last_error(monkeypatch):
    def _raise():
        raise RuntimeError("supabase unreachable")

    monkeypatch.setattr(reports_module, "_client_or_none", _raise)

    result = get_active_reports()

    assert result == []
    assert reports_module.last_error is not None
    assert "supabase unreachable" in reports_module.last_error


def test_get_active_reports_missing_config_is_not_an_exception(monkeypatch):
    monkeypatch.setattr(reports_module, "_client_or_none", lambda: None)

    result = get_active_reports()

    assert result == []
    assert reports_module.last_error is not None


# ---------------------------------------------------------------------------
# Live end-to-end: a confirmed blocked_path report reroutes the Wheelchair
# hero trip around the edge it lands on. Requires both a real graph
# snapshot (same convention as tests/test_api.py's hero-trip test) and live
# Supabase credentials, so it's skipped unless both are present.
# ---------------------------------------------------------------------------

_LIVE_SKIP_REASON = (
    "requires cornell_data/graph/graph_snapshot_p4.pkl and "
    "SUPABASE_URL/SUPABASE_ANON_KEY/SUPABASE_SERVICE_ROLE_KEY in .env"
)


@pytest.mark.skipif(
    not (SNAPSHOT_PATH.exists() and SUPABASE_URL and SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY),
    reason=_LIVE_SKIP_REASON,
)
def test_confirmed_blocked_path_reroutes_wheelchair_hero_trip():
    from supabase import create_client

    from service.app import app
    from service.graph_store import build_store, get_store

    with open(SNAPSHOT_PATH, "rb") as f:
        G = pickle.load(f)
    store = build_store(G, path=SNAPSHOT_PATH)

    anon = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    service = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

    app.dependency_overrides[get_store] = lambda: store
    created_ids: list[str] = []
    try:
        with TestClient(app) as c:
            body = {
                "origin": {"building": "Keeton"},
                "destination": {"building": "Goldwin Smith"},
                "profiles": ["wheelchair"],
            }
            before = c.post("/route", json=body)
            assert before.status_code == 200, before.text
            segments = before.json()["routes"][0]["segments"]
            assert segments, "hero trip returned a route with no segments"
            target_edge_id = segments[len(segments) // 2]["edge_id"]

            edges_by_id = {attrs.get("edge_id"): (u, v, attrs) for u, v, attrs in G.edges(data=True)}
            u, v, attrs = edges_by_id[target_edge_id]
            geom = attrs.get("geometry")
            if geom is not None:
                mid_x, mid_y = geom.interpolate(0.5, normalized=True).coords[0]
            else:
                mid_x = (G.nodes[u]["x"] + G.nodes[v]["x"]) / 2.0
                mid_y = (G.nodes[u]["y"] + G.nodes[v]["y"]) / 2.0
            mid_lon, mid_lat = _to_graph_crs.transform(mid_x, mid_y, direction="INVERSE")

            # Two reports (same type, within 15 m and 60 min) so the
            # reports_after_insert trigger auto-confirms both -- a lone
            # pending report wouldn't appear in the active_reports view.
            payload = {
                "type": "blocked_path",
                "location": f"POINT({mid_lon} {mid_lat})",
                "note": "TEST",
            }
            for _ in range(2):
                result = anon.table("reports").insert(payload).execute()
                created_ids.append(result.data[0]["id"])
            time.sleep(0.5)

            # Bypass the 5 s cache so the route call sees the new reports.
            reports_module._cache_reports = None
            reports_module._cache_time = 0.0

            after = c.post("/route", json=body)
            assert after.status_code in (200, 422), after.text
            if after.status_code == 200:
                after_edge_ids = {s["edge_id"] for s in after.json()["routes"][0]["segments"]}
                assert target_edge_id not in after_edge_ids
    finally:
        app.dependency_overrides.clear()
        try:
            if created_ids:
                service.table("reports").delete().in_("id", created_ids).execute()
            service.table("reports").delete().eq("note", "TEST").execute()
        except Exception:
            pass
