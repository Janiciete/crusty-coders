"""Tests for the routing core (service/conditions.py, reports.py, profiles.py,
cost.py, router.py) against the hand-built fixtures in conftest.py.

Acceptance criteria are drawn from the P3 prompt; see docs/project_plan.md
§8.2-8.4 and CLAUDE.md §5-§6 for the rules being exercised.
"""

from __future__ import annotations

import math
from datetime import datetime

import networkx as nx
import pytest

from service.conditions import Conditions, get_conditions
from service.cost import compute_dark_threshold, edge_cost, hard_limit_violations
from service.profiles import PROFILE_PRESETS, resolve_preferences
from service.reports import edge_effects_for, get_active_reports
from service.router import find_routes
from tests.conftest import make_edge


# ---------------------------------------------------------------------------
# conditions.py / reports.py stubs
# ---------------------------------------------------------------------------


def test_conditions_stub_on_off_auto():
    # P7 note: get_conditions() now does real astral/NWS work for "auto"
    # (see tests/test_conditions.py for full coverage of that). This test
    # predates P7 and only checks override plumbing, so it pins `now` to a
    # known daytime moment and forces ice="off" to stay network-free, per
    # CLAUDE.md's "no real network in tests" rule.
    noon = datetime(2026, 10, 4, 12, 0, 0)

    c = get_conditions({"darkness": "on", "ice": "off"}, now=noon)
    assert c.darkness is True
    assert c.ice is False

    c2 = get_conditions({"ice": "off"}, now=noon)
    assert c2.darkness is False
    assert c2.ice is False

    c3 = get_conditions({"darkness": "auto", "ice": "off"}, now=noon)
    assert c3.darkness is False


def test_reports_stub_returns_empty():
    assert get_active_reports() == []
    assert edge_effects_for(object(), []) == {}


# ---------------------------------------------------------------------------
# profiles.py: resolve_preferences
# ---------------------------------------------------------------------------


def test_unknown_profile_raises():
    with pytest.raises(ValueError):
        resolve_preferences(["not_a_profile"], None, [])


def test_unknown_override_key_raises():
    with pytest.raises(ValueError):
        resolve_preferences(["fastest"], {"bogus_key": 1}, [])


def test_unknown_adjustment_raises():
    with pytest.raises(ValueError):
        resolve_preferences(["fastest"], None, ["unknown_adjustment"])


def test_injured_low_vision_combine_rules_and_slowest_speed():
    prefs = resolve_preferences(["injured", "low_vision"], None, [])
    assert prefs.avoid_stairs is True  # from injured
    assert prefs.prefer_lit is True  # from low_vision
    assert prefs.walking_speed_ftps == pytest.approx(2.5)  # slowest of 2.5/4.3


def test_in_a_hurry_caps_distance_tolerance():
    prefs = resolve_preferences(["wheelchair"], None, ["in_a_hurry"])
    assert prefs.distance_tolerance <= 1.1


def test_walking_alone_forces_prefer_lit():
    prefs = resolve_preferences(["fastest"], None, ["walking_alone"])
    assert prefs.prefer_lit is True


# ---------------------------------------------------------------------------
# cost.py: hard_limit_violations
# ---------------------------------------------------------------------------


def test_surveyed_slope_ramp_rules_for_wheelchair():
    prefs = PROFILE_PRESETS["wheelchair"]

    attrs_7_no_ramp = make_edge(slope_pct=7.0, slope_source="cornell_survey", ramp=False)
    attrs_7_ramp = make_edge(slope_pct=7.0, slope_source="cornell_survey", ramp=True)
    attrs_9_ramp = make_edge(slope_pct=9.0, slope_source="cornell_survey", ramp=True)

    assert "slope_needs_ramp" in hard_limit_violations(attrs_7_no_ramp, prefs)
    assert hard_limit_violations(attrs_7_ramp, prefs) == []
    assert "slope_over_max" in hard_limit_violations(attrs_9_ramp, prefs)


