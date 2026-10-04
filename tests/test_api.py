"""Tests for service/app.py (Prompt 4 tasks 2, 4, 5, 6, 8).

Endpoint-shape / 422 / seed-filtering / no-leaked-keys tests run against the
hand-built fixture graphs in tests/conftest.py (main_graph, main_multigraph),
augmented here with x/y coordinates (the fixtures intentionally have none --
they test routing logic, not geometry) via a local `api_store` fixture, and
wired into the FastAPI app with `app.dependency_overrides`.

The one real-graph test (hero trip, all three profiles) is skipped if
cornell_data/graph/graph_snapshot_p4.pkl is missing, and deliberately reads
that snapshot path directly rather than GRAPH_PATH/the default graph.pkl, so
it is unaffected by any concurrent pipeline run that may be rewriting the
live graph.pkl.
"""

from __future__ import annotations

import math
import pickle
from pathlib import Path

import networkx as nx
import pytest
from fastapi.testclient import TestClient

from service import graph_store as graph_store_module
from service import reports as reports_module
from service.app import app
from service.graph_store import build_store, get_store
from service.profiles import PROFILE_PRESETS

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = REPO_ROOT / "cornell_data" / "graph" / "graph_snapshot_p4.pkl"

FAKE_SERVICE_KEY = "sk-test-super-secret-service-role-key-0123456789"


def _with_coordinates(G: nx.Graph) -> nx.Graph:
    """Lay nodes out on a simple line, 500 ft apart, in WORK_CRS (EPSG:2261,
    US feet) units, so GraphStore can build its STRtree / nearest_node /
    route_geometry. Values are arbitrary but far enough apart (500 ft) that
    the >100 m point-snap cutoff is meaningful in tests.
    """
    G = G.copy()
    for i, n in enumerate(sorted(G.nodes, key=str)):
        G.nodes[n]["x"] = 1_000_000.0 + 500.0 * i
        G.nodes[n]["y"] = 2_000_000.0
    return G


@pytest.fixture
def api_store(main_graph):
    G = _with_coordinates(main_graph)
    return build_store(G)


@pytest.fixture
def client(api_store):
    app.dependency_overrides[get_store] = lambda: api_store
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _origin_point(store):
    """A lat/lon that snaps to node 'O' (used as the origin in most tests)."""
    x, y = store.graph.nodes["O"]["x"], store.graph.nodes["O"]["y"]
    lon, lat = store._to_wgs84.transform(x, y)
    return {"lat": lat, "lon": lon}


# ---------------------------------------------------------------------------
# Endpoint shapes
# ---------------------------------------------------------------------------


def test_health_shape(client, api_store):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "graph_loaded": True,
        "nodes": api_store.graph.number_of_nodes(),
        "edges": api_store.graph.number_of_edges(),
        "supabase_configured": False,
    }


def test_profiles_shape():
    with TestClient(app) as c:
        r = c.get("/profiles")
    assert r.status_code == 200
    body = r.json()
    assert set(body["profiles"].keys()) == set(PROFILE_PRESETS.keys())
    wheelchair = body["profiles"]["wheelchair"]
    assert wheelchair["avoid_stairs"] is True
    assert wheelchair["max_slope_pct"] == pytest.approx(8.33)
    # math.inf must be JSON-safe (None), e.g. injured's max_slope_pct
    assert body["profiles"]["injured"]["max_slope_pct"] is None


def test_conditions_endpoint_overrides():
    with TestClient(app) as c:
        r = c.get("/conditions", params={"darkness": "on", "ice": "off"})
    assert r.status_code == 200
    body = r.json()
    assert body["darkness"] is True
    assert body["ice"] is False


def test_buildings_endpoint(client):
    r = client.get("/buildings")
    assert r.status_code == 200
    names = r.json()["buildings"]
    assert "Goldwin Smith" in names
    assert "Annex" in names
    assert "Annex2" in names


def test_bottlenecks_missing_returns_unavailable(client, monkeypatch):
    monkeypatch.setattr(
        "service.app.BOTTLENECKS_PATH", REPO_ROOT / "does" / "not" / "exist.json"
    )
    r = client.get("/bottlenecks")
    assert r.status_code == 200
    assert r.json() == {"available": False}


# ---------------------------------------------------------------------------
# POST /route: happy path
# ---------------------------------------------------------------------------


