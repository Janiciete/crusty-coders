"""Idempotent loader for Cornell's 15 undated trip-hazard entries (plan
§10.5), built by pipeline/build_graph.py into
cornell_data/graph/seed_reports.geojson.

Uses the **service** key only (never the browser-facing anon key): this is
the one script in the repo allowed to use SUPABASE_SERVICE_ROLE_KEY
(CLAUDE.md §2/§7). Never prints a key.

Every feature in seed_reports.geojson is loaded as a seed-only
"trip_hazard" report (migration 0005), overriding the pipeline's
provisional per-row `type`/`note` properties (set before the trip_hazard
type existed): type="trip_hazard", source="seed", note="Historical Cornell
trip hazard (undated)". Idempotent: existing source="seed" rows are deleted
before inserting, so re-running always leaves exactly 15 rows.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from postgrest import ReturnMethod

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_NOTE = "Historical Cornell trip hazard (undated)"


def _seed_reports_path() -> Path:
    graph_path = Path(os.getenv("GRAPH_PATH") or "cornell_data/graph/graph.pkl")
    if not graph_path.is_absolute():
        graph_path = REPO_ROOT / graph_path
    return graph_path.parent / "seed_reports.geojson"


def _point_wkt(lon: float, lat: float) -> str:
    return f"POINT({lon} {lat})"


def _load_features(path: Path) -> list[dict]:
    with open(path) as f:
        collection = json.load(f)
    return collection.get("features", [])


def _build_payloads(features: list[dict]) -> list[dict]:
    payloads = []
    for feature in features:
        lon, lat = feature["geometry"]["coordinates"]
        payloads.append(
            {
                "type": "trip_hazard",
                "source": "seed",
                "status": "confirmed",
                "location": _point_wkt(lon, lat),
                "note": SEED_NOTE,
            }
        )
    return payloads


def main() -> int:
    url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not service_key:
        print("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env", file=sys.stderr)
        return 1

    seed_path = _seed_reports_path()
    if not seed_path.exists():
        print(f"seed reports file not found: {seed_path}", file=sys.stderr)
        print("run pipeline/build_graph.py first", file=sys.stderr)
        return 1

    features = _load_features(seed_path)
    payloads = _build_payloads(features)

    from supabase import create_client

    service = create_client(url, service_key)

    # Idempotent: clear any existing seed rows first (returning minimal --
    # we don't need the deleted rows back, and report_confirmations has no
    # SELECT policy, so the convention here is to always request minimal
    # for non-anon-readable writes too).
    service.table("reports").delete(returning=ReturnMethod.minimal).eq("source", "seed").execute()

    if not payloads:
        print("0 seed reports loaded (seed_reports.geojson had no features)")
        return 0

    result = service.table("reports").insert(payloads).execute()
    rows = result.data or []

    bad_rows = [r for r in rows if r.get("source") != "seed" or r.get("status") != "confirmed"]
    if bad_rows:
        print(
            f"ERROR: {len(bad_rows)} of {len(rows)} inserted rows did not come back as "
            "source='seed', status='confirmed'. This means the reports_before_insert "
            "trigger (supabase/migrations/0003_triggers.sql) did not recognize this "
            "insert as coming from the service role -- see the [VERIFY] note on "
            "auth.role() there. Proposed fix (not applied here, per P6 constraints): "
            "treat a null/blank auth.role() as service_role in that trigger, e.g. "
            "`v_role is distinct from 'service_role'` -> "
            "`coalesce(v_role, 'service_role') is distinct from 'service_role'`.",
            file=sys.stderr,
        )
        return 1

    print(f"{len(rows)} seed reports loaded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