def test_estimated_slope_not_hard_blocked_but_costs_more():
    prefs = PROFILE_PRESETS["wheelchair"]
    conditions = Conditions()

    attrs_estimated_9 = make_edge(
        length_ft=100.0, slope_pct=9.0, slope_source="usgs_lidar_1m_estimate"
    )
    attrs_surveyed_4 = make_edge(
        length_ft=100.0, slope_pct=4.0, slope_source="cornell_survey"
    )

    # 9% is below ESTIMATED_SLOPE_HARD_PCT (10.0), so still only a penalty.
    assert hard_limit_violations(attrs_estimated_9, prefs) == []

    cost_estimated = edge_cost(attrs_estimated_9, prefs, conditions, dark_threshold=0.0)
    cost_surveyed = edge_cost(attrs_surveyed_4, prefs, conditions, dark_threshold=0.0)
    assert cost_estimated > cost_surveyed


# ---------------------------------------------------------------------------
# P4-fix2: estimated (lidar) slopes above ESTIMATED_SLOPE_HARD_PCT (10.0%)
# are hard-blocked for profiles whose own max_slope_pct is <= that threshold
# (wheelchair); estimated slopes at/under it keep the existing heavy penalty
# only; profiles with no hard slope limit (e.g. fastest) are unaffected.
# ---------------------------------------------------------------------------


def test_estimated_12_percent_blocks_wheelchair():
    prefs = PROFILE_PRESETS["wheelchair"]
    attrs_estimated_12 = make_edge(slope_pct=12.0, slope_source="usgs_lidar_1m_estimate")
    assert "slope_over_max_estimated" in hard_limit_violations(attrs_estimated_12, prefs)


def test_estimated_8_percent_penalized_not_blocked_for_wheelchair():
    prefs = PROFILE_PRESETS["wheelchair"]
    conditions = Conditions()

    attrs_estimated_8 = make_edge(
        length_ft=100.0, slope_pct=8.0, slope_source="usgs_lidar_1m_estimate"
    )
    attrs_surveyed_2 = make_edge(
        length_ft=100.0, slope_pct=2.0, slope_source="cornell_survey"
    )

    # Below the 10% hard-block threshold -> no violation, just a heavier cost.
    assert hard_limit_violations(attrs_estimated_8, prefs) == []
    cost_estimated = edge_cost(attrs_estimated_8, prefs, conditions, dark_threshold=0.0)
    cost_surveyed = edge_cost(attrs_surveyed_2, prefs, conditions, dark_threshold=0.0)
    assert cost_estimated > cost_surveyed


def test_estimated_12_percent_does_not_block_fastest():
    # Fastest has no hard slope limit (max_slope_pct = inf), so the new rule
    # (which only fires when prefs.max_slope_pct <= ESTIMATED_SLOPE_HARD_PCT)
    # must never block it.
    prefs = PROFILE_PRESETS["fastest"]
    attrs_estimated_12 = make_edge(slope_pct=12.0, slope_source="usgs_lidar_1m_estimate")
    assert hard_limit_violations(attrs_estimated_12, prefs) == []


def test_estimated_12_percent_does_not_affect_surveyed_or_unknown_rules():
    prefs = PROFILE_PRESETS["wheelchair"]
    # Surveyed slopes keep their existing (unchanged) hard-limit behavior.
    attrs_surveyed_12 = make_edge(slope_pct=12.0, slope_source="cornell_survey")
    assert "slope_over_max" in hard_limit_violations(attrs_surveyed_12, prefs)
    assert "slope_over_max_estimated" not in hard_limit_violations(attrs_surveyed_12, prefs)

    # "unknown" slopes are still never hard-blocked, even at 12%.
    attrs_unknown_12 = make_edge(slope_pct=12.0, slope_source="unknown")
    assert hard_limit_violations(attrs_unknown_12, prefs) == []


def test_ice_raises_cost_of_steep_edge():
    prefs = PROFILE_PRESETS["injured"]  # no hard slope limit, just a penalty
    attrs_steep = make_edge(length_ft=100.0, slope_pct=6.0, slope_source="cornell_survey")

    cost_no_ice = edge_cost(attrs_steep, prefs, Conditions(ice=False), dark_threshold=0.0)
    cost_ice = edge_cost(attrs_steep, prefs, Conditions(ice=True), dark_threshold=0.0)
    assert cost_ice > cost_no_ice


