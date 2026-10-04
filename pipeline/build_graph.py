"""Build the routing graph from fetched Cornell GIS layers.

Implements plan §5.2 in order: clip to the demo zone, normalize travel
labels, add crosswalks, snap/split/bridge the sidewalk network, match
stairs to polygons (and safety re-tag unlabeled stairs), convert dates,
flag suspect slopes, process entrances/door approaches, and attach
lighting density per edge.

Outputs (CLAUDE.md §3), written under cornell_data/graph/:
    edges.geojson  nodes.geojson  graph.pkl  seed_reports.geojson  build_report.txt

Usage:
    python pipeline/build_graph.py --bbox W S E N [--elevation]

--elevation (USGS lidar slope fill for unsurveyed segments) is P2 and
is not implemented here; passing it prints a notice and the elevation
step is skipped cleanly (everything else still runs).
"""

import argparse
import json
import math
import pickle
import re
import sys
from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString, Point, box
from shapely.ops import substring

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "cornell_data"
OUT_DIR = DATA_DIR / "graph"

WORK_CRS = "EPSG:2261"  # NY State Plane Central, US feet (native CRS of the source data)
WGS84 = "EPSG:4326"

SNAP_COINCIDENT_FT = 0.5     # endpoints closer than this are treated as the same point
SNAP_TOL_FT = 10.0           # endpoints within this get split/snap-connected
GAP_BRIDGE_FT = 35.0         # remaining disconnected components bridged up to this far
MIN_SEG_FT = 20.0            # segments shorter than this: slope stays "unknown" if unsurveyed
LONG_APPROACH_FT = 50.0      # door approaches longer than this are flagged
STAIR_MATCH_BUFFER_FT = 10.0  # buffer around stair polygons for matching stair-tagged segments
STAIR_RETAG_OVERLAP_FRAC = 0.1  # re-tag a non-stair segment if >= this fraction of its length
                                  # lies on a stair polygon (true intersection, no buffer)
ENTRANCE_ATTACH_FT = 100.0   # max distance to attach an entrance to the path network
LIGHTING_RADIUS_FT = 50.0    # fixtures within this of an edge count toward it
SUSPECT_SLOPE_PCT = 20.0     # non-stair slope above this is flagged suspect (plan §5.2, 51% outlier)

TRAVEL_MAP = {
    "preferred": "preferred",
    "potential prefered": "preferred_candidate",
    "potential preferred": "preferred_candidate",
    "stair": "stair",
    "steep": "steep",
    "ems": "service_route",
    "other": "other",
    "resurvey": "needs_check",
    "flag": "needs_check",
}
TRAVEL_NORMALIZED = {
    "preferred", "preferred_candidate", "stair", "steep",
    "service_route", "other", "unlabeled", "needs_check",
}


def _s(val) -> str:
    """Safe string coercion: None and NaN (float) both become "" instead of
    the literal "nan" or an AttributeError on .strip()."""
    if val is None:
        return ""
    if isinstance(val, float) and math.isnan(val):
        return ""
    return str(val)


def _f(val):
    """Safe float coercion: None/NaN become None."""
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def normalize_travel(raw) -> str:
    s = _s(raw).strip().lower()
    if s == "":
        return "unlabeled"
    return TRAVEL_MAP.get(s, "unlabeled")


