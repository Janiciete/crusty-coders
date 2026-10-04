"""Contract tests for cornell_data/graph/graph.pkl (CLAUDE.md §5, §8.1).

All tests are skipped if graph.pkl hasn't been built yet (it is a gitignored
pipeline output, not checked in). Run pipeline/fetch_cornell_data.py then
pipeline/build_graph.py --bbox ... to produce it.
"""

import pickle
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_PATH = REPO_ROOT / "cornell_data" / "graph" / "graph.pkl"
STAIRS_PATH = REPO_ROOT / "cornell_data" / "stairs.geojson"

sys.path.insert(0, str(REPO_ROOT / "pipeline"))

pytestmark = pytest.mark.skipif(
    not GRAPH_PATH.exists(),
    reason="graph.pkl not found; run pipeline/fetch_cornell_data.py and pipeline/build_graph.py first",
)

EDGE_ATTRS = [
    "length_ft", "kind", "travel", "is_stairs", "steps", "landings", "rail_side",
    "slope_pct", "slope_source", "cross_slope_pct", "width_ft", "surface",
    "defect_level", "ramp", "handrail", "curb_cuts", "surveyed", "date_surveyed",
    "lit_fixtures", "lit_per_100ft", "lit_watts", "inferred", "verified", "flags",
]
ALLOWED_SLOPE_SOURCE = {"cornell_survey", "usgs_lidar_1m_estimate", "unknown"}
ALLOWED_ACCESS = {"accessible", "not_accessible", "unknown"}
ALLOWED_DEFECT_LEVEL = {0, 1, 2, 3}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture(scope="module")
def G():
    with open(GRAPH_PATH, "rb") as f:
        return pickle.load(f)


@pytest.fixture(scope="module")
def edges(G):
    return list(G.edges(data=True))


@pytest.fixture(scope="module")
def entrance_nodes(G):
    return [(n, d) for n, d in G.nodes(data=True) if d.get("node_type") == "entrance"]


def test_graph_loads_with_pickle_and_networkx(G):
    import networkx as nx
    assert isinstance(G, (nx.Graph, nx.MultiGraph, nx.DiGraph, nx.MultiDiGraph))
    assert G.number_of_edges() > 0
    assert G.number_of_nodes() > 0


def test_every_edge_has_all_section_8_1_attributes(edges):
    assert edges, "graph has no edges"
    for u, v, data in edges:
        missing = [attr for attr in EDGE_ATTRS if attr not in data]
        assert not missing, f"edge {u}-{v} missing attributes: {missing}"


def test_slope_source_allowed_values(edges):
    for u, v, data in edges:
        assert data["slope_source"] in ALLOWED_SLOPE_SOURCE, (
            f"edge {u}-{v} has slope_source={data['slope_source']!r}"
        )


def test_entrance_access_allowed_values(entrance_nodes):
    assert entrance_nodes, "graph has no entrance nodes"
    for n, data in entrance_nodes:
        assert data["access"] in ALLOWED_ACCESS, f"node {n} has access={data['access']!r}"


def test_defect_level_allowed_values(edges):
    for u, v, data in edges:
        assert data["defect_level"] in ALLOWED_DEFECT_LEVEL, (
            f"edge {u}-{v} has defect_level={data['defect_level']!r}"
        )


def test_curb_cuts_on_crosswalk_edges(edges):
    crosswalk_edges = [(u, v, d) for u, v, d in edges if d.get("kind") == "crosswalk"]
    assert crosswalk_edges, "no crosswalk edges found"
    for u, v, data in crosswalk_edges:
        assert data["curb_cuts"] in (0, 1, 2), (
            f"crosswalk edge {u}-{v} has curb_cuts={data['curb_cuts']!r}"
        )


def test_date_surveyed_format(edges):
    for u, v, data in edges:
        d = data["date_surveyed"]
        assert d == "" or DATE_RE.match(d), f"edge {u}-{v} has date_surveyed={d!r}"


def test_largest_component_at_least_90_percent_of_length(G):
    import networkx as nx
    components = list(nx.connected_components(G))
    assert components, "graph has no connected components"
    lengths = []
    for comp in components:
        total = sum(d.get("length_ft", 0.0) for _, _, d in G.subgraph(comp).edges(data=True))
        lengths.append(total)
    total_len = sum(lengths)
    assert total_len > 0
    largest_share = max(lengths) / total_len
    assert largest_share >= 0.90, f"largest component is only {largest_share:.1%} of total edge length"