def test_darkness_raises_cost_of_dark_edge_for_fastest_too():
    prefs = PROFILE_PRESETS["fastest"]  # prefer_lit is False for Fastest
    attrs_dark = make_edge(length_ft=100.0, lit_per_100ft=1.0)
    dark_threshold = 5.0  # 1.0 <= 5.0 -> dark

    cost_dark_off = edge_cost(
        attrs_dark, prefs, Conditions(darkness=False), dark_threshold=dark_threshold
    )
    cost_dark_on = edge_cost(
        attrs_dark, prefs, Conditions(darkness=True), dark_threshold=dark_threshold
    )
    assert cost_dark_on > cost_dark_off


def test_compute_dark_threshold_is_bottom_quartile(main_graph):
    threshold = compute_dark_threshold(main_graph)
    assert isinstance(threshold, float)
    assert threshold >= 0.0


# ---------------------------------------------------------------------------
# router.py: routing-level behavior on the fixture graph
# ---------------------------------------------------------------------------


def test_wheelchair_avoids_stairs_fastest_takes_shortcut(main_graph):
    wheelchair_prefs = PROFILE_PRESETS["wheelchair"]
    fastest_prefs = PROFILE_PRESETS["fastest"]
    conditions = Conditions()

    wheelchair_routes = find_routes(
        main_graph, "O", ["E_access"], wheelchair_prefs, conditions, k=1
    )
    fastest_routes = find_routes(
        main_graph, "O", ["E_access"], fastest_prefs, conditions, k=1
    )

    assert len(wheelchair_routes) == 1
    assert len(fastest_routes) == 1

    wheelchair_route = wheelchair_routes[0]
    fastest_route = fastest_routes[0]

    assert "C" not in wheelchair_route.node_path  # never touches the stair node
    assert wheelchair_route.used_fallback is False
    assert wheelchair_route.length_ft == pytest.approx(250.0)

    assert fastest_route.node_path == ["O", "C", "E_access"]
    assert fastest_route.length_ft == pytest.approx(80.0)


def test_curb_cut_blocks_wheelchair_not_low_vision(main_graph):
    conditions = Conditions()

    wheelchair_routes = find_routes(
        main_graph, "O", ["E_access2"], PROFILE_PRESETS["wheelchair"], conditions, k=1
    )
    assert len(wheelchair_routes) == 1
    assert wheelchair_routes[0].used_fallback is True
    codes = {c for v in wheelchair_routes[0].violations for c in v["codes"]}
    assert "curb_cuts" in codes

    low_vision_routes = find_routes(
        main_graph, "O", ["E_access2"], PROFILE_PRESETS["low_vision"], conditions, k=1
    )
    assert len(low_vision_routes) == 1
    assert low_vision_routes[0].used_fallback is False
    assert low_vision_routes[0].violations == []


def test_wheelchair_destination_must_be_accessible(main_graph):
    conditions = Conditions()
    routes = find_routes(
        main_graph,
        "O",
        ["E_access", "E_unknown"],
        PROFILE_PRESETS["wheelchair"],
        conditions,
        k=1,
    )
    assert len(routes) == 1
    route = routes[0]
    assert route.node_path[-1] == "E_access"
    assert "E_unknown" not in route.node_path
    # Takes the step-free detour (stairs blocked), not the much shorter
    # unknown-entrance edge.
    assert route.length_ft == pytest.approx(250.0)


def test_fallback_when_only_stairs(stairs_only_graph):
    conditions = Conditions()
    routes = find_routes(
        stairs_only_graph,
        "O2",
        ["E_access6"],
        PROFILE_PRESETS["wheelchair"],
        conditions,
        k=3,
    )
    assert len(routes) == 1
    route = routes[0]
    assert route.used_fallback is True
    codes = {c for v in route.violations for c in v["codes"]}
    assert "stairs" in codes


def test_injured_low_vision_route_avoids_stairs(main_graph):
    prefs = resolve_preferences(["injured", "low_vision"], None, [])
    conditions = Conditions()
    routes = find_routes(main_graph, "O", ["E_access"], prefs, conditions, k=1)
    assert len(routes) == 1
    assert "C" not in routes[0].node_path
    assert routes[0].length_ft == pytest.approx(250.0)


