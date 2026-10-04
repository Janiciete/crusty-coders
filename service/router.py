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


def _edge_lengths(G) -> dict:
    lengths = {}
    for u, v, key, attrs in _edge_items(G):
        lengths[_edge_key(u, v, key)] = attrs.get("length_ft", 0.0)
    return lengths


def _build_adjacency(
    G,
    prefs: ResolvedPrefs,
    conditions: Conditions,
    edge_effects: dict,
    dark_threshold: float,
    enforce_hard_limits: bool,
) -> dict:
    """node -> list of (neighbor, edge_key, attrs, cost, violations).

    When enforce_hard_limits is True, edges with any hard-limit violation
    are left out of the adjacency entirely (impassable). When False (the
    fallback pass), they are kept but penalized heavily and their
    violations are recorded so the caller can report them.

    An edge effect of "remove" always blocks the edge, in both passes,
    since that represents a physical closure (a live report), not a
    preference.
    """
    adj: dict = {}
    for u, v, key, attrs in _edge_items(G):
        ekey = _edge_key(u, v, key)
        effect = edge_effects.get(ekey) if edge_effects else None

        violations = hard_limit_violations(attrs, prefs)
        if enforce_hard_limits and violations:
            continue

        cost = edge_cost(attrs, prefs, conditions, effect=effect, dark_threshold=dark_threshold)
        if cost is None:
            continue

        if not enforce_hard_limits and violations:
            cost += PENALTY_WEIGHTS["fallback_violation_penalty"] * len(violations)

        adj.setdefault(u, []).append((v, ekey, attrs, cost, violations))
        adj.setdefault(v, []).append((u, ekey, attrs, cost, violations))
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
        for (v, ekey, attrs, cost, violations) in adj.get(u, []):
            mult = edge_multiplier.get(ekey, 1.0)
            nd = d + cost * mult
            if v not in dist or nd < dist[v] - 1e-9:
                dist[v] = nd
                prev[v] = (u, ekey, attrs, cost, violations)
                heapq.heappush(heap, (nd, next(counter), v))

    return None, math.inf, prev


def _reconstruct(prev: dict, origin, target):
    node_path = [target]
    edge_records = []  # (edge_key, attrs, violations)
    cur = target
    while cur != origin:
        pv, ekey, attrs, _cost, violations = prev[cur]
        edge_records.append((ekey, attrs, violations))
        node_path.append(pv)
        cur = pv
    node_path.reverse()
    edge_records.reverse()

    edge_keys = [ek for ek, _a, _v in edge_records]
    length_ft = sum(a.get("length_ft", 0.0) for _ek, a, _v in edge_records)
    violations_out = [
        {"edge_key": ek, "codes": v} for ek, _a, v in edge_records if v
    ]
    return edge_keys, node_path, length_ft, violations_out


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

    Fallback rule: if no destination is reachable while enforcing hard
    limits, hard limits are relaxed into a heavy per-violation penalty and
    the search is retried; the single best route found this way is
    returned with `used_fallback=True` and every violating edge listed in
    `violations`.

    Alternatives: up to k routes, each subsequent search penalizing the
    previous routes' edges x1.5 (compounding); a candidate whose length
    overlaps a previously accepted route by more than 80% (by shared
    length / min route length) is rejected and its edges penalized again
    before retrying (CLAUDE.md §6).
    """
    edge_effects = edge_effects or {}

    if origin not in G.nodes:
        return []

    eligible = []
    for d in destinations:
        if d not in G.nodes:
            continue
        if prefs.require_accessible_entrance:
            access = G.nodes[d].get("access")
            if access is not None and access != "accessible":
                continue
        eligible.append(d)
    if not eligible:
        return []

    dark_threshold = compute_dark_threshold(G)
    edge_lengths = _edge_lengths(G)

    adj_strict = _build_adjacency(G, prefs, conditions, edge_effects, dark_threshold, True)
    target, dist, prev = _dijkstra(adj_strict, origin, eligible)

    used_fallback = False
    adj = adj_strict
    if target is None:
        adj = _build_adjacency(G, prefs, conditions, edge_effects, dark_threshold, False)
        target, dist, prev = _dijkstra(adj, origin, eligible)
        used_fallback = True
        if target is None:
            return []

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

        edge_keys, node_path, length_ft, violations = _reconstruct(cur_prev, origin, cur_target)
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
            )
        )
        accepted.append((edge_key_set, length_ft))
        for ek in edge_key_set:
            edge_multiplier[ek] = edge_multiplier.get(ek, 1.0) * 1.5

    return routes
