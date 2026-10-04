"""Tests for service/bottleneck.py (Prompt 8).

Fixture: a single stair edge is the only connection between two clusters of
accessible entrances in different buildings, so every sampled cross-building
trip must be blocked for Wheelchair, and fixing that one edge must unblock
all of them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import networkx as nx

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from service.bottleneck import find_bottlenecks


def _edge(**overrides) -> dict:
    attrs = {
        "length_ft": 50.0,
        "kind": "sidewalk",
        "travel": "preferred",
        "is_stairs": False,
        "steps": 0,
        "landings": 0,
        "rail_side": None,
        "slope_pct": 0.0,
        "slope_source": "cornell_survey",
        "cross_slope_pct": 0.0,
        "width_ft": 5.0,
        "surface": "concrete",
        "defect_level": 0,
        "ramp": False,
        "handrail": True,
        "curb_cuts": None,
        "surveyed": True,
        "date_surveyed": "2025-01-01",
        "lit_fixtures": 2,
        "lit_per_100ft": 10.0,
        "lit_watts": 100.0,
        "inferred": False,
        "verified": True,
        "flags": [],
        "edge_id": None,
    }
    attrs.update(overrides)
    return attrs


def _build_fixture() -> nx.Graph:
    """Two accessible-entrance clusters (Building A / Building B) joined by
    exactly one stair edge (M1-M2). Every A<->B trip must cross it; there is
    no step-free detour.
    """
    G = nx.Graph()
    G.add_node("EA1", access="accessible", door_id="A1", building="Building A")
    G.add_node("EA2", access="accessible", door_id="A2", building="Building A")
    G.add_node("EB1", access="accessible", door_id="B1", building="Building B")
    G.add_node("EB2", access="accessible", door_id="B2", building="Building B")
    G.add_node("M1")
    G.add_node("M2")

    G.add_edge("EA1", "M1", **_edge(edge_id="E_A1_M1"))
    G.add_edge("EA2", "M1", **_edge(edge_id="E_A2_M1"))
    G.add_edge(
        "M1",
        "M2",
        **_edge(edge_id="E_STAIR", is_stairs=True, travel="stair", steps=12, length_ft=30.0),
    )
    G.add_edge("M2", "EB1", **_edge(edge_id="E_M2_B1"))
    G.add_edge("M2", "EB2", **_edge(edge_id="E_M2_B2"))
    return G


def test_single_stair_edge_ranks_first_and_fully_unblocks():
    G = _build_fixture()
    result = find_bottlenecks(G, n_pairs=40, seed=42, top_n=5)

    assert result["pairs_sampled"] == 40
    # Every sampled trip is cross-building (A<->B), and the only path
    # between the two clusters crosses the stair edge -> every trip must be
    # blocked for Wheelchair (no strict route exists at all).
    assert result["blocked_trips"] == 40

    assert len(result["top_segments"]) == 1
    top = result["top_segments"][0]
    assert top["edge_id"] == "E_STAIR"
    assert top["trips_blocked"] == 40
    assert top["pct_trips_unblocked_if_fixed"] == 100.0
    assert top["kind"] == "sidewalk"
    assert top["length_ft"] == 30.0
    assert top["retagged_stair"] is False
    assert top["estimated"] is False

    assert len(top["violations"]) >= 1
    assert any(v["code"] == "stairs" for v in top["violations"])
    stairs_msgs = [v["message"] for v in top["violations"] if v["code"] == "stairs"]
    assert stairs_msgs and "stairs" in stairs_msgs[0] and "E_STAIR" in stairs_msgs[0]

    assert result["top5_combined_pct"] == 100.0
    assert result["note"]


def test_deterministic_with_fixed_seed():
    G = _build_fixture()
    r1 = find_bottlenecks(G, n_pairs=40, seed=42, top_n=5)
    r2 = find_bottlenecks(G, n_pairs=40, seed=42, top_n=5)

    assert r1["pairs_sampled"] == r2["pairs_sampled"]
    assert r1["blocked_trips"] == r2["blocked_trips"]
    assert [s["edge_id"] for s in r1["top_segments"]] == [s["edge_id"] for s in r2["top_segments"]]
    assert [s["trips_blocked"] for s in r1["top_segments"]] == [
        s["trips_blocked"] for s in r2["top_segments"]
    ]
    assert r1["top5_combined_pct"] == r2["top5_combined_pct"]


def test_no_coordinates_fixture_yields_none_lat_lon():
    G = _build_fixture()
    result = find_bottlenecks(G, n_pairs=10, seed=1, top_n=5)
    top = result["top_segments"][0]
    assert top["lat"] is None
    assert top["lon"] is None
