"""Tests for service/layers.py (S4 L1): shape and filter checks for the
five map overlay layers, against a small hand-built fixture graph (not
graph.pkl), per CLAUDE.md's unit-test convention.
"""

from __future__ import annotations

import networkx as nx
import pytest
from fastapi.testclient import TestClient

from service.app import app
from service.graph_store import build_store, get_store
from tests.conftest import make_edge


def _layers_graph() -> nx.Graph:
    """A-B are accessible entrances; O-B is a surveyed >5% slope (also over
    the wheelchair hard limit); B-C is an *estimated* >10% slope (hard
    blocked for wheelchair too); A-N1 is a stairs edge; B-N2 is too narrow
    for wheelchair; N1-N2 is a plain step-free segment. O-B and B-N2 are
    the two dimmest edges (bottom quartile of lit_per_100ft).
    """
    G = nx.Graph()
    nodes = ["O", "A", "B", "C", "N1", "N2"]
    for i, n in enumerate(nodes):
        G.add_node(n, x=1_000_000.0 + 500.0 * i, y=2_000_000.0)
    G.nodes["A"].update(access="accessible", door_id="DA", building="Hall A", auto_opener=True)
    G.nodes["B"].update(access="accessible", door_id="DB", building="Hall B", auto_opener=False)
    G.nodes["N1"].update(access="unknown")
    G.nodes["N2"].update(access="not_accessible")

    G.add_edge("O", "A", **make_edge(length_ft=100.0, slope_pct=2.0, lit_per_100ft=50.0, width_ft=5.0))
    G.add_edge("O", "B", **make_edge(length_ft=100.0, slope_pct=9.0, lit_per_100ft=1.0, width_ft=5.0))
    G.add_edge(
        "A",
        "N1",
        **make_edge(
            length_ft=30.0,
            travel="stair",
            is_stairs=True,
            steps=10,
            slope_pct=None,
            slope_source=None,
            lit_per_100ft=10.0,
            width_ft=5.0,
        ),
    )
    G.add_edge("B", "N2", **make_edge(length_ft=40.0, slope_pct=0.0, lit_per_100ft=2.0, width_ft=2.0))
    G.add_edge("N1", "N2", **make_edge(length_ft=60.0, slope_pct=0.0, lit_per_100ft=100.0, width_ft=5.0))
    G.add_edge(
        "B",
        "C",
        **make_edge(
            length_ft=80.0,
            slope_pct=12.0,
            slope_source="usgs_lidar_1m_estimate",
            lit_per_100ft=80.0,
            width_ft=5.0,
        ),
    )
    return G


@pytest.fixture
def client():
    store = build_store(_layers_graph())
    app.dependency_overrides[get_store] = lambda: store
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_store, None)


def test_list_layers(client):
    resp = client.get("/layers")
    assert resp.status_code == 200
    names = resp.json()["layers"]
    assert set(names) == {
        "accessible_entrances",
        "slopes",
        "dark_segments",
        "step_free_paths",
        "reports",
    }


def test_unknown_layer_404(client):
    resp = client.get("/layers/not_a_real_layer")
    assert resp.status_code == 404


def test_accessible_entrances_shape(client):
    resp = client.get("/layers/accessible_entrances")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 2
    by_building = {f["properties"]["building"]: f for f in body["features"]}
    assert by_building["Hall A"]["properties"]["auto_opener"] is True
    assert by_building["Hall B"]["properties"]["auto_opener"] is False
    for f in body["features"]:
        assert f["geometry"]["type"] == "Point"
        assert len(f["geometry"]["coordinates"]) == 2


def test_slopes_over_5pct_with_estimated_flag(client):
    resp = client.get("/layers/slopes")
    body = resp.json()
    assert len(body["features"]) == 2
    by_slope = {round(f["properties"]["slope_pct"]): f for f in body["features"]}
    assert by_slope[9]["properties"]["estimated"] is False
    assert by_slope[12]["properties"]["estimated"] is True
    for f in body["features"]:
        assert f["geometry"]["type"] == "LineString"


def test_dark_segments_bottom_quartile(client):
    resp = client.get("/layers/dark_segments")
    body = resp.json()
    lit_values = sorted(f["properties"]["lit_per_100ft"] for f in body["features"])
    assert lit_values == [1.0, 2.0]


def test_step_free_paths_excludes_stairs_narrow_and_too_steep(client):
    resp = client.get("/layers/step_free_paths")
    body = resp.json()
    # Only O-A and N1-N2 pass every wheelchair hard limit (stairs excluded,
    # B-N2 too narrow, O-B too steep surveyed, B-C too steep estimated).
    assert len(body["features"]) == 2


def test_reports_layer_marks_seed_as_historical(client, monkeypatch):
    fake_reports = [
        {"id": "r1", "type": "ice", "lat": 42.45, "lon": -76.48, "status": "active", "source": "user"},
        {
            "id": "r2",
            "type": "trip_hazard",
            "lat": 42.46,
            "lon": -76.481,
            "status": "confirmed",
            "source": "seed",
        },
    ]
    monkeypatch.setattr("service.layers.get_active_reports", lambda: fake_reports)
    resp = client.get("/layers/reports")
    body = resp.json()
    assert len(body["features"]) == 2
    by_id = {f["properties"]["id"]: f for f in body["features"]}
    assert by_id["r1"]["properties"]["historical"] is False
    assert by_id["r2"]["properties"]["historical"] is True


def test_non_reports_layer_is_cached(client, monkeypatch):
    """Toggling a cached layer twice should not recompute it -- confirmed by
    swapping the builder after the first call and seeing the first result
    still returned."""
    first = client.get("/layers/slopes").json()
    import service.layers as layers_module

    monkeypatch.setitem(
        layers_module.LAYER_BUILDERS, "slopes", lambda store: {"type": "FeatureCollection", "features": []}
    )
    second = client.get("/layers/slopes").json()
    assert second == first