def test_safety_no_unlabeled_stairs_on_stair_footprints(G):
    """Using the same stair-footprint overlap rule build_graph.py uses
    (>= STAIR_RETAG_OVERLAP_FRAC of a segment's length truly intersecting a
    stair polygon, no buffer), no edge on a stair footprint may have
    is_stairs == False. This is the safety-critical check: a Wheelchair
    route must never be routed onto an unlabeled staircase.
    """
    if not STAIRS_PATH.exists():
        pytest.skip("cornell_data/stairs.geojson not found; cannot re-check stair footprints")

    import geopandas as gpd
    from build_graph import WORK_CRS, STAIR_RETAG_OVERLAP_FRAC

    stairs = gpd.read_file(STAIRS_PATH).to_crs(WORK_CRS)
    stairs = stairs[stairs.geometry.notna()]
    stair_sindex = stairs.sindex

    violations = []
    for u, v, data in G.edges(data=True):
        if data.get("is_stairs"):
            continue
        geom = data.get("geometry")
        if geom is None or geom.length == 0:
            continue
        cand_idx = list(stair_sindex.query(geom, predicate="intersects"))
        if not cand_idx:
            continue
        cands = stairs.iloc[cand_idx]
        hits = cands[cands.geometry.intersects(geom)]
        if not len(hits):
            continue
        overlap_len = sum(h.intersection(geom).length for h in hits.geometry)
        frac = overlap_len / geom.length
        if frac >= STAIR_RETAG_OVERLAP_FRAC:
            violations.append((u, v, data.get("edge_id"), frac))

    assert not violations, (
        f"{len(violations)} edge(s) lie on a stair polygon but is_stairs=False: {violations[:10]}"
    )


# ---- P2: elevation fill (--elevation, USGS EPQS 1 m lidar) ----
#
# These tests only make assertions about edges that P2 was allowed to touch
# (see build_graph.is_elevation_candidate / run_elevation_fill). They are
# skipped (not failed) if the graph was built without --elevation, since in
# that case no edge should ever have slope_source == "usgs_lidar_1m_estimate"
# and there is nothing P2-specific to check.

MIN_SEG_FT = 20.0
OUTLIER_SLOPE_CAP_PCT = 60.0


@pytest.fixture(scope="module")
def estimated_edges(edges):
    return [(u, v, d) for u, v, d in edges if d.get("slope_source") == "usgs_lidar_1m_estimate"]


def test_elevation_fill_present_or_skip(estimated_edges):
    if not estimated_edges:
        pytest.skip("graph.pkl was built without --elevation (no usgs_lidar_1m_estimate edges)")


def test_no_cornell_survey_slope_changed_by_elevation_fill(edges):
    """P2 must never overwrite a surveyed slope: every edge that still carries
    slope_source == "cornell_survey" must not also carry the "estimated_slope"
    flag (which run_elevation_fill only ever adds alongside
    slope_source == "usgs_lidar_1m_estimate")."""
    for u, v, d in edges:
        if d.get("slope_source") == "cornell_survey":
            flags = d.get("flags", "")
            flag_list = flags.split(";") if isinstance(flags, str) else flags
            assert "estimated_slope" not in flag_list, (
                f"edge {u}-{v} is cornell_survey but was touched by the elevation fill"
            )


def test_every_filled_edge_is_long_enough_and_non_stair(estimated_edges):
    if not estimated_edges:
        pytest.skip("no usgs_lidar_1m_estimate edges in this graph")
    for u, v, d in estimated_edges:
        assert d.get("is_stairs") is False, f"edge {u}-{v} is a stair but has an estimated slope"
        assert d.get("length_ft", 0.0) >= MIN_SEG_FT or "suspect_slope" in (
            d.get("flags", "").split(";") if isinstance(d.get("flags"), str) else d.get("flags", [])
        ), (
            f"edge {u}-{v} has an estimated slope but is only {d.get('length_ft')} ft "
            f"(< {MIN_SEG_FT} ft) and was not a suspect_slope re-measurement"
        )


def test_no_estimated_slope_above_outlier_cap(estimated_edges):
    """Outlier guard (plan §5.2's 51% outlier): a lidar-derived slope estimate
    above OUTLIER_SLOPE_CAP_PCT indicates a bad 1 m DEM read (building/tree
    noise), not real terrain, and run_elevation_fill must fall back to
    "unknown" rather than publish it."""
    for u, v, d in estimated_edges:
        assert d.get("slope_pct") is not None, f"edge {u}-{v} is an estimate with no slope_pct"
        assert d["slope_pct"] <= OUTLIER_SLOPE_CAP_PCT, (
            f"edge {u}-{v} has an estimated slope of {d['slope_pct']}%, above the "
            f"{OUTLIER_SLOPE_CAP_PCT}% outlier cap"
        )


def test_short_unsurveyed_non_suspect_edges_stay_unknown(edges):
    """Edges under MIN_SEG_FT that were never flagged suspect should never be
    filled by the elevation step: they're too short to measure reliably."""
    for u, v, d in edges:
        if (
            d.get("slope_source") == "unknown"
            and d.get("length_ft", 0.0) < MIN_SEG_FT
            and not d.get("is_stairs")
        ):
            flags = d.get("flags", "")
            flag_list = flags.split(";") if isinstance(flags, str) else flags
            assert "estimated_slope" not in flag_list
