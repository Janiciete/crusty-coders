"""Tests for F1 (P13-lite): POST /route's `priorities` chip table.

Fixture-graph / direct-function tests only, no network and no app.py
TestClient -- the chip table lives in service/profiles.py's
resolve_preferences() and service/cost.py's edge_cost(), both of which take
plain dicts/ResolvedPrefs and never touch the network.
"""

from __future__ import annotations

import pytest

from service.cost import edge_cost
from service.conditions import Conditions
from service.profiles import PROFILE_PRESETS, resolve_preferences
from service.router import find_routes
from tests.conftest import make_edge

NO_CONDITIONS = Conditions(darkness=False, ice=False, source={}, warnings=[])


def _cost(attrs, prefs):
    return edge_cost(attrs, prefs, NO_CONDITIONS, dark_threshold=0.0)


# ---------------------------------------------------------------------------
# No priorities -> identical to Fastest
# ---------------------------------------------------------------------------


def test_no_priorities_matches_fastest_explicit():
    implicit = resolve_preferences([], None, [], None)
    explicit = resolve_preferences(["fastest"], None, [])
    assert implicit == explicit


def test_empty_priorities_dict_is_a_no_op():
    with_empty = resolve_preferences(["fastest"], None, [], {})
    without = resolve_preferences(["fastest"], None, [])
    assert with_empty == without


# ---------------------------------------------------------------------------
# avoid_stairs
# ---------------------------------------------------------------------------


def test_avoid_stairs_essential_is_hard():
    prefs = resolve_preferences([], None, [], {"avoid_stairs": "essential"})
    assert prefs.avoid_stairs is True
    assert prefs.penalty_scale.get("stairs", 1.0) == 1.0


@pytest.mark.parametrize("level,expected_scale", [("important", 3.0), ("nice", 1.5)])
def test_avoid_stairs_soft_levels_scale_stair_cost(level, expected_scale):
    prefs = resolve_preferences([], None, [], {"avoid_stairs": level})
    assert prefs.avoid_stairs is False
    assert prefs.penalty_scale["stairs"] == expected_scale

    stair_edge = make_edge(is_stairs=True, slope_pct=None, slope_source=None)
    baseline = resolve_preferences([], None, [])
    assert _cost(stair_edge, prefs) == pytest.approx(_cost(stair_edge, baseline) * expected_scale)


def test_avoid_stairs_router_level_main_graph_essential(main_graph):
    prefs = resolve_preferences([], None, [], {"avoid_stairs": "essential"})
    result = find_routes(
        main_graph, "O", ["E_access"], prefs, NO_CONDITIONS, edge_effects={}, k=1
    )[0]
    assert all(not main_graph.edges[u, v].get("is_stairs") for u, v in zip(result.node_path, result.node_path[1:]))


# ---------------------------------------------------------------------------
# avoid_steep
# ---------------------------------------------------------------------------


def test_avoid_steep_essential_sets_hard_slope_and_ramp_limits():
    prefs = resolve_preferences([], None, [], {"avoid_steep": "essential"})
    assert prefs.max_slope_pct == 8.33
    assert prefs.ramp_required_above_pct == 5.0
    assert prefs.penalty_scale.get("slope", 1.0) == 1.0


def test_avoid_steep_essential_also_applies_wheelchair_width_and_cross_slope():
    # F3 Task 0: read dynamically from PROFILE_PRESETS["wheelchair"], not
    # hardcoded, so this stays correct if the preset's own values change.
    prefs = resolve_preferences([], None, [], {"avoid_steep": "essential"})
    wheelchair = PROFILE_PRESETS["wheelchair"]
    assert prefs.min_width_ft == wheelchair.min_width_ft
    assert prefs.max_cross_slope_pct == wheelchair.max_cross_slope_pct


@pytest.mark.parametrize("level,expected_scale", [("important", 3.0), ("nice", 1.5)])
def test_avoid_steep_soft_levels_scale_slope_cost(level, expected_scale):
    prefs = resolve_preferences([], None, [], {"avoid_steep": level})
    assert prefs.max_slope_pct == float("inf")  # unchanged, not hard
    assert prefs.penalty_scale["slope"] == expected_scale

    sloped_edge = make_edge(slope_pct=4.0, slope_source="cornell_survey")
    baseline = resolve_preferences([], None, [])
    base_cost = _cost(sloped_edge, baseline)
    scaled_cost = _cost(sloped_edge, prefs)
    # Only the slope term scales; length*(1+penalty) isn't a pure multiple of
    # the slope penalty alone, so compare the slope contribution directly.
    length = sloped_edge["length_ft"]
    base_slope_penalty = (base_cost / length) - 1.0
    scaled_slope_penalty = (scaled_cost / length) - 1.0
    assert scaled_slope_penalty == pytest.approx(base_slope_penalty * expected_scale)


# ---------------------------------------------------------------------------
# curb_cuts
# ---------------------------------------------------------------------------


def test_curb_cuts_essential_is_hard():
    prefs = resolve_preferences([], None, [], {"curb_cuts": "essential"})
    assert prefs.require_curb_cuts is True


