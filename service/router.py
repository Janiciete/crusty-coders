"""Routing core: Dijkstra over a personalized cost graph, multi-destination,
alternatives, and the fallback rule (plan §8.2-8.4; CLAUDE.md §6).

Graph-specific details are kept to the small helpers at the top of this file
(`_edge_items`, `_edge_key`) so P4 can adapt them once the real graph.pkl
schema from P1 is known. Everything else (adjacency building, Dijkstra,
alternatives, fallback) is graph-shape agnostic and works for both
`networkx.Graph` and `networkx.MultiGraph`.

[VERIFY after P1]: edges are treated as fully undirected here (a route may
traverse any edge in either direction, and slope/cross-slope attributes are
assumed to apply the same regardless of direction of travel). If the real
pipeline encodes slope direction (uphill vs downhill), this assumption must
be revisited together with whichever edges end up directed.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field

from service.conditions import Conditions
from service.cost import compute_dark_threshold, edge_cost, hard_limit_violations
from service.profiles import PENALTY_WEIGHTS, ResolvedPrefs

# ---------------------------------------------------------------------------
# Graph-shape helpers. [VERIFY after P1]: real graph is expected to be a
# networkx.Graph or MultiGraph; both are supported identically below.
# ---------------------------------------------------------------------------


def _edge_items(G):
    """Yield (u, v, key, attrs) for every edge in G.

    key is the MultiGraph edge key, or None for a plain Graph.
    """
    if G.is_multigraph():
        for u, v, key, attrs in G.edges(keys=True, data=True):
            yield u, v, key, attrs
    else:
        for u, v, attrs in G.edges(data=True):
            yield u, v, None, attrs


def _edge_key(u, v, key):
    """Canonical, direction-independent id for an edge.

    Used both to look up `edge_effects` and to report which edges a route
    used / violated. Stable regardless of which endpoint traversal started
    from.
    """
    a, b = sorted((u, v), key=repr)
    return (a, b, key if key is not None else 0)


@dataclass
class RouteResult:
    edge_keys: list = field(default_factory=list)
    node_path: list = field(default_factory=list)
    length_ft: float = 0.0
    cost: float = 0.0
    violations: list = field(default_factory=list)
    used_fallback: bool = False
    # Tier 4 only (see find_routes): edge_keys of "remove"-effect (reported
    # blocked-path) edges this route was forced to use because no route
    # avoiding them existed even under the relaxed/widened earlier tiers.
    forced_blocked_edges: list = field(default_factory=list)


def _edge_lengths(G) -> dict:
    lengths = {}
    for u, v, key, attrs in _edge_items(G):
        lengths[_edge_key(u, v, key)] = attrs.get("length_ft", 0.0)
    return lengths


# Tier 4 (find_routes): a reported blocked path ("remove" edge effect) is
# let through as a very heavy penalty instead of excluding the edge, so a
# live report can never make a trip between two listed buildings outright
# impossible -- it just becomes a strongly discouraged last resort. Chosen
# as a large multiple of the existing per-violation fallback penalty so a
# forced-blocked edge is always worse than any ordinary hard-limit
# violation, but a finite number (never None/inf) so Dijkstra still works
# and a route is still returned when this is truly the only way through.
_FORCED_BLOCKED_PENALTY = PENALTY_WEIGHTS["fallback_violation_penalty"] * 20


def _build_adjacency(
    G,
    prefs: ResolvedPrefs,
    conditions: Conditions,
    edge_effects: dict,
    dark_threshold: float,
    enforce_hard_limits: bool,
    remove_as_penalty: bool = False,
) -> dict:
    """node -> list of (neighbor, edge_key, attrs, cost, violations, forced_blocked).

    When enforce_hard_limits is True, edges with any hard-limit violation
    are left out of the adjacency entirely (impassable). When False (the
    fallback pass), they are kept but penalized heavily and their
    violations are recorded so the caller can report them.

    An edge effect of "remove" blocks the edge (cost.edge_cost returns
    None for it) unless remove_as_penalty is True (tier 4 only), in which
    case the edge is kept passable with a very heavy flat penalty
    (_FORCED_BLOCKED_PENALTY) instead, and `forced_blocked` is True for it
    so the caller can report it (CLAUDE.md §6: never silently drop a
    report; here, never silently drop the *trip* either).
    """
    adj: dict = {}
    for u, v, key, attrs in _edge_items(G):
        ekey = _edge_key(u, v, key)
        effect = edge_effects.get(ekey) if edge_effects else None

        violations = hard_limit_violations(attrs, prefs)
        if enforce_hard_limits and violations:
            continue

        forced_blocked = False
        cost_effect = effect
        if effect == "remove" and remove_as_penalty:
            forced_blocked = True
            cost_effect = None  # bypass cost.py's hard block; penalize below instead

        cost = edge_cost(attrs, prefs, conditions, effect=cost_effect, dark_threshold=dark_threshold)
        if cost is None:
            continue

        if forced_blocked:
            cost += _FORCED_BLOCKED_PENALTY

        if not enforce_hard_limits and violations:
            cost += PENALTY_WEIGHTS["fallback_violation_penalty"] * len(violations)

        adj.setdefault(u, []).append((v, ekey, attrs, cost, violations, forced_blocked))
        adj.setdefault(v, []).append((u, ekey, attrs, cost, violations, forced_blocked))
    return adj


def _dijkstra(adj: dict, origin, targets, edge_multiplier: dict | None = None):
    """Shortest path from origin to the nearest node in `targets`.

    Returns (target_node_or_None, total_cost, prev) where prev maps
    node -> (prev_node, edge_key, attrs, base_cost, violations).
    """
    edge_multiplier = edge_multiplier or {}
    targets = set(targets)
    dist = {origin: 0.0}
    prev: dict = {}
    visited = set()
    counter = itertools.count()
    heap = [(0.0, next(counter), origin)]

    while heap:
        d, _, u = heapq.heappop(heap)
        if u in visited:
            continue
        visited.add(u)
        if u in targets:
            return u, d, prev
        for (v, ekey, attrs, cost, violations, forced_blocked) in adj.get(u, []):
            mult = edge_multiplier.get(ekey, 1.0)
            nd = d + cost * mult
            if v not in dist or nd < dist[v] - 1e-9:
                dist[v] = nd
                prev[v] = (u, ekey, attrs, cost, violations, forced_blocked)
                heapq.heappush(heap, (nd, next(counter), v))

    return None, math.inf, prev


def _reconstruct(prev: dict, origin, target):
    node_path = [target]
    edge_records = []  # (edge_key, attrs, violations, forced_blocked)
    cur = target
    while cur != origin:
        pv, ekey, attrs, _cost, violations, forced_blocked = prev[cur]
        edge_records.append((ekey, attrs, violations, forced_blocked))
        node_path.append(pv)
        cur = pv
    node_path.reverse()
    edge_records.reverse()

    edge_keys = [ek for ek, _a, _v, _f in edge_records]
    length_ft = sum(a.get("length_ft", 0.0) for _ek, a, _v, _f in edge_records)
    violations_out = [
        {"edge_key": ek, "codes": v} for ek, _a, v, _f in edge_records if v
    ]
    forced_blocked_edges = [ek for ek, _a, _v, f in edge_records if f]
    return edge_keys, node_path, length_ft, violations_out, forced_blocked_edges


def find_routes(
    G,
    origin,
    destinations: list,
    prefs: ResolvedPrefs,
    conditions: Conditions,
    edge_effects: dict | None = None,
    k: int = 3,
) -> list[RouteResult]:
    """Rank routes from origin to the cheapest of several destinations.

    Dijkstra runs to a temporary super-sink conceptually; in practice we run
    a multi-target Dijkstra (equivalent, cheaper) and stop at the first
    target popped. Destinations are filtered first: if
    prefs.require_accessible_entrance, any destination node whose `access`
    attribute exists and is not "accessible" is dropped ("unknown" is never
    chosen, per CLAUDE.md §5).

    Fallback rule (CLAUDE.md §6: "never silently drops limits" -- and never
    silently drops the trip either): four tiers, each tried only if the
    previous one finds no route to any eligible destination at all.

      1. Strict: hard limits enforced; destinations filtered to
         accessible-only when `prefs.require_accessible_entrance` (exactly
         today's pre-existing behavior -- no regression).
      2. Hard limits relaxed into a heavy per-violation penalty (the
         pre-existing fallback rule); same destination set as tier 1.
      3. Also accept any routable destination entrance, even when
         `require_accessible_entrance` is set -- a non-accessible door is
         no longer an exclusion, just something the caller (app.py /
         explain.py) can see from the chosen route's destination node and
         report as a violation/`fit` reason.
      4. Also let a "remove" edge effect (a reported blocked path) through
         as a very heavy penalty instead of excluding the edge, so a live
         report can never make the whole trip impossible by itself.

    The single best route found at whichever tier first succeeds is used
    as the base for the alternatives search below; `used_fallback` is True
    for any tier past the first, and `forced_blocked_edges` on the
    resulting RouteResult(s) is non-empty only when tier 4 was needed.

    Alternatives: up to k routes, each subsequent search penalizing the
    previous routes' edges x1.5 (compounding); a candidate whose length
    overlaps a previously accepted route by more than 80% (by shared
    length / min route length) is rejected and its edges penalized again
    before retrying (CLAUDE.md §6).
    """
    edge_effects = edge_effects or {}

    if origin not in G.nodes:
        return []

    def _eligible(widen: bool) -> list:
        elig = []
        for d in destinations:
            if d not in G.nodes:
                continue
            if not widen and prefs.require_accessible_entrance:
                access = G.nodes[d].get("access")
                if access is not None and access != "accessible":
                    continue
            elig.append(d)
        return elig

    narrow_eligible = _eligible(widen=False)
    wide_eligible = _eligible(widen=True)
    if not wide_eligible:
        return []

    dark_threshold = compute_dark_threshold(G)
    edge_lengths = _edge_lengths(G)

    tiers = [
        (narrow_eligible, True, False),
        (narrow_eligible, False, False),
        (wide_eligible, False, False),
        (wide_eligible, False, True),
    ]

    target = dist = prev = None
    eligible = adj = None
    tier_used = 0
    for i, (elig, enforce_hard, remove_as_penalty) in enumerate(tiers, start=1):
        if not elig:
            continue
        candidate_adj = _build_adjacency(
            G, prefs, conditions, edge_effects, dark_threshold, enforce_hard, remove_as_penalty
        )
        t, d, p = _dijkstra(candidate_adj, origin, elig)
        if t is not None:
            target, dist, prev = t, d, p
            eligible, adj = elig, candidate_adj
            tier_used = i
            break

    if target is None:
        return []

    used_fallback = tier_used > 1

    routes: list[RouteResult] = []
    accepted: list[tuple[set, float]] = []
    edge_multiplier: dict = {}

    attempts = 0
    max_attempts = max(k * 5, 5)
    cur_target, cur_dist, cur_prev = target, dist, prev

    while len(routes) < k and attempts < max_attempts:
        attempts += 1
        if routes:
            cur_target, cur_dist, cur_prev = _dijkstra(adj, origin, eligible, edge_multiplier)
        if cur_target is None:
            break

        edge_keys, node_path, length_ft, violations, forced_blocked_edges = _reconstruct(
            cur_prev, origin, cur_target
        )
        edge_key_set = set(edge_keys)

        overlaps_prior = False
        for prior_keys, prior_length in accepted:
            shared_length = sum(edge_lengths.get(ek, 0.0) for ek in (edge_key_set & prior_keys))
            denom = min(length_ft, prior_length)
            if denom > 0 and (shared_length / denom) > 0.8:
                overlaps_prior = True
                break

        if overlaps_prior:
            for ek in edge_key_set:
                edge_multiplier[ek] = edge_multiplier.get(ek, 1.0) * 1.5
            continue

        routes.append(
            RouteResult(
                edge_keys=edge_keys,
                node_path=node_path,
                length_ft=length_ft,
                cost=cur_dist,
                violations=violations,
                used_fallback=used_fallback,
                forced_blocked_edges=forced_blocked_edges,
            )
        )
        accepted.append((edge_key_set, length_ft))
        for ek in edge_key_set:
            edge_multiplier[ek] = edge_multiplier.get(ek, 1.0) * 1.5

    return routes
