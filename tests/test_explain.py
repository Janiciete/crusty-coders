"""Tests for service/explain.py (Prompt 4 task 3) against the hand-built
fixture graphs in tests/conftest.py (not graph.pkl; see test_api.py for the
one real-graph test).
"""

from __future__ import annotations

import math

import pytest

from service.conditions import Conditions
from service.explain import build_explanation, build_segments, build_violations, compute_stats
from service.profiles import PROFILE_PRESETS
from service.router import find_routes


def test_compute_stats_basic_fields(main_graph):
    conditions = Conditions()
    wheelchair_route = find_routes(
        main_graph, "O", ["E_access"], PROFILE_PRESETS["wheelchair"], conditions, k=1
    )[0]
    fastest_route = find_routes(
        main_graph, "O", ["E_access"], PROFILE_PRESETS["fastest"], conditions, k=1
    )[0]

    stats = compute_stats(
        main_graph, wheelchair_route, fastest_route, PROFILE_PRESETS["wheelchair"], dark_threshold=0.0
    )

    assert stats["distance_ft"] == pytest.approx(250.0)
    # wheelchair speed 3.3 ft/s -> 250/3.3/60 minutes
    assert stats["est_time_min"] == pytest.approx(250.0 / 3.3 / 60.0, abs=0.05)
    assert stats["max_slope_pct"] == pytest.approx(2.0)
    assert stats["max_cross_slope_pct"] == pytest.approx(0.0)
    assert stats["destination_entrance"] == {
        "door_id": "D1",
        "building": "Goldwin Smith",
        "access": "accessible",
        "auto_opener": None,
    }
    assert stats["unverified_segments"] == 0
    assert stats["estimated_segments"] == 0


def test_steps_avoided_is_nonnegative_diff_vs_fastest(main_graph):
    conditions = Conditions()
    wheelchair_route = find_routes(
        main_graph, "O", ["E_access"], PROFILE_PRESETS["wheelchair"], conditions, k=1
    )[0]
    fastest_route = find_routes(
        main_graph, "O", ["E_access"], PROFILE_PRESETS["fastest"], conditions, k=1
    )[0]

    # Fastest takes O-C-E_access (the 10-step stair shortcut); wheelchair
    # takes the step-free O-A-B-E_access detour.
    assert fastest_route.node_path == ["O", "C", "E_access"]
    assert "C" not in wheelchair_route.node_path

    stats = compute_stats(
        main_graph, wheelchair_route, fastest_route, PROFILE_PRESETS["wheelchair"], dark_threshold=0.0
    )
    assert stats["steps_avoided"] == 10

    # And the reverse direction never goes negative: comparing the fastest
    # route to itself avoids 0 steps, not a negative number.
    self_stats = compute_stats(
        main_graph, fastest_route, fastest_route, PROFILE_PRESETS["fastest"], dark_threshold=0.0
    )
    assert self_stats["steps_avoided"] == 0


def test_build_segments_shape(main_graph):
    conditions = Conditions()
    route = find_routes(
        main_graph, "O", ["E_access"], PROFILE_PRESETS["fastest"], conditions, k=1
    )[0]
    segments = build_segments(main_graph, route)
    assert len(segments) == len(route.edge_keys)
    for seg in segments:
        assert set(seg.keys()) == {
            "edge_id",
            "slope_pct",
            "slope_source",
            "cross_slope_pct",
            "date_surveyed",
            "verified",
        }


def test_build_violations_names_segment_and_standard(stairs_only_graph):
    conditions = Conditions()
    route = find_routes(
        stairs_only_graph, "O2", ["E_access6"], PROFILE_PRESETS["wheelchair"], conditions, k=1
    )[0]
    assert route.used_fallback is True

    violations = build_violations(stairs_only_graph, route, PROFILE_PRESETS["wheelchair"])
    assert violations, "expected at least one violation for a stairs-only route"
    codes = {v["code"] for v in violations}
    assert "stairs" in codes
    stairs_violation = next(v for v in violations if v["code"] == "stairs")
    assert "stairs" in stairs_violation["message"]
    assert "safe" not in stairs_violation["message"].lower()


