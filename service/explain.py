"""Route statistics and plain-language explanations (plan §8.5-8.6; Prompt 4
task 3). Pure functions over a `router.RouteResult`, the graph, and the
resolved preferences -- no I/O, no Supabase, no FastAPI.

§8.5 stats (all returned by `compute_stats`): distance_ft, est_time_min,
steps_avoided, max_slope_pct, max_cross_slope_pct, pct_lit,
destination_entrance, reports_avoided, unverified_segments,
estimated_segments.

§8.6 explanation lines (`build_explanation`) are template-based, never call
anything "safe" (CLAUDE.md §7/§11), and label lidar-estimated slopes and
inferred/unverified edges as "estimated" / "unverified" rather than stating
them as fact.
"""

from __future__ import annotations

from service.profiles import PROFILE_PRESETS, ResolvedPrefs, SLOPE_SOURCE_ESTIMATE

_FASTEST_SPEED_FTPS = PROFILE_PRESETS["fastest"].walking_speed_ftps


def _edge_data(G, edge_key) -> dict:
    a, b, k = edge_key
    data = G.get_edge_data(a, b, k)
    if data is None:
        data = G.get_edge_data(b, a, k)
    return data or {}


def _route_edge_data(G, route) -> list[dict]:
    return [_edge_data(G, ek) for ek in route.edge_keys]


def _edge_id(G, edge_key) -> str:
    a, b, k = edge_key
    data = _edge_data(G, edge_key)
    return data.get("edge_id") or f"{a}|{b}|{k}"


def _is_dark(attrs: dict, dark_threshold: float) -> bool:
    lit = attrs.get("lit_per_100ft")
    return lit is not None and lit <= dark_threshold


# ---------------------------------------------------------------------------
# §8.5 stats
# ---------------------------------------------------------------------------


def compute_stats(
    G,
    route,
    fastest_route,
    prefs: ResolvedPrefs,
    dark_threshold: float,
    report_edge_map: dict[str, list] | None = None,
) -> dict:
    report_edge_map = report_edge_map or {}
    edges = _route_edge_data(G, route)
    length_ft = route.length_ft

    est_time_min = None
    if prefs.walking_speed_ftps and prefs.walking_speed_ftps > 0:
        est_time_min = length_ft / prefs.walking_speed_ftps / 60.0

    steps_this = sum(a.get("steps") or 0 for a in edges)
    if fastest_route is not None:
        fastest_edges = _route_edge_data(G, fastest_route)
        steps_fastest = sum(a.get("steps") or 0 for a in fastest_edges)
    else:
        steps_fastest = steps_this
    steps_avoided = max(0, steps_fastest - steps_this)

    slopes = [a["slope_pct"] for a in edges if a.get("slope_pct") is not None]
    max_slope_pct = max(slopes) if slopes else 0.0

    cross_slopes = [
        a["cross_slope_pct"] for a in edges if a.get("cross_slope_pct") is not None
    ]
    max_cross_slope_pct = max(cross_slopes) if cross_slopes else 0.0

    lit_length = sum(
        a.get("length_ft", 0.0) for a in edges if not _is_dark(a, dark_threshold)
    )
    pct_lit = (lit_length / length_ft * 100.0) if length_ft > 0 else 0.0

    dest_node = route.node_path[-1] if route.node_path else None
    destination_entrance = None
    if dest_node is not None:
        dnode = G.nodes[dest_node]
        if "building" in dnode:
            destination_entrance = {
                "door_id": dnode.get("door_id"),
                "building": dnode.get("building"),
                "access": dnode.get("access"),
                "auto_opener": dnode.get("auto_opener"),
            }

    reports_avoided = 0
    if fastest_route is not None and report_edge_map:
        fastest_keys = set(fastest_route.edge_keys)
        this_keys = set(route.edge_keys)
        touched_fastest = {
            rid
            for rid, ekeys in report_edge_map.items()
            if fastest_keys & {tuple(ek) for ek in ekeys}
        }
        reports_avoided = sum(
            1
            for rid in touched_fastest
            if not (this_keys & {tuple(ek) for ek in report_edge_map[rid]})
        )

    unverified_segments = sum(
        1 for a in edges if a.get("inferred") or a.get("verified") is False
    )
    estimated_segments = sum(
        1 for a in edges if a.get("slope_source") == SLOPE_SOURCE_ESTIMATE
    )

    return {
        "distance_ft": round(length_ft, 1),
        "est_time_min": round(est_time_min, 1) if est_time_min is not None else None,
        "steps_avoided": steps_avoided,
        "max_slope_pct": round(max_slope_pct, 2),
        "max_cross_slope_pct": round(max_cross_slope_pct, 2),
        "pct_lit": round(pct_lit, 1),
        "destination_entrance": destination_entrance,
        "reports_avoided": reports_avoided,
        "unverified_segments": unverified_segments,
        "estimated_segments": estimated_segments,
    }