def test_route_happy_path_building_destination(client, api_store):
    body = {
        "origin": _origin_point(api_store),
        "destination": {"building": "Goldwin Smith"},
        "profiles": ["wheelchair"],
    }
    r = client.post("/route", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "routes" in data and len(data["routes"]) >= 1
    route = data["routes"][0]
    assert route["geometry"]["type"] == "LineString"
    assert len(route["geometry"]["coordinates"]) >= 2
    assert route["stats"]["destination_entrance"]["building"] == "Goldwin Smith"
    assert route["stats"]["destination_entrance"]["access"] == "accessible"
    assert isinstance(route["explanation"], list) and route["explanation"]
    assert isinstance(route["segments"], list) and route["segments"]


def test_route_three_profiles_differ(client, api_store):
    def run(profile):
        body = {
            "origin": _origin_point(api_store),
            "destination": {"building": "Goldwin Smith"},
            "profiles": [profile],
        }
        r = client.post("/route", json=body)
        assert r.status_code == 200, r.text
        return r.json()["routes"][0]

    fastest = run("fastest")
    wheelchair = run("wheelchair")

    # Fastest takes the stair shortcut (80 ft); wheelchair the step-free
    # detour (250 ft) -- same fixture scenario as test_routing.py.
    assert fastest["stats"]["distance_ft"] == pytest.approx(80.0)
    assert wheelchair["stats"]["distance_ft"] == pytest.approx(250.0)
    assert wheelchair["stats"]["steps_avoided"] == 10


# ---------------------------------------------------------------------------
# 422 cases
# ---------------------------------------------------------------------------


def test_route_unknown_profile_422(client, api_store):
    body = {
        "origin": _origin_point(api_store),
        "destination": {"building": "Goldwin Smith"},
        "profiles": ["not_a_real_profile"],
    }
    r = client.post("/route", json=body)
    assert r.status_code == 422


def test_route_ambiguous_building_422(client, api_store):
    # "Annex" is an exact match for one building, but querying a substring
    # shared by both "Annex" and "Annex2" must be ambiguous.
    body = {
        "origin": _origin_point(api_store),
        "destination": {"building": "nnex"},
        "profiles": ["fastest"],
    }
    r = client.post("/route", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "Annex" in str(detail)
    assert "Annex2" in str(detail)


def test_route_exact_building_name_wins_over_substring(client, api_store):
    # Exact match ("Annex") must win even though "Annex2" also matches as a
    # substring.
    body = {
        "origin": _origin_point(api_store),
        "destination": {"building": "Annex"},
        "profiles": ["fastest"],
    }
    r = client.post("/route", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["routes"][0]["stats"]["destination_entrance"]["building"] == "Annex"


def test_route_unknown_building_422(client, api_store):
    body = {
        "origin": _origin_point(api_store),
        "destination": {"building": "Totally Fake Hall"},
        "profiles": ["fastest"],
    }
    r = client.post("/route", json=body)
    assert r.status_code == 422


def test_route_point_too_far_422(client):
    body = {
        "origin": {"lat": 0.0, "lon": 0.0},  # nowhere near campus
        "destination": {"building": "Goldwin Smith"},
        "profiles": ["fastest"],
    }
    r = client.post("/route", json=body)
    assert r.status_code == 422
    assert "100" in str(r.json()["detail"])


def test_location_both_building_and_point_rejected(client, api_store):
    body = {
        "origin": {"building": "Goldwin Smith", **_origin_point(api_store)},
        "destination": {"building": "Annex"},
        "profiles": ["fastest"],
    }
    r = client.post("/route", json=body)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Wheelchair destination must be an accessible entrance / avoid unknown
# ---------------------------------------------------------------------------


def test_wheelchair_prefers_accessible_destination_over_unknown(client, api_store):
    # Goldwin Smith has both E_access (accessible) and E_unknown (unknown,
    # shorter) -- wheelchair must land on the accessible one.
    body = {
        "origin": _origin_point(api_store),
        "destination": {"building": "Goldwin Smith"},
        "profiles": ["wheelchair"],
    }
    r = client.post("/route", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["routes"][0]["stats"]["destination_entrance"]["access"] == "accessible"


# ---------------------------------------------------------------------------
# distance_tolerance enforcement (app layer, task 2 bullet 3)
# ---------------------------------------------------------------------------


def test_distance_tolerance_1_0_drops_every_longer_alternative(client, api_store):
    # Annex2 (E_access5) has a direct 130 ft path (O-R1-R2) and a 170 ft
    # detour (O-R1-R3-R2); Fastest's own distance_tolerance is 1.0, and its
    # baseline *is* its own route, so the 170 ft detour (170/130 ~= 1.31x)
    # must be dropped, leaving exactly one route even though alternatives=3
    # was requested.
    body = {
        "origin": _origin_point(api_store),
        "destination": {"building": "Annex2"},
        "profiles": ["fastest"],
        "alternatives": 3,
    }
    r = client.post("/route", json=body)
    assert r.status_code == 200, r.text
    routes = r.json()["routes"]
    assert len(routes) == 1
    assert routes[0]["stats"]["distance_ft"] == pytest.approx(130.0)


def test_distance_tolerance_1_5_keeps_the_detour_alternative(client, api_store):
    # Same two paths, but low_vision's distance_tolerance is 1.5, comfortably
    # above the detour's 1.31x ratio, so both routes should survive.
    body = {
        "origin": _origin_point(api_store),
        "destination": {"building": "Annex2"},
        "profiles": ["low_vision"],
        "alternatives": 3,
    }
    r = client.post("/route", json=body)
    assert r.status_code == 200, r.text
    lengths = sorted(rt["stats"]["distance_ft"] for rt in r.json()["routes"])
    assert lengths[0] == pytest.approx(130.0)
    assert len(lengths) == 2
    assert lengths[1] == pytest.approx(170.0)


# ---------------------------------------------------------------------------
# include_seed_reports (task 4): seed reports filtered before edge_effects_for
# ---------------------------------------------------------------------------


def test_include_seed_reports_false_filters_seed_before_edge_effects(client, api_store, monkeypatch):
    seen = {}

    fake_reports = [
        {"id": "r1", "type": "blocked_path", "source": "seed"},
        {"id": "r2", "type": "ice", "source": "user"},
    ]

    def fake_get_active_reports():
        return fake_reports

    def fake_edge_effects_for(G, reports):
        seen["reports"] = reports
        return {}

    monkeypatch.setattr("service.app.get_active_reports", fake_get_active_reports)
    monkeypatch.setattr("service.app.edge_effects_for", fake_edge_effects_for)

    body = {
        "origin": _origin_point(api_store),
        "destination": {"building": "Goldwin Smith"},
        "profiles": ["fastest"],
        "include_seed_reports": False,
    }
    r = client.post("/route", json=body)
    assert r.status_code == 200, r.text
    assert seen["reports"] == [{"id": "r2", "type": "ice", "source": "user"}]


def test_include_seed_reports_true_by_default_keeps_seed(client, api_store, monkeypatch):
    seen = {}
    fake_reports = [{"id": "r1", "type": "blocked_path", "source": "seed"}]

    def fake_edge_effects_for(G, reports):
        seen["reports"] = reports
        return {}

    monkeypatch.setattr("service.app.get_active_reports", lambda: fake_reports)
    monkeypatch.setattr("service.app.edge_effects_for", fake_edge_effects_for)

    body = {
        "origin": _origin_point(api_store),
        "destination": {"building": "Goldwin Smith"},
        "profiles": ["fastest"],
    }
    r = client.post("/route", json=body)
    assert r.status_code == 200, r.text
    assert seen["reports"] == fake_reports


def test_reports_last_error_adds_warning(client, api_store, monkeypatch):
    monkeypatch.setattr(reports_module, "last_error", "supabase unreachable")
    try:
        body = {
            "origin": _origin_point(api_store),
            "destination": {"building": "Goldwin Smith"},
            "profiles": ["fastest"],
        }
        r = client.post("/route", json=body)
        assert r.status_code == 200, r.text
        assert "Live reports unavailable; routing without them." in r.json()["warnings"]
    finally:
        monkeypatch.setattr(reports_module, "last_error", None)


# ---------------------------------------------------------------------------
# No response ever contains a key
# ---------------------------------------------------------------------------


def test_no_response_contains_a_secret_key(client, api_store, monkeypatch):
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", FAKE_SERVICE_KEY)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key-value")

    endpoints = [
        ("GET", "/health", None),
        ("GET", "/profiles", None),
        ("GET", "/conditions", None),
        ("GET", "/buildings", None),
        ("GET", "/bottlenecks", None),
    ]
    for method, path, body in endpoints:
        r = client.get(path) if method == "GET" else client.post(path, json=body)
        assert FAKE_SERVICE_KEY not in r.text
        assert "anon-key-value" not in r.text

    r = client.post(
        "/route",
        json={
            "origin": _origin_point(api_store),
            "destination": {"building": "Goldwin Smith"},
            "profiles": ["fastest"],
        },
    )
    assert FAKE_SERVICE_KEY not in r.text
    assert "anon-key-value" not in r.text

    # health should now report supabase as configured, without leaking it
    health = client.get("/health").json()
    assert health["supabase_configured"] is True
    assert "key" not in str(health).lower().replace("supabase_configured", "")


# ---------------------------------------------------------------------------
# slope_source == "unknown": unverified penalty, never a slope hard limit
# (task 2's distance_tolerance bullet neighbor: slope_source bullet)
# ---------------------------------------------------------------------------


def test_unknown_slope_source_never_hard_blocks_but_is_penalized():
    from service.conditions import Conditions
    from service.cost import edge_cost, hard_limit_violations
    from tests.conftest import make_edge

    prefs = PROFILE_PRESETS["wheelchair"]
    conditions = Conditions()

    attrs_unknown_steep = make_edge(
        length_ft=100.0, slope_pct=40.0, slope_source="unknown"
    )
    # Even a wildly steep "unknown" slope must not trip slope_over_max or
    # slope_needs_ramp -- those only fire for slope_source == cornell_survey.
    codes = hard_limit_violations(attrs_unknown_steep, prefs)
    assert "slope_over_max" not in codes
    assert "slope_needs_ramp" not in codes

    attrs_flat_surveyed = make_edge(
        length_ft=100.0, slope_pct=0.0, slope_source="cornell_survey"
    )
    cost_unknown = edge_cost(attrs_unknown_steep, prefs, conditions, dark_threshold=0.0)
    cost_flat_surveyed = edge_cost(attrs_flat_surveyed, prefs, conditions, dark_threshold=0.0)
    # The unverified penalty (unknown_slope_flat) must make the "unknown"
    # edge cost more than a flat, fully-surveyed edge of the same length.
    assert cost_unknown > cost_flat_surveyed


# ---------------------------------------------------------------------------
# One real-graph test (hero trip, all three profiles) -- skipped if the
# snapshot isn't present. Deliberately loads SNAPSHOT_PATH directly, not
# GRAPH_PATH / the default graph.pkl.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not SNAPSHOT_PATH.exists(), reason="graph_snapshot_p4.pkl not found")
def test_hero_trip_three_profiles_on_real_graph():
    with open(SNAPSHOT_PATH, "rb") as f:
        G = pickle.load(f)
    store = build_store(G, path=SNAPSHOT_PATH)

    app.dependency_overrides[get_store] = lambda: store
    try:
        with TestClient(app) as c:
            results = {}
            for profile in ("fastest", "injured", "wheelchair"):
                body = {
                    "origin": {"building": "Keeton"},
                    "destination": {"building": "Goldwin Smith"},
                    "profiles": [profile],
                }
                r = c.post("/route", json=body)
                assert r.status_code == 200, r.text
                results[profile] = r.json()["routes"][0]
    finally:
        app.dependency_overrides.clear()

    lengths = {p: results[p]["stats"]["distance_ft"] for p in results}
    assert len(set(round(v, 1) for v in lengths.values())) >= 2, (
        f"expected the three profiles to differ, got {lengths}"
    )

    wheelchair = results["wheelchair"]
    wc_segments = wheelchair["segments"]
    wc_edge_ids = {s["edge_id"] for s in wc_segments}
    violations_present = bool(wheelchair["violations"])
    # Re-derive is_stairs per segment from the graph itself (segments don't
    # carry is_stairs; cross-check against G by edge_id).
    stair_edge_ids = {
        d.get("edge_id")
        for _u, _v, d in G.edges(data=True)
        if d.get("is_stairs")
    }
    used_a_stair = bool(wc_edge_ids & stair_edge_ids)
    assert (not used_a_stair) or violations_present

    dest = wheelchair["stats"]["destination_entrance"]
    assert dest is not None
    assert dest["building"] == "Goldwin Smith Hall"
    assert dest["access"] == "accessible"