def test_build_violations_slope_over_max_names_ada_ramp_limit():
    import networkx as nx
    from tests.conftest import make_edge

    G = nx.Graph()
    G.add_node("O3")
    G.add_node("E_access9", access="accessible", door_id="D9", building="SteepHall")
    G.add_edge(
        "O3",
        "E_access9",
        **make_edge(length_ft=40.0, slope_pct=11.0, slope_source="cornell_survey", ramp=True),
    )
    conditions = Conditions()
    route = find_routes(
        G, "O3", ["E_access9"], PROFILE_PRESETS["wheelchair"], conditions, k=1
    )[0]
    assert route.used_fallback is True

    violations = build_violations(G, route, PROFILE_PRESETS["wheelchair"])
    msg = next(v["message"] for v in violations if v["code"] == "slope_over_max")
    assert "11%" in msg
    assert "8.33%" in msg
    assert "ADA ramp limit" in msg
    assert "safe" not in msg.lower()


def test_build_violations_slope_over_max_estimated_says_estimated():
    """P4-fix2: an estimated (lidar) slope over ESTIMATED_SLOPE_HARD_PCT
    (10%) must hard-block Wheelchair via the fallback rule, with a message
    that clearly says the number is estimated, not surveyed.
    """
    import networkx as nx
    from tests.conftest import make_edge

    G = nx.Graph()
    G.add_node("O4")
    G.add_node("E_access10", access="accessible", door_id="D11", building="LidarSteepHall")
    G.add_edge(
        "O4",
        "E_access10",
        **make_edge(length_ft=40.0, slope_pct=15.4, slope_source="usgs_lidar_1m_estimate"),
    )
    conditions = Conditions()
    route = find_routes(
        G, "O4", ["E_access10"], PROFILE_PRESETS["wheelchair"], conditions, k=1
    )[0]
    assert route.used_fallback is True

    violations = build_violations(G, route, PROFILE_PRESETS["wheelchair"])
    msg = next(v["message"] for v in violations if v["code"] == "slope_over_max_estimated")
    assert "15.4%" in msg
    assert "estimated" in msg.lower()
    assert "8.33%" in msg
    assert "ADA ramp limit" in msg
    assert "safe" not in msg.lower()

    # The route-level explanation's max-slope line must also say "estimated"
    # whenever the reported max slope actually came from a lidar estimate.
    stats = compute_stats(G, route, None, PROFILE_PRESETS["wheelchair"], dark_threshold=0.0)
    lines = build_explanation(G, route, stats, PROFILE_PRESETS["wheelchair"], None)
    slope_line = next(line for line in lines if line.startswith("Maximum slope"))
    assert "estimated" in slope_line.lower()


def test_build_explanation_mentions_slope_lighting_and_destination(main_graph):
    conditions = Conditions()
    wheelchair_route = find_routes(
        main_graph, "O", ["E_access"], PROFILE_PRESETS["wheelchair"], conditions, k=1
    )[0]
    fastest_route = find_routes(
        main_graph, "O", ["E_access"], PROFILE_PRESETS["fastest"], conditions, k=1
    )[0]
    stats = compute_stats(
        main_graph, wheelchair_route, fastest_route, PROFILE_PRESETS["wheelchair"], dark_threshold=0.0
    )
    lines = build_explanation(
        main_graph, wheelchair_route, stats, PROFILE_PRESETS["wheelchair"], fastest_route
    )
    text = " | ".join(lines)
    assert any("staircase" in line for line in lines)
    assert any("Maximum slope" in line for line in lines)
    assert any("Maximum cross slope" in line for line in lines)
    assert any("Ends at an accessible entrance" in line for line in lines)
    assert any("lit" in line for line in lines)
    assert "safe" not in text.lower()


def test_build_explanation_never_says_safe_even_with_violations(stairs_only_graph):
    conditions = Conditions()
    route = find_routes(
        stairs_only_graph, "O2", ["E_access6"], PROFILE_PRESETS["wheelchair"], conditions, k=1
    )[0]
    stats = compute_stats(
        stairs_only_graph, route, None, PROFILE_PRESETS["wheelchair"], dark_threshold=0.0
    )
    lines = build_explanation(
        stairs_only_graph, route, stats, PROFILE_PRESETS["wheelchair"], None
    )
    text = " | ".join(lines).lower()
    assert "safe" not in text
    assert any("does not fully meet standards" in line.lower() for line in lines)
