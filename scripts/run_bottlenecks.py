"""Runs the bottleneck finder (plan §3.2, §8.7; CLAUDE.md §6; Prompt 8) on
the real graph.pkl and writes cornell_data/graph/bottlenecks.json, which
`GET /bottlenecks` (service/app.py) serves as-is.

Usage: python scripts/run_bottlenecks.py
"""

from __future__ import annotations

import json
import os
import pickle
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from service.bottleneck import find_bottlenecks  # noqa: E402

N_PAIRS = 2000
SEED = 42
TOP_N = 10


def _graph_path() -> Path:
    env = os.getenv("GRAPH_PATH") or ""
    p = Path(env) if env else REPO_ROOT / "cornell_data" / "graph" / "graph.pkl"
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p


def _output_path() -> Path:
    return REPO_ROOT / "cornell_data" / "graph" / "bottlenecks.json"


def main() -> None:
    graph_path = _graph_path()
    print(f"Loading graph from {graph_path} ...")
    with open(graph_path, "rb") as f:
        G = pickle.load(f)
    print(f"Graph loaded: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges.")

    start = time.time()

    def on_progress(n: int) -> None:
        if n % 200 == 0:
            elapsed = time.time() - start
            print(f"  ... {n}/{N_PAIRS} pairs processed ({elapsed:.1f}s elapsed)")

    result = find_bottlenecks(
        G, n_pairs=N_PAIRS, seed=SEED, top_n=TOP_N, on_progress=on_progress
    )

    elapsed = time.time() - start
    print(f"Done in {elapsed:.1f}s.")

    out_path = _output_path()
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_path, "w") as f:
        json.dump(result, f, indent=2)
    os.replace(tmp_path, out_path)
    print(f"Wrote {out_path}")

    print()
    print(f"Pairs sampled: {result['pairs_sampled']}")
    print(f"Blocked trips: {result['blocked_trips']}")
    print()
    print("Top 5 segments blocking the most Wheelchair trips:")
    for seg in result["top_segments"][:5]:
        codes = ", ".join(v["code"] for v in seg["violations"])
        print(
            f"  {seg['edge_id']}: {seg['trips_blocked']} trips blocked "
            f"({codes}) -> {seg['pct_trips_unblocked_if_fixed']:.1f}% of all "
            f"sampled trips unblocked if fixed"
        )
    print()
    print(f"Top 5 fixed together: {result['top5_combined_pct']:.1f}% of all sampled trips unblocked")


if __name__ == "__main__":
    main()
