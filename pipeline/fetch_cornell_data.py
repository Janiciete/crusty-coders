"""Download Cornell Facilities GIS layers used by build_graph.py.

Read-only GET/query requests only (CLAUDE.md §2, §7). No API key required.
Pages results 2,000 records at a time via resultOffset and writes one
GeoJSON file per layer into cornell_data/.

Usage:
    python pipeline/fetch_cornell_data.py
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://gis.fcs.cornell.edu/arcgis/rest/services/Production"
PAGE_SIZE = 2000
OUT_DIR = Path(__file__).resolve().parent.parent / "cornell_data"

# name -> (service path, output filename, plan §4.1 expected count)
LAYERS = {
    "walk_inventory": ("WalkInventory_2026/FeatureServer/0", "walk_inventory.geojson", 5689),
    "crosswalks": ("Path_of_Travel_2022/FeatureServer/1", "crosswalks.geojson", 288),
    "entrances": ("EntranceInventory_2025/FeatureServer/0", "entrances.geojson", 2978),
    "stairs": ("Stairs_Polygon/FeatureServer/0", "stairs.geojson", 779),
    "lighting": ("Campus_Lighting/FeatureServer/17", "lighting.geojson", 6679),
    "buildings": ("Buildings/FeatureServer/0", "buildings.geojson", 1220),
    "trip_hazards": ("Trip_Hazards/FeatureServer/0", "trip_hazards.geojson", 15),
}


def _get_json(url: str, retries: int = 3):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "crusty-coders-pipeline/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def fetch_layer(service_path: str) -> dict:
    """Fetch every record of an ArcGIS FeatureServer layer as a single GeoJSON FeatureCollection."""
    features = []
    offset = 0
    while True:
        url = (
            f"{BASE_URL}/{service_path}/query"
            f"?where=1%3D1&outFields=*&f=geojson"
            f"&resultOffset={offset}&resultRecordCount={PAGE_SIZE}"
        )
        data = _get_json(url)
        if "error" in data:
            raise RuntimeError(f"ArcGIS error for {service_path}: {data['error']}")
        page_features = data.get("features", [])
        features.extend(page_features)
        if len(page_features) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return {"type": "FeatureCollection", "features": features}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Fetching Cornell GIS layers into {OUT_DIR}/ ...")
    print(f"{'layer':16s} {'fetched':>8s} {'plan §4.1':>10s}")
    ok = True
    for name, (service_path, filename, expected) in LAYERS.items():
        try:
            fc = fetch_layer(service_path)
        except RuntimeError as exc:
            print(f"  ERROR fetching {name}: {exc}", file=sys.stderr)
            ok = False
            continue
        out_path = OUT_DIR / filename
        with open(out_path, "w") as f:
            json.dump(fc, f)
        count = len(fc["features"])
        flag = "" if count == expected else "  <-- differs from plan"
        print(f"{name:16s} {count:8d} {expected:10d}{flag}")
    if not ok:
        print("One or more layers failed to download.", file=sys.stderr)
        return 1
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