# ---------------------------------------------------------------------------
# Segments (CLAUDE.md §5 response contract)
# ---------------------------------------------------------------------------


def build_segments(G, route) -> list[dict]:
    segments = []
    for edge_key in route.edge_keys:
        data = _edge_data(G, edge_key)
        segments.append(
            {
                "edge_id": _edge_id(G, edge_key),
                "slope_pct": data.get("slope_pct"),
                "slope_source": data.get("slope_source"),
                "cross_slope_pct": data.get("cross_slope_pct"),
                "date_surveyed": data.get("date_surveyed"),
                "verified": data.get("verified"),
            }
        )
    return segments


# ---------------------------------------------------------------------------
# Violations: name the segment and the standard (task 3)
# ---------------------------------------------------------------------------

_RAMP_LIMIT = 8.33
_WALKWAY_LIMIT = 5.0
_CROSS_LIMIT = 2.0


def _slope_limit_label(limit: float) -> str:
    if limit == float("inf"):
        return "slope limit"
    if abs(limit - _RAMP_LIMIT) < 0.01:
        return "ADA ramp limit"
    if abs(limit - _WALKWAY_LIMIT) < 0.01:
        return "ADA walkway limit"
    return f"{limit:.2f}% slope limit"


def _violation_message(data: dict, code: str, prefs: ResolvedPrefs, edge_id: str) -> str:
    slope = data.get("slope_pct")
    if code == "stairs":
        steps = data.get("steps")
        step_txt = f" ({steps} steps)" if steps else ""
        return f"segment {edge_id}: stairs{step_txt}, not usable with this profile's avoid-stairs limit"
    if code == "slope_over_max":
        label = _slope_limit_label(prefs.max_slope_pct)
        slope_txt = f"{slope:.0f}%" if slope is not None else "unmeasured"
        return (
            f"segment {edge_id}: {slope_txt} slope, exceeds the "
            f"{prefs.max_slope_pct:.2f}% {label}"
        )
    if code == "slope_needs_ramp":
        slope_txt = f"{slope:.0f}%" if slope is not None else "unmeasured"
        return (
            f"segment {edge_id}: {slope_txt} slope with no ramp marked, exceeds the "
            f"{prefs.ramp_required_above_pct:.1f}% threshold above which ADA requires a ramp"
        )
    if code == "slope_over_max_estimated":
        label = _slope_limit_label(prefs.max_slope_pct)
        slope_txt = f"{slope:.1f}%" if slope is not None else "unmeasured"
        return (
            f"segment {edge_id}: {slope_txt} slope (estimated from lidar), exceeds the "
            f"{prefs.max_slope_pct:.2f}% {label}"
        )
    if code == "curb_cuts":
        cc = data.get("curb_cuts")
        return (
            f"segment {edge_id}: only {cc} of 2 required curb cuts present "
            f"(ADA curb-cut standard)"
        )
    if code == "width":
        width = data.get("width_ft")
        width_txt = f"{width:.1f} ft" if width is not None else "unmeasured width"
        return (
            f"segment {edge_id}: {width_txt}, narrower than the "
            f"{prefs.min_width_ft:.1f} ft ADA minimum clear width"
        )
    return f"segment {edge_id}: violates {code}"