def parse_width_ft(raw):
    """Parse the Walk Inventory `width` field: plain numbers (feet) or feet-inches
    like 8'3", using various quote characters seen in the real data."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    # normalize curly quotes to straight ones
    s = s.replace("’", "'").replace("‘", "'").replace("”", '"').replace("“", '"')
    try:
        return float(s)
    except ValueError:
        pass
    m = re.match(r"^(\d+(?:\.\d+)?)\s*'\s*(\d+(?:\.\d+)?)\s*\"?\s*$", s)
    if m:
        feet = float(m.group(1))
        inches = float(m.group(2))
        return feet + inches / 12.0
    m = re.match(r'^(\d+(?:\.\d+)?)\s*"\s*$', s)
    if m:
        return float(m.group(1)) / 12.0
    return None


DEFECT_SEVERITY = {"no defect": 0, "low": 1, "medium": 2, "high": 3}


def defect_level_from_fields(*raw_values) -> int:
    level = 0
    for raw in raw_values:
        if raw is None:
            continue
        s = str(raw).strip().lower()
        if s in ("", "no defect"):
            continue
        for word, sev in DEFECT_SEVERITY.items():
            if word != "no defect" and word in s:
                level = max(level, sev)
    return level


def epoch_ms_to_date(raw) -> str:
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return ""
    try:
        ts = pd.to_datetime(float(raw), unit="ms", utc=True)
        if pd.isna(ts):
            return ""
        return ts.strftime("%Y-%m-%d")
    except (ValueError, TypeError, OverflowError):
        return ""


def map_access_control(raw) -> str:
    s = _s(raw).strip().lower()
    if not s:
        return "open"
    if "card" in s:
        return "card"
    if "key" in s:
        return "key"
    return "open"


class NodeStore:
    """Allocates stable string node ids, keyed by exact (rounded) coordinate."""

    def __init__(self):
        self._by_coord = {}
        self._counter = 0
        self.points = {}  # node_id -> shapely Point (WORK_CRS)

    def _key(self, pt):
        return (round(pt.x / SNAP_COINCIDENT_FT), round(pt.y / SNAP_COINCIDENT_FT))

    def get_or_create(self, pt: Point) -> str:
        key = self._key(pt)
        if key in self._by_coord:
            return self._by_coord[key]
        node_id = f"N{self._counter:06d}"
        self._counter += 1
        self._by_coord[key] = node_id
        self.points[node_id] = pt
        return node_id

    def new_id(self, pt: Point) -> str:
        node_id = f"N{self._counter:06d}"
        self._counter += 1
        self.points[node_id] = pt
        return node_id


def explode_lines(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)
    return gdf[gdf.geometry.geom_type == "LineString"].copy()


def split_at_touch_points(lines: list) -> list:
    """For each line, split it wherever another line's endpoint lands on its
    interior within SNAP_TOL_FT (T-junction repair). `lines` is a list of dicts
    with a 'geom' LineString and an 'attrs' dict; returns a new list of the
    same shape (attrs copied to every sub-segment)."""
    endpoints = []
    endpoint_owner = []
    for i, rec in enumerate(lines):
        g = rec["geom"]
        coords = list(g.coords)
        endpoints.append(Point(coords[0]))
        endpoint_owner.append(i)
        endpoints.append(Point(coords[-1]))
        endpoint_owner.append(i)
    if not endpoints:
        return lines
    endpoints_gdf = gpd.GeoDataFrame({"owner": endpoint_owner}, geometry=endpoints)
    endpoints_sindex = endpoints_gdf.sindex

    split_distances = {i: set() for i in range(len(lines))}
    for i, rec in enumerate(lines):
        g = rec["geom"]
        length = g.length
        if length == 0:
            continue
        buffered = g.buffer(SNAP_TOL_FT)
        candidate_idx = list(endpoints_sindex.query(buffered, predicate="intersects"))
        for idx in candidate_idx:
            owner = endpoint_owner[idx]
            if owner == i:
                continue
            pt = endpoints[idx]
            dist_to_line = g.distance(pt)
            if dist_to_line > SNAP_TOL_FT:
                continue
            d = g.project(pt)
            if d <= 1e-6 or d >= length - 1e-6:
                continue  # it's basically at an endpoint already, no split needed
            split_distances[i].add(round(d, 3))

    new_lines = []
    for i, rec in enumerate(lines):
        g = rec["geom"]
        length = g.length
        dists = sorted(split_distances[i])
        if not dists:
            new_lines.append(rec)
            continue
        cut_points = [0.0] + dists + [length]
        for a, b in zip(cut_points, cut_points[1:]):
            if b - a < 1e-6:
                continue
            sub = substring(g, a, b)
            if sub.is_empty or sub.length < 1e-6:
                continue
            new_lines.append({"geom": sub, "attrs": rec["attrs"]})
    return new_lines


def build_edge_graph(lines: list, nodes: NodeStore):
    """Turn split lines into (u, v, geom, length_ft, attrs) tuples using the
    NodeStore for coincident-endpoint merging. Also returns a list of
    'snap' connector edges for endpoints within SNAP_TOL_FT of each other
    that were not already merged by exact coincidence or line-splitting."""
    raw_edges = []
    for rec in lines:
        g = rec["geom"]
        u = nodes.get_or_create(Point(g.coords[0]))
        v = nodes.get_or_create(Point(g.coords[-1]))
        if u == v:
            continue
        raw_edges.append((u, v, g, g.length, rec["attrs"]))

    # snap connectors: dangling endpoints within SNAP_TOL_FT of another dangling
    # endpoint belonging to a different node, not already connected.
    node_ids = list(nodes.points.keys())
    node_points = [nodes.points[n] for n in node_ids]
    node_gdf = gpd.GeoDataFrame({"node_id": node_ids}, geometry=node_points)
    node_sindex = node_gdf.sindex
    snap_edges = []
    seen_pairs = set()
    for a, pt in enumerate(node_points):
        candidate_idx = list(node_sindex.query(pt.buffer(SNAP_TOL_FT), predicate="intersects"))
        for b in candidate_idx:
            if b == a:
                continue
            na, nb = node_ids[a], node_ids[b]
            key = tuple(sorted((na, nb)))
            if key in seen_pairs:
                continue
            dist = pt.distance(node_points[b])
            if dist < 1e-6 or dist > SNAP_TOL_FT:
                continue
            seen_pairs.add(key)
            snap_edges.append((na, nb, LineString([pt, node_points[b]]), dist))
    return raw_edges, snap_edges


def bridge_gaps(G: nx.Graph, nodes: NodeStore):
    """Connect smaller connected components to their nearest neighbor (any other
    component) within GAP_BRIDGE_FT, as flagged 'inferred' edges. Returns the
    list of added (u, v, geom, length_ft) tuples."""
    components = list(nx.connected_components(G))
    if len(components) <= 1:
        return []
    comp_lengths = []
    for comp in components:
        total = sum(
            d.get("length_ft", 0.0) for u, v, d in G.subgraph(comp).edges(data=True)
        )
        comp_lengths.append(total)
    order = sorted(range(len(components)), key=lambda i: -comp_lengths[i])
    components = [components[i] for i in order]

    added = []
    main_component = set(components[0])
    for comp in components[1:]:
        comp_nodes = list(comp)
        comp_points = [nodes.points[n] for n in comp_nodes]
        main_nodes = list(main_component)
        main_points = [nodes.points[n] for n in main_nodes]
        if not main_points or not comp_points:
            continue
        main_gdf = gpd.GeoDataFrame({"node_id": main_nodes}, geometry=main_points)
        main_sindex = main_gdf.sindex
        best_dist = None
        best_pair = None
        for i, pt in enumerate(comp_points):
            res = main_sindex.nearest(pt, max_distance=GAP_BRIDGE_FT, return_distance=True, return_all=False)
            idx_arr, dist_arr = res
            if idx_arr.shape[1] == 0:
                continue
            j = int(idx_arr[1][0])
            dist = float(dist_arr[0])
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_pair = (comp_nodes[i], main_nodes[j])
        if best_pair is not None and best_dist is not None and best_dist <= GAP_BRIDGE_FT:
            u, v = best_pair
            geom = LineString([nodes.points[u], nodes.points[v]])
            added.append((u, v, geom, best_dist))
            main_component |= comp  # merge for subsequent iterations
        # else: stays disconnected; acceptable per plan (gap bridging only up to 35 ft)
    return added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), required=True)
    ap.add_argument("--elevation", action="store_true")
    args = ap.parse_args()

    W, S, E, N = args.bbox
    bbox_poly_wgs84 = box(W, S, E, N)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report_lines = []

    def log(msg=""):
        print(msg)
        report_lines.append(msg)

    log("=== build_graph.py ===")
    log(f"bbox (lon/lat): W={W} S={S} E={E} N={N}")
    log(f"elevation: {'requested' if args.elevation else 'not requested'}")
    if args.elevation:
        log("--elevation: lidar slope fill is not implemented here (P2). Skipping that step cleanly.")

    required = [
        "walk_inventory.geojson", "crosswalks.geojson", "entrances.geojson",
        "stairs.geojson", "lighting.geojson", "buildings.geojson",
    ]
    for fname in required:
        if not (DATA_DIR / fname).exists():
            log(f"ERROR: missing {DATA_DIR / fname}. Run fetch_cornell_data.py first.")
            return 1

    walk = gpd.read_file(DATA_DIR / "walk_inventory.geojson")
    crosswalks = gpd.read_file(DATA_DIR / "crosswalks.geojson")
    entrances = gpd.read_file(DATA_DIR / "entrances.geojson")
    stairs = gpd.read_file(DATA_DIR / "stairs.geojson")
    lighting = gpd.read_file(DATA_DIR / "lighting.geojson")
    buildings = gpd.read_file(DATA_DIR / "buildings.geojson")
    trip_hazards_path = DATA_DIR / "trip_hazards.geojson"
    trip_hazards = gpd.read_file(trip_hazards_path) if trip_hazards_path.exists() else None

    entrances = entrances[entrances.geometry.notna()].copy()

    # ---- clip to demo zone (in WGS84, then reproject) ----
    def clip(gdf):
        gdf = gdf[gdf.geometry.notna()].copy()
        gdf = gdf[gdf.geometry.intersects(bbox_poly_wgs84)].copy()
        gdf["geometry"] = gdf.geometry.intersection(bbox_poly_wgs84)
        gdf = gdf[~gdf.geometry.is_empty].copy()
        return gdf.to_crs(WORK_CRS)

    walk_c = clip(walk)
    crosswalks_c = clip(crosswalks)
    entrances_c = entrances[entrances.geometry.within(bbox_poly_wgs84)].copy().to_crs(WORK_CRS)
    stairs_c = clip(stairs)
    lighting_c = lighting[lighting.geometry.within(bbox_poly_wgs84)].copy().to_crs(WORK_CRS)
    buildings_all = buildings.to_crs(WORK_CRS)  # keep all buildings for name matching near the zone

    log(f"\nClipped to demo zone: walk={len(walk_c)} crosswalks={len(crosswalks_c)} "
        f"entrances={len(entrances_c)} stairs={len(stairs_c)} lighting={len(lighting_c)}")

    walk_c = explode_lines(walk_c)
    crosswalks_c = explode_lines(crosswalks_c)

    # ---- normalize Walk Inventory attributes ----
    sidewalk_lines = []
    stair_count_raw = 0
    for _, row in walk_c.iterrows():
        travel_norm = normalize_travel(row.get("travel"))
        if travel_norm == "stair":
            stair_count_raw += 1
        width_ft = parse_width_ft(row.get("width"))
        defect_level = defect_level_from_fields(row.get("cracking"), row.get("rgh_spall"), row.get("patch_grind"))
        slope_val = _f(row.get("slope_1"))
        has_slope = slope_val is not None
        length_ft = row.geometry.length
        slope_source = "cornell_survey" if has_slope else ("unknown")
        flags = []
        if has_slope and travel_norm != "stair" and slope_val > SUSPECT_SLOPE_PCT:
            flags.append("suspect_slope")
        cross_val = _f(row.get("crossslo_1"))
        material = _s(row.get("material_1")).strip().lower()
        attrs = {
            "kind": "sidewalk",
            "travel": travel_norm,
            "is_stairs": travel_norm == "stair",
            "steps": None,
            "landings": None,
            "rail_side": None,
            "slope_pct": slope_val,
            "slope_source": slope_source,
            "cross_slope_pct": cross_val,
            "width_ft": width_ft,
            "surface": material if material else "unknown",
            "defect_level": defect_level,
            "ramp": _s(row.get("ramp_1")).strip().lower() == "yes",
            "handrail": _s(row.get("handrail_1")).strip().lower() == "yes",
            "curb_cuts": None,
            "surveyed": _s(row.get("Complete")).strip().lower() == "yes",
            "date_surveyed": epoch_ms_to_date(row.get("date_surveyed")),
            "lit_fixtures": 0,
            "lit_per_100ft": 0.0,
            "lit_watts": 0.0,
            "inferred": False,
            "verified": False,
            "flags": flags,
        }
        attrs["verified"] = bool(attrs["surveyed"] and attrs["slope_source"] == "cornell_survey")
        sidewalk_lines.append({"geom": row.geometry, "attrs": attrs})

    # ---- crosswalks ----
    crosswalk_lines = []
    for _, row in crosswalks_c.iterrows():
        curb_raw = _s(row.get("curb_cut")).strip()
        try:
            curb_cuts = int(curb_raw)
            if curb_cuts not in (0, 1, 2):
                curb_cuts = None
        except (ValueError, TypeError):
            curb_cuts = None
        slope_val = _f(row.get("slope"))
        has_slope = slope_val is not None
        cross_val = _f(row.get("cross_slop"))
        attrs = {
            "kind": "crosswalk",
            "travel": "other",
            "is_stairs": False,
            "steps": None,
            "landings": None,
            "rail_side": None,
            "slope_pct": slope_val,
            "slope_source": "cornell_survey" if has_slope else "unknown",
            "cross_slope_pct": cross_val,
            "width_ft": None,
            "surface": "crosswalk",
            "defect_level": 0,
            "ramp": False,
            "handrail": False,
            "curb_cuts": curb_cuts,
            "surveyed": _s(row.get("updated")).strip().lower() == "yes",
            "date_surveyed": "",
            "lit_fixtures": 0,
            "lit_per_100ft": 0.0,
            "lit_watts": 0.0,
            "inferred": False,
            "verified": has_slope,
            "flags": [],
        }
        crosswalk_lines.append({"geom": row.geometry, "attrs": attrs})

    n_crosswalk_segments = len(crosswalk_lines)

    all_lines = sidewalk_lines + crosswalk_lines
    log(f"\nBefore splitting: {len(sidewalk_lines)} sidewalk segments, {len(crosswalk_lines)} crosswalk segments")

    all_lines = [rec for rec in all_lines if rec["geom"].length > 1e-6]
    all_lines = split_at_touch_points(all_lines)
    log(f"After touch-point splitting: {len(all_lines)} line segments")

    nodes = NodeStore()
    raw_edges, snap_edges = build_edge_graph(all_lines, nodes)
    log(f"Nodes after coincident-endpoint merge: {len(nodes.points)}")
    log(f"Snap connector edges (endpoints within {SNAP_TOL_FT:g} ft): {len(snap_edges)}")

    G = nx.MultiGraph()
    for n, pt in nodes.points.items():
        G.add_node(n, x=pt.x, y=pt.y, node_type="path")

    edge_counter = 0

    def add_edge(u, v, geom, length_ft, attrs):
        nonlocal edge_counter
        key = f"E{edge_counter:06d}"
        edge_counter += 1
        full_attrs = dict(attrs)
        full_attrs["edge_id"] = key
        full_attrs["length_ft"] = float(length_ft)
        full_attrs["geometry"] = geom
        G.add_edge(u, v, **full_attrs)
        return key

    for u, v, geom, length_ft, attrs in raw_edges:
        add_edge(u, v, geom, length_ft, attrs)

    for u, v, geom, dist in snap_edges:
        attrs = {
            "kind": "snap", "travel": "other", "is_stairs": False, "steps": None,
            "landings": None, "rail_side": None, "slope_pct": None, "slope_source": "unknown",
            "cross_slope_pct": None, "width_ft": None, "surface": "unknown", "defect_level": 0,
            "ramp": False, "handrail": False, "curb_cuts": None, "surveyed": False,
            "date_surveyed": "", "lit_fixtures": 0, "lit_per_100ft": 0.0, "lit_watts": 0.0,
            "inferred": False, "verified": False, "flags": ["snap_connector"],
        }
        add_edge(u, v, geom, dist, attrs)

    n_components_pre_bridge = nx.number_connected_components(G)
    bridges = bridge_gaps(G, nodes)
    for u, v, geom, dist in bridges:
        attrs = {
            "kind": "inferred", "travel": "other", "is_stairs": False, "steps": None,
            "landings": None, "rail_side": None, "slope_pct": None, "slope_source": "unknown",
            "cross_slope_pct": None, "width_ft": None, "surface": "unknown", "defect_level": 0,
            "ramp": False, "handrail": False, "curb_cuts": None, "surveyed": False,
            "date_surveyed": "", "lit_fixtures": 0, "lit_per_100ft": 0.0, "lit_watts": 0.0,
            "inferred": True, "verified": False, "flags": ["inferred_gap_bridge"],
        }
        add_edge(u, v, geom, dist, attrs)
    log(f"Connected components before gap bridging: {n_components_pre_bridge}")
    log(f"Gap-bridge 'inferred' edges added (<= {GAP_BRIDGE_FT:g} ft): {len(bridges)}")
    log(f"Connected components after gap bridging: {nx.number_connected_components(G)}")

    # ---- stairs: match stair-tagged segments to polygons, re-tag unlabeled stairs ----
    stair_sindex = stairs_c.sindex if len(stairs_c) else None
    matched = 0
    retagged = 0
    has_step_count = 0
    stair_tagged_total = 0
    for u, v, data in G.edges(data=True):
        geom = data["geometry"]
        is_sidewalk = data.get("kind") == "sidewalk"
        is_stair_tagged = is_sidewalk and data["travel"] == "stair"
        if is_stair_tagged:
            stair_tagged_total += 1
        if data.get("is_stairs") and not is_stair_tagged:
            continue  # already a stair (e.g. retagged earlier); nothing more to do
        if stair_sindex is None:
            continue
        buffered = geom.buffer(STAIR_MATCH_BUFFER_FT)
        candidate_idx = list(stair_sindex.query(buffered, predicate="intersects"))
        if not candidate_idx:
            continue
        candidates = stairs_c.iloc[candidate_idx]
        if is_stair_tagged:
            # attribute transfer from nearest matching polygon
            best = candidates.iloc[candidates.geometry.distance(geom).values.argmin()]
            matched += 1
            steps = best.get("Num_Tread")
            if steps is not None and not (isinstance(steps, float) and math.isnan(steps)):
                data["steps"] = int(steps)
                has_step_count += 1
            landings = best.get("Num_Land")
            if landings is not None and not (isinstance(landings, float) and math.isnan(landings)):
                data["landings"] = int(landings)
            rail = best.get("Rail_Side_1")
            if rail:
                data["rail_side"] = str(rail).strip().lower()
        else:
            # safety: a non-stair-tagged segment whose geometry meaningfully lies
            # on a stair polygon footprint (>= 10% of its own length inside the
            # polygon, true intersection with no buffer) must be re-tagged. The
            # 10% threshold avoids false positives from segments that merely
            # clip a polygon corner/edge after touch-point splitting.
            true_hits = candidates[candidates.geometry.intersects(geom)]
            overlap_len = sum(h.intersection(geom).length for h in true_hits.geometry) if len(true_hits) else 0.0
            if geom.length > 0 and (overlap_len / geom.length) >= STAIR_RETAG_OVERLAP_FRAC:
                data["is_stairs"] = True
                data["travel"] = "stair"
                data["flags"] = list(data.get("flags", [])) + ["retagged_stair"]
                data["slope_pct"] = None
                data["slope_source"] = "unknown"
                retagged += 1

    log(f"\nStair segments tagged by Walk Inventory (travel=stair): {stair_tagged_total}")
    log(f"Stair segments matched to a stair polygon (within {STAIR_MATCH_BUFFER_FT:g} ft): {matched}")
    log(f"Matched stair segments with a step count: {has_step_count}")
    log(f"Non-stair segments re-tagged as stairs (true polygon overlap): {retagged}")

    # stairs have no meaningful slope
    for u, v, data in G.edges(data=True):
        if data.get("is_stairs"):
            data["slope_pct"] = None
            data["slope_source"] = "unknown"

    # ---- entrances ----
    def derive_access(row):
        # access is driven by the "accessible path of travel" field (path_of_travel);
        # threshold_lt_half_in_and_bev is kept as its own finer-grained node attribute
        # (threshold_ok) rather than folded into this flag. Blank/unknown never
        # counts as accessible (plan §5.2).
        t = _s(row.get("type")).strip()
        ac = _s(row.get("access_control")).strip()
        if t == "Exit Only" or ac == "Sealed":
            return None  # dropped entirely
        pot = _s(row.get("path_of_travel")).strip()
        if pot == "Yes":
            return "accessible"
        if pot == "No":
            return "not_accessible"
        return "unknown"

    building_sindex = buildings_all.sindex
    entrance_nodes = []
    n_dropped_entrance = 0
    for _, row in entrances_c.iterrows():
        access = derive_access(row)
        if access is None:
            n_dropped_entrance += 1
            continue
        cand_idx = list(building_sindex.query(row.geometry.buffer(50.0), predicate="intersects"))
        building_name = ""
        if cand_idx:
            cands = buildings_all.iloc[cand_idx]
            nearest = cands.iloc[cands.geometry.distance(row.geometry).values.argmin()]
            building_name = _s(nearest.get("facil_name"))
        clear_width = _f(row.get("clear_open_width"))
        threshold_raw = _s(row.get("threshold_lt_half_in_and_bev")).strip()
        threshold_ok = {"Yes": True, "No": False}.get(threshold_raw, None)
        entrance_nodes.append({
            "geom": row.geometry,
            "door_id": _s(row.get("door_id")),
            "access": access,
            "auto_opener": _s(row.get("auto_opener")).strip().lower() == "yes",
            "threshold_ok": threshold_ok,
            "clear_width_in": clear_width,
            "access_control": map_access_control(row.get("access_control")),
            "building": building_name,
        })

    log(f"\nEntrances in zone: {len(entrances_c)}; dropped (exit-only/sealed): {n_dropped_entrance}; "
        f"kept: {len(entrance_nodes)}")
    n_accessible = sum(1 for e in entrance_nodes if e["access"] == "accessible")
    log(f"Accessible entrances: {n_accessible}")

    # Attach each entrance to the nearest point on the nearest path edge within
    # ENTRANCE_ATTACH_FT. If that nearest point falls in the interior of an edge
    # (not already a node), the edge is split there so the entrance is actually
    # spliced into the routable network rather than dangling off a new point
    # that no other edge touches.
    attachable = {}  # slot_id -> (u, v, key, geom)
    slot_counter = 0
    for u, v, key, data in G.edges(keys=True, data=True):
        if data.get("kind") == "door approach":
            continue
        attachable[slot_counter] = (u, v, key, data["geometry"])
        slot_counter += 1

    def rebuild_attach_index():
        ids = list(attachable.keys())
        geoms = [attachable[i][3] for i in ids]
        gdf = gpd.GeoDataFrame({"slot": ids}, geometry=geoms, crs=WORK_CRS)
        return gdf, (gdf.sindex if len(gdf) else None)

    attach_gdf, attach_sindex = rebuild_attach_index()

    n_attached = 0
    n_long_approach = 0
    n_unattached = 0
    n_edges_split_for_entrances = 0
    for e in entrance_nodes:
        node_id = nodes.new_id(e["geom"])
        G.add_node(node_id, x=e["geom"].x, y=e["geom"].y, node_type="entrance",
                   door_id=e["door_id"], access=e["access"], auto_opener=e["auto_opener"],
                   threshold_ok=e["threshold_ok"], clear_width_in=e["clear_width_in"],
                   access_control=e["access_control"], building=e["building"])
        if attach_sindex is None:
            n_unattached += 1
            continue
        buffered = e["geom"].buffer(ENTRANCE_ATTACH_FT)
        cand_idx = list(attach_sindex.query(buffered, predicate="intersects"))
        best_slot, best_dist = None, None
        for idx in cand_idx:
            slot = int(attach_gdf.iloc[idx]["slot"])
            if slot not in attachable:
                continue
            _, _, _, geom = attachable[slot]
            d = geom.distance(e["geom"])
            if best_dist is None or d < best_dist:
                best_dist, best_slot = d, slot
        if best_slot is None or best_dist is None or best_dist > ENTRANCE_ATTACH_FT:
            n_unattached += 1
            continue

        u, v, key, geom = attachable[best_slot]
        length_total = geom.length
        proj_d = geom.project(e["geom"])
        attach_pt = geom.interpolate(proj_d)

        if proj_d <= SNAP_COINCIDENT_FT:
            attach_node = u
        elif proj_d >= length_total - SNAP_COINCIDENT_FT:
            attach_node = v
        else:
            attach_node = nodes.new_id(attach_pt)
            G.add_node(attach_node, x=attach_pt.x, y=attach_pt.y, node_type="path")
            edge_data = G[u][v][key]
            sub1 = substring(geom, 0, proj_d)
            sub2 = substring(geom, proj_d, length_total)
            attrs1 = dict(edge_data)
            attrs1["geometry"] = sub1
            attrs1["length_ft"] = float(sub1.length)
            attrs1["edge_id"] = f"{edge_data['edge_id']}a"
            attrs2 = dict(edge_data)
            attrs2["geometry"] = sub2
            attrs2["length_ft"] = float(sub2.length)
            attrs2["edge_id"] = f"{edge_data['edge_id']}b"
            G.remove_edge(u, v, key)
            key1 = G.add_edge(u, attach_node, **attrs1)
            key2 = G.add_edge(attach_node, v, **attrs2)
            del attachable[best_slot]
            attachable[slot_counter] = (u, attach_node, key1, sub1)
            slot_counter += 1
            attachable[slot_counter] = (attach_node, v, key2, sub2)
            slot_counter += 1
            attach_gdf, attach_sindex = rebuild_attach_index()
            n_edges_split_for_entrances += 1

        approach_len = e["geom"].distance(attach_pt)
        flags = []
        if approach_len > LONG_APPROACH_FT:
            flags.append("long_approach")
            n_long_approach += 1
        attrs = {
            "kind": "door approach", "travel": "other", "is_stairs": False, "steps": None,
            "landings": None, "rail_side": None, "slope_pct": None, "slope_source": "unknown",
            "cross_slope_pct": None, "width_ft": None, "surface": "unknown", "defect_level": 0,
            "ramp": False, "handrail": False, "curb_cuts": None, "surveyed": False,
            "date_surveyed": "", "lit_fixtures": 0, "lit_per_100ft": 0.0, "lit_watts": 0.0,
            "inferred": False, "verified": False, "flags": flags,
        }
        add_edge(node_id, attach_node, LineString([e["geom"], attach_pt]), approach_len, attrs)
        n_attached += 1

    log(f"Entrances attached to the path network (<= {ENTRANCE_ATTACH_FT:g} ft): {n_attached}")
    log(f"Path edges split to splice in an entrance attachment point: {n_edges_split_for_entrances}")
    log(f"Entrance approaches flagged long (> {LONG_APPROACH_FT:g} ft): {n_long_approach}")
    log(f"Entrances left unattached (> {ENTRANCE_ATTACH_FT:g} ft from network): {n_unattached}")

    # ---- stairs safety pass #2: door-approach edges were created after the
    # first stair pass, so re-check every still-non-stair edge (same rule) now
    # that the graph is complete. This guarantees the safety invariant holds
    # for every edge kind, not just the ones that existed before entrances
    # were attached. ----
    late_retagged = 0
    if stair_sindex is not None:
        for u, v, data in G.edges(data=True):
            if data.get("is_stairs"):
                continue
            geom = data["geometry"]
            if geom.length == 0:
                continue
            buffered = geom.buffer(STAIR_MATCH_BUFFER_FT)
            candidate_idx = list(stair_sindex.query(buffered, predicate="intersects"))
            if not candidate_idx:
                continue
            candidates = stairs_c.iloc[candidate_idx]
            true_hits = candidates[candidates.geometry.intersects(geom)]
            if not len(true_hits):
                continue
            overlap_len = sum(h.intersection(geom).length for h in true_hits.geometry)
            if (overlap_len / geom.length) >= STAIR_RETAG_OVERLAP_FRAC:
                data["is_stairs"] = True
                data["travel"] = "stair"
                data["flags"] = list(data.get("flags", [])) + ["retagged_stair"]
                data["slope_pct"] = None
                data["slope_source"] = "unknown"
                late_retagged += 1
    retagged += late_retagged
    log(f"Additional segments re-tagged as stairs after entrance attachment (door approaches, etc.): {late_retagged}")
    log(f"Total non-stair segments re-tagged as stairs (true polygon overlap): {retagged}")

    # ---- lighting: fixtures within LIGHTING_RADIUS_FT of each edge ----
    if len(lighting_c):
        lighting_c = lighting_c.copy()
        lighting_c["nfix"] = pd.to_numeric(lighting_c.get("number_of_fixtures"), errors="coerce").fillna(1.0)
        lighting_c["watts"] = pd.to_numeric(lighting_c.get("l_wattage"), errors="coerce").fillna(0.0)
        light_sindex = lighting_c.sindex
        for u, v, data in G.edges(data=True):
            geom = data["geometry"]
            buffered = geom.buffer(LIGHTING_RADIUS_FT)
            idx = list(light_sindex.query(buffered, predicate="intersects"))
            if not idx:
                continue
            cands = lighting_c.iloc[idx]
            within = cands[cands.geometry.distance(geom) <= LIGHTING_RADIUS_FT]
            if len(within):
                fixtures = float(within["nfix"].sum())
                data["lit_fixtures"] = fixtures
                length_ft = data["length_ft"]
                data["lit_per_100ft"] = fixtures / (length_ft / 100.0) if length_ft > 0 else 0.0
                data["lit_watts"] = float(within["watts"].sum())

    # ---- largest connected component share by length ----
    comps = list(nx.connected_components(G))
    comp_len = []
    for comp in comps:
        total = sum(d.get("length_ft", 0.0) for u, v, d in G.subgraph(comp).edges(data=True))
        comp_len.append(total)
    total_len = sum(comp_len) or 1.0
    largest_share = max(comp_len) / total_len if comp_len else 0.0

    log(f"\n=== Validation summary ===")
    log(f"Graph type: {type(G).__name__} (undirected)")
    log(f"Nodes: {G.number_of_nodes()}  Edges: {G.number_of_edges()}")
    log(f"Connected components: {len(comps)}")
    log(f"Largest connected component: {largest_share * 100:.1f}% of total edge length")

    kinds = sorted({d.get("kind") for _, _, d in G.edges(data=True)})
    travels = sorted({d.get("travel") for _, _, d in G.edges(data=True)})
    slope_sources = sorted({d.get("slope_source") for _, _, d in G.edges(data=True)})
    accesses = sorted({d.get("access") for _, d in G.nodes(data=True) if d.get("node_type") == "entrance"})
    log(f"kind values: {kinds}")
    log(f"travel values: {travels}")
    log(f"slope_source values: {slope_sources}")
    log(f"entrance access values: {accesses}")

    n_over5 = sum(1 for _, _, d in G.edges(data=True) if d.get("slope_pct") is not None and d["slope_pct"] > 5.0)
    n_over833 = sum(1 for _, _, d in G.edges(data=True) if d.get("slope_pct") is not None and d["slope_pct"] > 8.33)
    log(f"Edges with slope_pct > 5%: {n_over5}")
    log(f"Edges with slope_pct > 8.33%: {n_over833}")
    log(f"Crosswalk segments in zone: {n_crosswalk_segments}")

    # ---- seed reports (Trip Hazards) ----
    seed_features = []
    if trip_hazards is not None:
        th_clip = clip(trip_hazards)
        for _, row in th_clip.iterrows():
            centroid = row.geometry.centroid
            centroid_wgs84 = gpd.GeoSeries([centroid], crs=WORK_CRS).to_crs(WGS84).iloc[0]
            seed_features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [centroid_wgs84.x, centroid_wgs84.y]},
                "properties": {
                    "type": "blocked_path",
                    "source": "seed",
                    "status": "confirmed",
                    "note": _s(row.get("Concern")),
                    "location_text": _s(row.get("Location")),
                    "historical": True,
                },
            })
    with open(OUT_DIR / "seed_reports.geojson", "w") as f:
        json.dump({"type": "FeatureCollection", "features": seed_features}, f)
    log(f"\nSeed reports (Trip Hazards in zone): {len(seed_features)}")

    # ---- write outputs ----
    edge_records = []
    for u, v, d in G.edges(data=True):
        rec = dict(d)
        geom = rec.pop("geometry")
        rec["flags"] = ";".join(rec.get("flags", []))
        rec["u"] = u
        rec["v"] = v
        rec["geometry"] = geom
        edge_records.append(rec)
    edges_gdf = gpd.GeoDataFrame(edge_records, geometry="geometry", crs=WORK_CRS).to_crs(WGS84)
    edges_gdf.to_file(OUT_DIR / "edges.geojson", driver="GeoJSON")

    node_records = []
    for n, d in G.nodes(data=True):
        rec = dict(d)
        x, y = rec.pop("x"), rec.pop("y")
        rec["node_id"] = n
        rec["geometry"] = Point(x, y)
        node_records.append(rec)
    nodes_gdf = gpd.GeoDataFrame(node_records, geometry="geometry", crs=WORK_CRS).to_crs(WGS84)
    nodes_gdf.to_file(OUT_DIR / "nodes.geojson", driver="GeoJSON")

    with open(OUT_DIR / "graph.pkl", "wb") as f:
        pickle.dump(G, f)

    with open(OUT_DIR / "build_report.txt", "w") as f:
        f.write("\n".join(report_lines) + "\n")

    log(f"\nWrote {OUT_DIR / 'edges.geojson'}, {OUT_DIR / 'nodes.geojson'}, "
        f"{OUT_DIR / 'graph.pkl'}, {OUT_DIR / 'seed_reports.geojson'}, {OUT_DIR / 'build_report.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
