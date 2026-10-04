"""Profile presets, penalty weights, and preference resolution.

CLAUDE.md §5 (preference keys) and §6 (team defaults / combining rules) are
binding; plan §8.3 is encoded in PROFILE_PRESETS. See module docstrings in
cost.py and router.py for how these values are consumed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Graph-vocabulary constants. These describe strings/values produced by
# build_graph.py. Confirmed against the real P1 build (graph.pkl: MultiGraph,
# 4070 nodes, 6092 edges) by P4 -- see CLAUDE.md Known issues (P1) for the
# source facts. Kept in one place so later prompts only edit this block if
# the pipeline's vocabulary ever changes.
# ---------------------------------------------------------------------------
KIND_SIDEWALK = "sidewalk"
KIND_CROSSWALK = "crosswalk"
KIND_SNAP = "snap"
KIND_DOOR_APPROACH = "door approach"
KIND_INFERRED = "inferred"

SLOPE_SOURCE_SURVEYED = "cornell_survey"
SLOPE_SOURCE_ESTIMATE = "usgs_lidar_1m_estimate"
# "unknown" is used for every unsurveyed edge until P2's --elevation/lidar
# fill runs (and will remain for segments <20 ft afterward, per plan §5.2).
# cost.py's edge_cost handles it structurally (the `else` branch after the
# surveyed/estimate checks), not by name; this constant exists so other
# modules (e.g. explain.py) can refer to it by name instead of a bare string.
SLOPE_SOURCE_UNKNOWN = "unknown"

# [DECISION] (P4-fix2) Lidar-estimated slopes are noisy (~1-2 points) but an
# estimate this far over the ADA ramp limit is very likely a real barrier,
# not noise. Above this threshold, an estimated-slope edge is hard-blocked
# for any profile whose own max_slope_pct is at or below the threshold (i.e.
# wheelchair); estimated slopes from 5% up to this threshold still only get
# the existing heavy cost penalty (cost.py), not a hard block. Surveyed-slope
# hard limits (SLOPE_SOURCE_SURVEYED) and "unknown" handling are unchanged.
ESTIMATED_SLOPE_HARD_PCT = 10.0

# Surface values that get an extra roughness penalty. Confirmed present in
# the real final graph (cornell_data/graph/graph.pkl, post-lidar), with
# total edge length per value: "unknown" (3729 edges / 60862.5 ft),
# "concrete" (1511 / 59565.2), "asphalt" (445 / 26571.2), "crosswalk"
# (146 / 4230.4), "cast stone" (70 / 1748.6), "concrete/other" (47 / 1611.7),
# "concrete tile" (43 / 1081.9), "brick" (27 / 983.2), "tile" (16 / 770.8),
# "stone" (26 / 443.9), "asphalt/other" (11 / 383.2), "gravel" (10 / 362.6),
# "mulch" (2 / 194.5), "cobblestone" (4 / 148.6), "dirt" (3 / 107.1),
# "other" (1 / 26.8), "granite" (1 / 8.5).
#
# [DECISION] (P4-fix) extended ROUGH_SURFACES with "dirt", "mulch", "stone",
# "granite", and "cast stone" -- natural/unfinished surfaces that read as
# rough underfoot/under-wheel, matching the existing gravel/cobblestone/brick
# entries in kind. Left "concrete tile" and "asphalt" (and asphalt/other,
# concrete/other, plain "tile", "other", "unknown", "crosswalk") unchanged:
# asphalt and concrete tile are engineered, generally smooth walking
# surfaces despite the name; the "/other" and bare "unknown"/"other"/"tile"
# values are ambiguous rather than confirmed-rough, so no penalty is
# assumed without more data.
ROUGH_SURFACES = {
    "gravel",
    "cobblestone",
    "brick",
    "dirt",
    "mulch",
    "stone",
    "granite",
    "cast stone",
}

# Preference keys a caller may override via POST /route "preferences" (CLAUDE.md
# §5). walking_speed_ftps and the internal-only fields below are NOT
# overridable directly; they are derived from the chosen profiles.
OVERRIDABLE_PREFERENCE_KEYS = {
    "avoid_stairs",
    "max_slope_pct",
    "max_cross_slope_pct",
    "min_width_ft",
    "prefer_lit",
    "surface_sensitivity",
    "distance_tolerance",
    "require_curb_cuts",
    "require_accessible_entrance",
    "ramp_required_above_pct",
}

KNOWN_ADJUSTMENTS = {"in_a_hurry", "carrying_items", "walking_alone"}

# F1 (P13-lite): preference-chip ids and importance levels a caller may
# send via POST /route "priorities" (not yet part of CLAUDE.md §5's
# preferences JSON -- see docs/API.md for the table this encodes).
PRIORITY_CHIP_IDS = {"avoid_stairs", "avoid_steep", "curb_cuts", "accessible_entrance", "well_lit"}
PRIORITY_LEVELS = {"essential", "important", "nice"}

# Penalty-scale multiplier applied per chip at "important"/"nice" (CLAUDE.md
# Known issues, F1); well_lit's "essential" case is handled separately
# (resolve_preferences) since it uses x5, not x3/x1.5.
_PRIORITY_SCALE_BY_LEVEL = {"important": 3.0, "nice": 1.5}


@dataclass
class ResolvedPrefs:
    avoid_stairs: bool = False
    max_slope_pct: float = math.inf
    max_cross_slope_pct: float = math.inf
    min_width_ft: float = 0.0
    prefer_lit: bool = False
    surface_sensitivity: int = 0
    distance_tolerance: float = 1.0
    require_curb_cuts: bool = False
    require_accessible_entrance: bool = False
    ramp_required_above_pct: float = math.inf
    walking_speed_ftps: float = 4.3
    # Internal-only (not part of §5 JSON; not directly overridable):
    dark_penalty_multiplier: float = 1.0
    penalize_missing_handrail: bool = False
    carrying_items: bool = False
    profiles: tuple = field(default_factory=tuple)
    # F1 (P13-lite): per-penalty-term multipliers from "important"/"nice"
    # priority chips (CLAUDE.md Known issues). Keys: "stairs", "slope",
    # "curb_cuts", "lighting". Empty by default, so edge_cost()'s
    # `.get(key, 1.0)` lookups are a no-op and every pre-F1 test is
    # unaffected.
    penalty_scale: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Plan §8.3 profile defaults. CLAUDE.md §6: walking speeds fastest/low_vision/
# night_walk 4.3 ft/s, wheelchair 3.3, injured 2.5.
# ---------------------------------------------------------------------------
PROFILE_PRESETS: dict[str, ResolvedPrefs] = {
    "wheelchair": ResolvedPrefs(
        avoid_stairs=True,
        max_slope_pct=8.33,
        max_cross_slope_pct=2.0,
        min_width_ft=3.0,
        prefer_lit=False,
        surface_sensitivity=2,
        distance_tolerance=3.0,
        require_curb_cuts=True,
        require_accessible_entrance=True,
        ramp_required_above_pct=5.0,
        walking_speed_ftps=3.3,
    ),
    "injured": ResolvedPrefs(
        avoid_stairs=True,
        max_slope_pct=math.inf,  # soft penalty only per plan §8.3
        max_cross_slope_pct=math.inf,
        min_width_ft=0.0,
        prefer_lit=False,
        surface_sensitivity=1,
        distance_tolerance=1.5,
        require_curb_cuts=False,
        require_accessible_entrance=False,
        ramp_required_above_pct=math.inf,
        walking_speed_ftps=2.5,
        penalize_missing_handrail=True,
    ),
    "low_vision": ResolvedPrefs(
        avoid_stairs=False,
        max_slope_pct=math.inf,
        max_cross_slope_pct=math.inf,
        min_width_ft=0.0,
        prefer_lit=True,
        surface_sensitivity=2,
        distance_tolerance=1.5,
        require_curb_cuts=False,
        require_accessible_entrance=False,
        ramp_required_above_pct=math.inf,
        walking_speed_ftps=4.3,
        dark_penalty_multiplier=1.0,
    ),
    "night_walk": ResolvedPrefs(
        avoid_stairs=False,
        max_slope_pct=math.inf,
        max_cross_slope_pct=math.inf,
        min_width_ft=0.0,
        prefer_lit=True,
        surface_sensitivity=0,
        distance_tolerance=1.3,
        require_curb_cuts=False,
        require_accessible_entrance=False,
        ramp_required_above_pct=math.inf,
        walking_speed_ftps=4.3,
        dark_penalty_multiplier=2.0,  # "strong" dark penalty per plan §8.3
    ),
    "fastest": ResolvedPrefs(
        avoid_stairs=False,
        max_slope_pct=math.inf,
        max_cross_slope_pct=math.inf,
        min_width_ft=0.0,
        prefer_lit=False,
        surface_sensitivity=0,
        distance_tolerance=1.0,
        require_curb_cuts=False,
        require_accessible_entrance=False,
        ramp_required_above_pct=math.inf,
        walking_speed_ftps=4.3,
    ),
}

for _name, _preset in PROFILE_PRESETS.items():
    PROFILE_PRESETS[_name] = (
        _preset.__class__(**{**_preset.__dict__, "profiles": (_name,)})
    )
del _name, _preset


# ---------------------------------------------------------------------------
# Penalty weights (plan §8.2, team defaults §6). All tunable here, not in
# code comments elsewhere. See report for the rationale behind each value.
# ---------------------------------------------------------------------------
PENALTY_WEIGHTS: dict[str, float] = {
    # multiplier-units added per percentage point of (surveyed) slope.
    "slope_per_pct": 0.15,
    # flat addition when slope_source is the lidar estimate (on top of the
    # proportional term below), so estimated segments always cost more than
    # a same-slope surveyed one, and are never hard-blocked (§6).
    "estimated_slope_flat": 0.5,
    # flat addition when slope is unknown (<20 ft unsurveyed segment, §5).
    "unknown_slope_flat": 0.3,
    # multiplier-units added per percentage point of cross slope.
    "cross_slope_per_pct": 0.20,
    # per defect_level (0-3), scaled by prefs.surface_sensitivity (0-3).
    "surface_defect_per_level": 0.10,
    # flat addition (scaled by surface_sensitivity) for rough surfaces.
    "surface_rough": 0.15,
    # per missing curb cut (0,1,2 needed), only when not hard-blocked.
    "curb_cut_missing": 0.30,
    # dark-segment penalty; scaled by prefs.dark_penalty_multiplier.
    "lighting_dark": 0.40,
    # inferred/unverified edges.
    "unverified": 0.20,
    # injured: steep (>5%) surveyed segment with no handrail.
    "no_handrail_steep": 0.15,
    # condition multipliers (CLAUDE.md §6).
    "ice_slope_multiplier": 2.0,
    "ice_unverified_multiplier": 1.5,
    # adjustment: carrying_items doubles the stair's own cost, where stairs
    # are still allowed (avoid_stairs False). Applied as a multiplier on the
    # whole edge cost, not just the slope term, since "stair cost" has no
    # separate slope component.
    "carrying_items_stair_multiplier": 2.0,
    # fallback rule: heavy flat addition per violated hard limit so the
    # fallback route still prefers fewer/lesser violations over more.
    "fallback_violation_penalty": 50.0,
}


def resolve_preferences(
    profiles: list[str],
    overrides: dict | None,
    adjustments: list[str],
    priorities: dict | None = None,
) -> ResolvedPrefs:
    """Combine one or more profile presets, apply overrides and adjustments.

    Combining rules (CLAUDE.md §6): union of hard limits, max of each
    penalty, slowest walking speed. Unknown profile names, unknown
    adjustment names, or unknown preference override keys raise ValueError.

    F1 (P13-lite) `priorities`: {chip_id: "essential"|"important"|"nice"}.
    When `profiles` is empty/missing, defaults to `["fastest"]` so a
    priorities-only request still resolves. Applied *after* profiles and
    `overrides` (same layering as `overrides`, so it can only add
    restrictions on top of whatever the profiles/overrides already set, not
    loosen them). Unknown chip ids or levels raise ValueError (-> 422).
    """
    if not profiles:
        profiles = ["fastest"]

    for p in profiles:
        if p not in PROFILE_PRESETS:
            raise ValueError(f"unknown profile: {p!r}")

    adjustments = adjustments or []
    for a in adjustments:
        if a not in KNOWN_ADJUSTMENTS:
            raise ValueError(f"unknown adjustment: {a!r}")

    overrides = overrides or {}
    for k in overrides:
        if k not in OVERRIDABLE_PREFERENCE_KEYS:
            raise ValueError(f"unknown preference key: {k!r}")

    priorities = priorities or {}
    for chip_id, level in priorities.items():
        if chip_id not in PRIORITY_CHIP_IDS:
            raise ValueError(f"unknown priority chip id: {chip_id!r}")
        if level not in PRIORITY_LEVELS:
            raise ValueError(f"unknown priority level: {level!r}")

    result = ResolvedPrefs(
        avoid_stairs=False,
        max_slope_pct=math.inf,
        max_cross_slope_pct=math.inf,
        min_width_ft=0.0,
        prefer_lit=False,
        surface_sensitivity=0,
        distance_tolerance=1.0,
        require_curb_cuts=False,
        require_accessible_entrance=False,
        ramp_required_above_pct=math.inf,
        walking_speed_ftps=math.inf,
        dark_penalty_multiplier=1.0,
        penalize_missing_handrail=False,
        carrying_items=False,
        profiles=tuple(profiles),
    )

    for p in profiles:
        preset = PROFILE_PRESETS[p]
        result.avoid_stairs = result.avoid_stairs or preset.avoid_stairs
        result.max_slope_pct = min(result.max_slope_pct, preset.max_slope_pct)
        result.max_cross_slope_pct = min(
            result.max_cross_slope_pct, preset.max_cross_slope_pct
        )
        result.min_width_ft = max(result.min_width_ft, preset.min_width_ft)
        result.prefer_lit = result.prefer_lit or preset.prefer_lit
        result.surface_sensitivity = max(
            result.surface_sensitivity, preset.surface_sensitivity
        )
        result.distance_tolerance = max(
            result.distance_tolerance, preset.distance_tolerance
        )
        result.require_curb_cuts = result.require_curb_cuts or preset.require_curb_cuts
        result.require_accessible_entrance = (
            result.require_accessible_entrance or preset.require_accessible_entrance
        )
        result.ramp_required_above_pct = min(
            result.ramp_required_above_pct, preset.ramp_required_above_pct
        )
        result.walking_speed_ftps = min(
            result.walking_speed_ftps, preset.walking_speed_ftps
        )
        result.dark_penalty_multiplier = max(
            result.dark_penalty_multiplier, preset.dark_penalty_multiplier
        )
        result.penalize_missing_handrail = (
            result.penalize_missing_handrail or preset.penalize_missing_handrail
        )

    for key, value in overrides.items():
        setattr(result, key, value)

    for chip_id, level in priorities.items():
        if chip_id == "avoid_stairs":
            if level == "essential":
                result.avoid_stairs = True
            else:
                result.penalty_scale["stairs"] = _PRIORITY_SCALE_BY_LEVEL[level]
        elif chip_id == "avoid_steep":
            if level == "essential":
                wheelchair = PROFILE_PRESETS["wheelchair"]
                result.max_slope_pct = min(result.max_slope_pct, 8.33)
                result.ramp_required_above_pct = min(result.ramp_required_above_pct, 5.0)
                # F3 (Task 0): also apply the wheelchair preset's clear-width
                # hard limit and cross-slope figure, read from the preset
                # rather than hardcoded, so "avoid steep slopes" implies the
                # same minimum passable width a wheelchair profile requires.
                # (min_width_ft is cost.py's only actual hard limit of the
                # two; max_cross_slope_pct has no hard-limit check anywhere
                # in cost.py today -- it only affects explanation/stat
                # wording, same as it does for the real wheelchair profile.)
                result.min_width_ft = max(result.min_width_ft, wheelchair.min_width_ft)
                result.max_cross_slope_pct = min(result.max_cross_slope_pct, wheelchair.max_cross_slope_pct)
            else:
                result.penalty_scale["slope"] = _PRIORITY_SCALE_BY_LEVEL[level]
        elif chip_id == "curb_cuts":
            if level == "essential":
                result.require_curb_cuts = True
            else:
                result.penalty_scale["curb_cuts"] = _PRIORITY_SCALE_BY_LEVEL[level]
        elif chip_id == "accessible_entrance":
            if level == "essential":
                result.require_accessible_entrance = True
            # "important"/"nice": no-op here -- app.py's destination-entrance
            # filter already prefers accessible entrances by default.
        elif chip_id == "well_lit":
            result.prefer_lit = True
            if level != "essential":
                result.penalty_scale["lighting"] = _PRIORITY_SCALE_BY_LEVEL[level]
            else:
                result.penalty_scale["lighting"] = 5.0
            # "essential" can't be a hard limit (no per-edge "is lit" cutoff
            # exists); app.py adds a warning explaining this.

    if "in_a_hurry" in adjustments:
        result.distance_tolerance = min(result.distance_tolerance, 1.1)
    if "carrying_items" in adjustments:
        result.carrying_items = True
    if "walking_alone" in adjustments:
        result.prefer_lit = True

    return result