def build_violations(G, route, prefs: ResolvedPrefs) -> list[dict]:
    out = []
    for v in route.violations:
        edge_key = v["edge_key"]
        data = _edge_data(G, edge_key)
        edge_id = _edge_id(G, edge_key)
        for code in v["codes"]:
            out.append(
                {
                    "edge_id": edge_id,
                    "code": code,
                    "message": _violation_message(data, code, prefs, edge_id),
                }
            )
    return out


# ---------------------------------------------------------------------------
# §8.6 explanation lines
# ---------------------------------------------------------------------------


def build_explanation(
    G,
    route,
    stats: dict,
    prefs: ResolvedPrefs,
    fastest_route,
) -> list[str]:
    lines: list[str] = []
    edges = _route_edge_data(G, route)

    stairs_this = {
        ek for ek, a in zip(route.edge_keys, edges) if a.get("is_stairs")
    }
    stairs_fastest: set = set()
    if fastest_route is not None:
        fastest_edges = _route_edge_data(G, fastest_route)
        stairs_fastest = {
            ek
            for ek, a in zip(fastest_route.edge_keys, fastest_edges)
            if a.get("is_stairs")
        }
    avoided_stairs = stairs_fastest - stairs_this
    if avoided_stairs and stats["steps_avoided"] > 0:
        plural = "s" if len(avoided_stairs) != 1 else ""
        lines.append(
            f"Avoids {len(avoided_stairs)} staircase{plural} ({stats['steps_avoided']} steps)"
        )

    max_slope = stats["max_slope_pct"]
    slope_is_estimated = any(
        a.get("slope_source") == SLOPE_SOURCE_ESTIMATE
        and a.get("slope_pct") is not None
        and abs(a["slope_pct"] - max_slope) < 1e-6
        for a in edges
    )
    est_txt = " (estimated)" if slope_is_estimated else ""
    within_txt = "within" if max_slope <= _WALKWAY_LIMIT else "exceeds"
    lines.append(
        f"Maximum slope: {max_slope:.1f}%{est_txt} ({within_txt} the {_WALKWAY_LIMIT:.0f}% ADA walkway limit)"
    )

    max_cross = stats["max_cross_slope_pct"]
    within_cross_txt = "within" if max_cross <= _CROSS_LIMIT else "exceeds"
    lines.append(
        f"Maximum cross slope: {max_cross:.1f}% ({within_cross_txt} the {_CROSS_LIMIT:.0f}% ADA limit)"
    )

    de = stats["destination_entrance"]
    if de is not None:
        access = de.get("access")
        if access == "accessible":
            acc_txt = "an accessible entrance"
        elif access == "unknown":
            acc_txt = "an entrance of unverified accessibility"
        else:
            acc_txt = "a non-accessible entrance"
        opener_txt = " with an automatic door" if de.get("auto_opener") else ""
        lines.append(f"Ends at {acc_txt}{opener_txt}")

    lines.append(f"{stats['pct_lit']:.0f}% of the route is lit")

    if fastest_route is not None and stats["est_time_min"] is not None:
        fastest_time_min = fastest_route.length_ft / _FASTEST_SPEED_FTPS / 60.0
        diff = stats["est_time_min"] - fastest_time_min
        if diff >= 0.1:
            lines.append(f"{diff:.1f} minutes longer than the shortest route")

    if stats["reports_avoided"] > 0:
        plural = "s" if stats["reports_avoided"] != 1 else ""
        lines.append(
            f"Avoids {stats['reports_avoided']} active report{plural} along the shortest route"
        )

    if stats["estimated_segments"] > 0:
        plural = "s" if stats["estimated_segments"] != 1 else ""
        lines.append(
            f"Includes {stats['estimated_segments']} estimated-slope segment{plural} (lidar-based, not yet field-surveyed)"
        )
    if stats["unverified_segments"] > 0:
        plural = "s" if stats["unverified_segments"] != 1 else ""
        lines.append(
            f"Includes {stats['unverified_segments']} unverified/inferred segment{plural}"
        )

    for v in build_violations(G, route, prefs):
        lines.append(f"Does not fully meet standards here: {v['message']}")

    return lines