@pytest.mark.parametrize("level,expected_scale", [("important", 3.0), ("nice", 1.5)])
def test_curb_cuts_soft_levels_scale_penalty(level, expected_scale):
    prefs = resolve_preferences([], None, [], {"curb_cuts": level})
    assert prefs.require_curb_cuts is False
    assert prefs.penalty_scale["curb_cuts"] == expected_scale

    crossing = make_edge(kind="crosswalk", curb_cuts=1)
    baseline = resolve_preferences([], None, [])
    base_cost = _cost(crossing, baseline)
    scaled_cost = _cost(crossing, prefs)
    assert scaled_cost > base_cost


# ---------------------------------------------------------------------------
# accessible_entrance (important/nice is a documented no-op here)
# ---------------------------------------------------------------------------


def test_accessible_entrance_essential_is_hard():
    prefs = resolve_preferences([], None, [], {"accessible_entrance": "essential"})
    assert prefs.require_accessible_entrance is True


@pytest.mark.parametrize("level", ["important", "nice"])
def test_accessible_entrance_soft_levels_are_a_noop(level):
    prefs = resolve_preferences([], None, [], {"accessible_entrance": level})
    baseline = resolve_preferences([], None, [])
    assert prefs.require_accessible_entrance is False
    assert prefs == baseline


# ---------------------------------------------------------------------------
# well_lit
# ---------------------------------------------------------------------------


def test_well_lit_essential_sets_prefer_lit_and_x5_scale_not_hard():
    prefs = resolve_preferences([], None, [], {"well_lit": "essential"})
    assert prefs.prefer_lit is True
    assert prefs.penalty_scale["lighting"] == 5.0


@pytest.mark.parametrize("level,expected_scale", [("important", 3.0), ("nice", 1.5)])
def test_well_lit_soft_levels(level, expected_scale):
    prefs = resolve_preferences([], None, [], {"well_lit": level})
    assert prefs.prefer_lit is True
    assert prefs.penalty_scale["lighting"] == expected_scale

    dark_edge = make_edge(lit_per_100ft=0.0)
    baseline_lit = resolve_preferences([], None, [], {"well_lit": "nice"})
    # Compare against prefer_lit=True but scale=1.0 to isolate the lighting-scale effect.
    baseline_lit.penalty_scale["lighting"] = 1.0
    base_cost = edge_cost(dark_edge, baseline_lit, NO_CONDITIONS, dark_threshold=100.0)
    scaled_cost = edge_cost(dark_edge, prefs, NO_CONDITIONS, dark_threshold=100.0)
    length = dark_edge["length_ft"]
    base_penalty = (base_cost / length) - 1.0
    scaled_penalty = (scaled_cost / length) - 1.0
    assert scaled_penalty == pytest.approx(base_penalty * expected_scale)


# ---------------------------------------------------------------------------
# Validation (-> 422 via resolve_preferences raising ValueError)
# ---------------------------------------------------------------------------


def test_unknown_chip_id_raises():
    with pytest.raises(ValueError):
        resolve_preferences([], None, [], {"bogus_chip": "essential"})


def test_unknown_level_raises():
    with pytest.raises(ValueError):
        resolve_preferences([], None, [], {"avoid_stairs": "bogus_level"})


# ---------------------------------------------------------------------------
# Combining with an explicit profile (profiles given, priorities layer on top)
# ---------------------------------------------------------------------------


def test_priorities_layer_on_top_of_explicit_profile():
    # "injured" doesn't require curb cuts by default; essential curb_cuts
    # should still add that hard requirement on top.
    prefs = resolve_preferences(["injured"], None, [], {"curb_cuts": "essential"})
    assert prefs.avoid_stairs is True  # from injured
    assert prefs.require_curb_cuts is True  # from priorities


def test_wheelchair_equivalent_priority_combo_matches_or_exceeds_wheelchair_preset(main_graph):
    priorities = {
        "avoid_stairs": "essential",
        "avoid_steep": "essential",
        "curb_cuts": "essential",
        "accessible_entrance": "essential",
    }
    prefs = resolve_preferences([], None, [], priorities)
    wheelchair = PROFILE_PRESETS["wheelchair"]

    assert prefs.avoid_stairs == wheelchair.avoid_stairs
    assert prefs.max_slope_pct <= wheelchair.max_slope_pct
    assert prefs.require_curb_cuts == wheelchair.require_curb_cuts
    assert prefs.require_accessible_entrance == wheelchair.require_accessible_entrance
    assert prefs.min_width_ft == wheelchair.min_width_ft  # F3 Task 0
    assert prefs.max_cross_slope_pct == wheelchair.max_cross_slope_pct  # F3 Task 0

    priority_route = find_routes(
        main_graph, "O", ["E_access"], prefs, NO_CONDITIONS, edge_effects={}, k=1
    )[0]
    wheelchair_route = find_routes(
        main_graph, "O", ["E_access"], wheelchair, NO_CONDITIONS, edge_effects={}, k=1
    )[0]
    assert len(priority_route.node_path) >= 1  # sanity: a route was found
    assert priority_route.length_ft >= wheelchair_route.length_ft - 1e-6
