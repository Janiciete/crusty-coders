"""Hand-built fixture graphs for the routing core (CLAUDE.md: unit tests use
a tiny fixture graph, not graph.pkl).

See the module docstring in service/router.py for which attribute names are
graph-specific ([VERIFY after P1]).

ASCII map of `main_graph` (lengths in ft; all edges undirected):

    O --50(flat)-- C ==stairs(30,is_stairs)== E_access
    |
    100(2%,surv)  A --100(2%,surv)-- B --50(2%,surv)-- E_access
    (O-A-B-E_access is the step-free detour; O-C-E_access is the stair
     shortcut -- both reach the SAME accessible entrance)

    O --40(flat)-- E_unknown            (shorter, but access="unknown")

    O --60(flat)-- X ==crosswalk,1 curb cut,20ft== Y --60(flat)-- E_access2
    (only path to E_access2; blocks Wheelchair, penalizes Low Vision)

    O --10(flat)-- R1 --100(flat)-- R2 --20(flat)-- E_access5
                   R1 --70(flat)-- R3 --70(flat)-- R2
    (two parallel paths R1->R2: direct 100ft vs detour via R3 140ft; used
     to force a >20% longer detour when the direct edge is "removed")

Separate fixtures:
  - `stairs_only_graph`: a single node pair connected only by a stairs edge,
    for the fallback-rule test.
  - `main_multigraph`: `main_graph`'s O-A-B-E_access / O-C-E_access subgraph
    rebuilt as a networkx.MultiGraph, to prove router.py works for both
    Graph and MultiGraph.

Dark/steep/ice edges used by the cost.py unit tests are built inline in
test_routing.py as plain attrs dicts (CLAUDE.md: nothing in cost.py reads
a graph, so there is no need to route through one for those checks).
"""

from __future__ import annotations

import sys
from pathlib import Path

import networkx as nx
import pytest

# Make `service` (and `tests`, for the `from tests.conftest import make_edge`
# re-export some tests use) importable when pytest is invoked as
# `pytest tests/test_routing.py` from the repo root, without relying on
# `python -m pytest` or a repo-root conftest.py/pyproject.toml that other
# in-flight prompts might also be touching.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def make_edge(**overrides) -> dict:
    """A fully-populated, "boring" sidewalk edge (CLAUDE.md §5 attributes),
    with overrides layered on top.
    """
    attrs = {
        "length_ft": 50.0,
        "kind": "sidewalk",
        "travel": "preferred",
        "is_stairs": False,
        "steps": 0,
        "landings": 0,
        "rail_side": None,
        "slope_pct": 0.0,
        "slope_source": "cornell_survey",
        "cross_slope_pct": 0.0,
        "width_ft": 5.0,
        "surface": "concrete",
        "defect_level": 0,
        "ramp": False,
        "handrail": True,
        "curb_cuts": None,
        "surveyed": True,
        "date_surveyed": "2025-01-01",
        "lit_fixtures": 2,
        "lit_per_100ft": 10.0,
        "lit_watts": 100.0,
        "inferred": False,
        "verified": True,
        "flags": [],
    }
    attrs.update(overrides)
    return attrs


@pytest.fixture
def main_graph() -> nx.Graph:
    G = nx.Graph()

    G.add_node("O")
    G.add_node("A")
    G.add_node("B")
    G.add_node("C")
    G.add_node("E_access", access="accessible", door_id="D1", building="Goldwin Smith")
    G.add_node("E_unknown", access="unknown", door_id="D2", building="Goldwin Smith")
    G.add_node("X")
    G.add_node("Y")
    G.add_node("E_access2", access="accessible", door_id="D3", building="Annex")
    G.add_node("R1")
    G.add_node("R2")
    G.add_node("R3")
    G.add_node("E_access5", access="accessible", door_id="D4", building="Annex2")

    # Step-free detour: O-A-B-E_access (total 250 ft, gentle 2% surveyed slope)
    G.add_edge("O", "A", **make_edge(length_ft=100.0, slope_pct=2.0))
    G.add_edge("A", "B", **make_edge(length_ft=100.0, slope_pct=2.0))
    G.add_edge("B", "E_access", **make_edge(length_ft=50.0, slope_pct=2.0))

    # Stair shortcut: O-C-E_access (total 80 ft, much shorter)
    G.add_edge("O", "C", **make_edge(length_ft=50.0, slope_pct=0.0))
    G.add_edge(
        "C",
        "E_access",
        **make_edge(
            length_ft=30.0,
            kind="sidewalk",
            travel="stair",
            is_stairs=True,
            steps=10,
            landings=1,
            slope_pct=None,
            slope_source=None,
        ),
    )

    # Shorter path to an entrance with unknown accessibility.
    G.add_edge("O", "E_unknown", **make_edge(length_ft=40.0, kind="door approach"))

    # Crosswalk with only 1 curb cut; only path to E_access2.
    G.add_edge("O", "X", **make_edge(length_ft=60.0))
    G.add_edge(
        "X",
        "Y",
        **make_edge(length_ft=20.0, kind="crosswalk", curb_cuts=1),
    )
    G.add_edge("Y", "E_access2", **make_edge(length_ft=60.0))

    # Removable direct edge vs. a 40%-longer detour, for the report-effect test.
    G.add_edge("O", "R1", **make_edge(length_ft=10.0))
    G.add_edge("R1", "R2", **make_edge(length_ft=100.0))
    G.add_edge("R1", "R3", **make_edge(length_ft=70.0))
    G.add_edge("R3", "R2", **make_edge(length_ft=70.0))
    G.add_edge("R2", "E_access5", **make_edge(length_ft=20.0))

    return G


@pytest.fixture
def stairs_only_graph() -> nx.Graph:
    """A trip where stairs are the only way to the destination."""
    G = nx.Graph()
    G.add_node("O2")
    G.add_node("E_access6", access="accessible", door_id="D5", building="OnlyStairsHall")
    G.add_edge(
        "O2",
        "E_access6",
        **make_edge(
            length_ft=40.0,
            travel="stair",
            is_stairs=True,
            steps=12,
            slope_pct=None,
            slope_source=None,
        ),
    )
    return G


@pytest.fixture
def main_multigraph() -> nx.MultiGraph:
    """Same step-free-vs-stairs scenario as main_graph, as a MultiGraph with
    an extra parallel edge between O and C, to prove router.py handles
    parallel edges correctly (picks the cheaper of the two).
    """
    G = nx.MultiGraph()
    G.add_node("O")
    G.add_node("A")
    G.add_node("B")
    G.add_node("C")
    G.add_node("E_access", access="accessible", door_id="D1", building="Goldwin Smith")

    G.add_edge("O", "A", **make_edge(length_ft=100.0, slope_pct=2.0))
    G.add_edge("A", "B", **make_edge(length_ft=100.0, slope_pct=2.0))
    G.add_edge("B", "E_access", **make_edge(length_ft=50.0, slope_pct=2.0))

    G.add_edge("O", "C", **make_edge(length_ft=50.0, slope_pct=0.0))
    # A parallel, more expensive edge between O and C (rough surface).
    G.add_edge("O", "C", **make_edge(length_ft=50.0, slope_pct=0.0, surface="gravel", defect_level=2))
    G.add_edge(
        "C",
        "E_access",
        **make_edge(
            length_ft=30.0,
            travel="stair",
            is_stairs=True,
            steps=10,
            slope_pct=None,
            slope_source=None,
        ),
    )

    return G
