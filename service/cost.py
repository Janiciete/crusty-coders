"""Hard limits and the per-edge cost function (plan §8.2, §8.4; CLAUDE.md §6).

Nothing here reads files, the network, or Supabase; it only operates on
edge-attribute dicts and the `ResolvedPrefs` / `Conditions` objects produced
by profiles.py / conditions.py.
"""

from __future__ import annotations

import math

from service.conditions import Conditions
from service.profiles import (
    ESTIMATED_SLOPE_HARD_PCT,
    PENALTY_WEIGHTS,
    ROUGH_SURFACES,
    SLOPE_SOURCE_ESTIMATE,
    SLOPE_SOURCE_SURVEYED,
    ResolvedPrefs,
)


def hard_limit_violations(attrs: dict, prefs: ResolvedPrefs) -> list[str]:
    """Return the hard-limit violation codes an edge trips for these prefs.

    Codes: "stairs", "slope_over_max", "slope_needs_ramp",
    "slope_over_max_estimated", "curb_cuts", "width". Slope limits apply to
    surveyed slopes (slope_source == "cornell_survey") in full (§6); lidar
    estimates (slope_source == "usgs_lidar_1m_estimate") are hard-blocked
    only above ESTIMATED_SLOPE_HARD_PCT, and only for profiles whose own
    max_slope_pct is at or below that threshold (CLAUDE.md §6, P4-fix2) --
    lidar noise is a couple of points, so an estimate that far over the ADA
    ramp limit is very likely a real barrier. Estimated slopes at or below
    the threshold, and "unknown" slopes, are never hard-blocked; they only
    get a cost penalty (see edge_cost).
    """
    codes: list[str] = []

    if prefs.avoid_stairs and attrs.get("is_stairs"):
        codes.append("stairs")

    slope = attrs.get("slope_pct") or 0.0
    slope_source = attrs.get("slope_source")
    if slope_source == SLOPE_SOURCE_SURVEYED:
        if slope > prefs.max_slope_pct:
            codes.append("slope_over_max")
        elif slope > prefs.ramp_required_above_pct and not attrs.get("ramp"):
            codes.append("slope_needs_ramp")
    elif slope_source == SLOPE_SOURCE_ESTIMATE:
        if slope > ESTIMATED_SLOPE_HARD_PCT and prefs.max_slope_pct <= ESTIMATED_SLOPE_HARD_PCT:
            codes.append("slope_over_max_estimated")

    curb_cuts = attrs.get("curb_cuts")
    if prefs.require_curb_cuts and curb_cuts is not None and curb_cuts < 2:
        codes.append("curb_cuts")

    width = attrs.get("width_ft")
    if width is not None and width < prefs.min_width_ft:
        codes.append("width")

    return codes


def edge_cost(
    attrs: dict,
    prefs: ResolvedPrefs,
    conditions: Conditions,
    effect=None,
    *,
    dark_threshold: float,
) -> float | None:
    """Cost of traversing one edge, or None if it is blocked.

    effect: None, "remove" (physically blocked — a live report, always
    wins over everything else), or a float multiplier (penalizes the edge,
    e.g. a live ice/too_steep/too_dark report).

    Formula (plan §8.2): length * (1 + slope + cross-slope + surface +
    lighting + unverified penalties), then condition multipliers (CLAUDE.md
    §6): ice x2 on slope penalties and x1.5 on unverified penalties;
    darkness turns on the lighting penalty for every profile. Finally the
    carrying_items adjustment doubles the whole cost of stair edges where
    stairs are still allowed.
    """
    if effect == "remove":
        return None

    length = attrs.get("length_ft", 0.0)

    # --- slope ---
    slope = attrs.get("slope_pct") or 0.0
    slope_source = attrs.get("slope_source")
    if slope_source == SLOPE_SOURCE_SURVEYED:
        slope_penalty = PENALTY_WEIGHTS["slope_per_pct"] * slope
    elif slope_source == SLOPE_SOURCE_ESTIMATE:
        slope_penalty = (
            PENALTY_WEIGHTS["slope_per_pct"] * slope
            + PENALTY_WEIGHTS["estimated_slope_flat"]
        )
    else:
        slope_penalty = PENALTY_WEIGHTS["unknown_slope_flat"]

    # --- cross slope ---
    cross = attrs.get("cross_slope_pct") or 0.0
    cross_penalty = PENALTY_WEIGHTS["cross_slope_per_pct"] * cross

    # --- surface (defects + roughness), scaled by surface_sensitivity ---
    defect = attrs.get("defect_level") or 0
    surface_penalty = PENALTY_WEIGHTS["surface_defect_per_level"] * defect * prefs.surface_sensitivity
    if attrs.get("surface") in ROUGH_SURFACES:
        surface_penalty += PENALTY_WEIGHTS["surface_rough"] * prefs.surface_sensitivity

    # --- curb cuts (crosswalks); only a penalty when not hard-blocking ---
    curb_cuts = attrs.get("curb_cuts")
    if curb_cuts is not None and curb_cuts < 2 and not prefs.require_curb_cuts:
        surface_penalty += PENALTY_WEIGHTS["curb_cut_missing"] * (2 - curb_cuts)

    # --- missing handrail on a steep surveyed segment (injured) ---
    if (
        prefs.penalize_missing_handrail
        and slope_source == SLOPE_SOURCE_SURVEYED
        and slope > 5.0
        and not attrs.get("handrail")
    ):
        surface_penalty += PENALTY_WEIGHTS["no_handrail_steep"]

    # --- unverified / inferred ---
    unverified_penalty = 0.0
    if attrs.get("inferred") or attrs.get("verified") is False:
        unverified_penalty = PENALTY_WEIGHTS["unverified"]

    # --- lighting ---
    lighting_penalty = 0.0
    lit = attrs.get("lit_per_100ft")
    is_dark = lit is not None and lit <= dark_threshold
    if is_dark and (prefs.prefer_lit or conditions.darkness):
        lighting_penalty = PENALTY_WEIGHTS["lighting_dark"] * prefs.dark_penalty_multiplier

    # --- condition multipliers ---
    if conditions.ice:
        slope_penalty *= PENALTY_WEIGHTS["ice_slope_multiplier"]
        unverified_penalty *= PENALTY_WEIGHTS["ice_unverified_multiplier"]

    total_penalty = (
        slope_penalty
        + cross_penalty
        + surface_penalty
        + lighting_penalty
        + unverified_penalty
    )

    cost = length * (1.0 + total_penalty)

    # --- live-report penalty multiplier (float effect) ---
    if isinstance(effect, (int, float)):
        cost *= float(effect)

    # --- carrying_items: doubles stair cost where stairs are still allowed ---
    if prefs.carrying_items and attrs.get("is_stairs"):
        cost *= PENALTY_WEIGHTS["carrying_items_stair_multiplier"]

    return cost


def compute_dark_threshold(G) -> float:
    """Bottom quartile of lit_per_100ft across all edges in G (CLAUDE.md §6).

    Edges missing lit_per_100ft are ignored. If no edge has the attribute,
    returns 0.0 (nothing will be considered dark).
    """
    values = []
    for _u, _v, attrs in G.edges(data=True):
        lit = attrs.get("lit_per_100ft")
        if lit is not None:
            values.append(lit)

    if not values:
        return 0.0

    values.sort()
    n = len(values)
    # Simple quartile: position at 25% into the sorted list (nearest-rank).
    idx = max(0, min(n - 1, math.ceil(0.25 * n) - 1))
    return values[idx]