# ---------------------------------------------------------------------------
# `fit` (Prompt A task 2): a route-level "does this actually meet what you
# asked for" summary, grouped by type (never one line per edge), with no
# disability labels, never "safe", and estimated slopes labeled "estimated"
# -- same rules as build_explanation. Separate from (and does not change)
# `violations`/`warnings`.
# ---------------------------------------------------------------------------

_SLOPE_VIOLATION_CODES = {"slope_over_max", "slope_needs_ramp", "slope_over_max_estimated"}


def _violation_edge_keys(route, *codes: str) -> set:
    wanted = set(codes)
    return {v["edge_key"] for v in route.violations if wanted & set(v["codes"])}


def build_fit(G, route, stats: dict, prefs: ResolvedPrefs, baseline_length_ft: float | None) -> dict:
    """{"status": "full"|"partial", "reasons": [...]}.

    "partial" whenever the route has any hard-limit violation (fallback
    tiers 2-4), a non-accessible destination door when an accessible one
    was required, a forced-through blocked-path edge (tier 4), or a length
    beyond `prefs.distance_tolerance` vs. the unconstrained baseline.
    `reasons` are short, plain-language lines grouped by type.
    """
    reasons: list[str] = []

    stairs_edges = _violation_edge_keys(route, "stairs")
    if stairs_edges:
        steps = sum((_edge_data(G, ek).get("steps") or 0) for ek in stairs_edges)
        plural = "s" if len(stairs_edges) != 1 else ""
        step_txt = f" ({steps} steps)" if steps else ""
        reasons.append(f"Uses {len(stairs_edges)} staircase{plural}{step_txt}")

    slope_edges = _violation_edge_keys(route, *_SLOPE_VIOLATION_CODES)
    if slope_edges:
        max_slope = 0.0
        any_estimated = False
        for ek in slope_edges:
            data = _edge_data(G, ek)
            slope = data.get("slope_pct")
            if slope is not None:
                max_slope = max(max_slope, slope)
            if data.get("slope_source") == SLOPE_SOURCE_ESTIMATE:
                any_estimated = True
        limit = prefs.max_slope_pct
        if limit == float("inf"):
            limit = prefs.ramp_required_above_pct
        limit_txt = f"{limit:.0f}%" if limit != float("inf") else "slope"
        plural = "es" if len(slope_edges) != 1 else ""
        est_txt = ", estimated" if any_estimated else ""
        reasons.append(
            f"{len(slope_edges)} short stretch{plural} steeper than your {limit_txt} limit "
            f"(steepest {max_slope:.1f}%{est_txt})"
        )

    curb_cut_edges = _violation_edge_keys(route, "curb_cuts")
    if curb_cut_edges:
        plural = "s" if len(curb_cut_edges) != 1 else ""
        reasons.append(f"{len(curb_cut_edges)} crossing{plural} without full curb cuts")

    width_edges = _violation_edge_keys(route, "width")
    if width_edges:
        plural = "es" if len(width_edges) != 1 else ""
        reasons.append(
            f"{len(width_edges)} stretch{plural} narrower than your "
            f"{prefs.min_width_ft:.1f} ft minimum"
        )

    de = stats.get("destination_entrance")
    if prefs.require_accessible_entrance and de is not None and de.get("access") != "accessible":
        reasons.append("Ends at a door not marked accessible")

    if route.forced_blocked_edges:
        reasons.append("Uses a reported blocked path that couldn't be avoided on this trip")

    if baseline_length_ft and baseline_length_ft > 0:
        ratio = route.length_ft / baseline_length_ft
        if ratio > prefs.distance_tolerance + 1e-6:
            reasons.append(f"{ratio:.1f}× longer than you said you'd walk")

    return {"status": "partial" if reasons else "full", "reasons": reasons}