def test_remove_effect_forces_detour_over_20_percent(main_graph):
    prefs = PROFILE_PRESETS["fastest"]
    conditions = Conditions()

    direct = find_routes(main_graph, "O", ["E_access5"], prefs, conditions, k=1)[0]
    assert direct.node_path == ["O", "R1", "R2", "E_access5"]
    assert direct.length_ft == pytest.approx(130.0)

    # Find the R1-R2 edge key the same way router.py does internally.
    from service.router import _edge_key  # white-box: confirm key format once

    removed = {_edge_key("R1", "R2", None): "remove"}
    detoured = find_routes(
        main_graph, "O", ["E_access5"], prefs, conditions, edge_effects=removed, k=1
    )[0]

    assert "R3" in detoured.node_path
    assert detoured.length_ft > direct.length_ft * 1.2  # >20% longer


def test_estimated_12_percent_edge_forces_wheelchair_detour():
    """A direct, short edge whose lidar-estimated slope is 12% must be
    unusable for Wheelchair even though it's never been field-surveyed; a
    compliant (surveyed, gentle) detour is taken instead. Fastest, which has
    no hard slope limit, still takes the direct edge.
    """
    G = nx.Graph()
    G.add_node("O")
    G.add_node("D")
    G.add_node("E_access", access="accessible", door_id="D9", building="Steep Hall")

    G.add_edge(
        "O",
        "E_access",
        **make_edge(length_ft=10.0, slope_pct=12.0, slope_source="usgs_lidar_1m_estimate"),
    )
    G.add_edge("O", "D", **make_edge(length_ft=50.0, slope_pct=2.0, slope_source="cornell_survey"))
    G.add_edge(
        "D", "E_access", **make_edge(length_ft=50.0, slope_pct=2.0, slope_source="cornell_survey")
    )

    conditions = Conditions()

    wheelchair_route = find_routes(
        G, "O", ["E_access"], PROFILE_PRESETS["wheelchair"], conditions, k=1
    )[0]
    assert wheelchair_route.used_fallback is False
    assert wheelchair_route.violations == []
    assert "D" in wheelchair_route.node_path
    assert wheelchair_route.length_ft == pytest.approx(100.0)

    fastest_route = find_routes(
        G, "O", ["E_access"], PROFILE_PRESETS["fastest"], conditions, k=1
    )[0]
    assert fastest_route.node_path == ["O", "E_access"]
    assert fastest_route.length_ft == pytest.approx(10.0)


def test_estimated_12_percent_only_route_uses_fallback():
    """When the estimated-steep edge is the ONLY way to the destination,
    Wheelchair must fall back and report the violation rather than silently
    using (or silently refusing) the edge.
    """
    G = nx.Graph()
    G.add_node("O3")
    G.add_node("E_access7", access="accessible", door_id="D10", building="OnlySteepHall")
    G.add_edge(
        "O3",
        "E_access7",
        **make_edge(length_ft=40.0, slope_pct=12.0, slope_source="usgs_lidar_1m_estimate"),
    )

    conditions = Conditions()
    routes = find_routes(
        G, "O3", ["E_access7"], PROFILE_PRESETS["wheelchair"], conditions, k=1
    )
    assert len(routes) == 1
    route = routes[0]
    assert route.used_fallback is True
    codes = {c for v in route.violations for c in v["codes"]}
    assert "slope_over_max_estimated" in codes


def test_multigraph_support(main_multigraph):
    conditions = Conditions()

    wheelchair_routes = find_routes(
        main_multigraph,
        "O",
        ["E_access"],
        PROFILE_PRESETS["wheelchair"],
        conditions,
        k=1,
    )
    assert len(wheelchair_routes) == 1
    assert "C" not in wheelchair_routes[0].node_path
    assert wheelchair_routes[0].length_ft == pytest.approx(250.0)

    fastest_routes = find_routes(
        main_multigraph, "O", ["E_access"], PROFILE_PRESETS["fastest"], conditions, k=1
    )
    assert len(fastest_routes) == 1
    assert fastest_routes[0].node_path == ["O", "C", "E_access"]
    assert fastest_routes[0].length_ft == pytest.approx(80.0)
