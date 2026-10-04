"""Live reports (Supabase-backed). Stub per CLAUDE.md §5 until Prompt 6.

`get_active_reports` will eventually read the `active_reports` Supabase view.
`edge_effects_for` will eventually spatially match reports to graph edges
(~20 m, per CLAUDE.md §6) and return "remove" for blocked_path/construction
or a penalty multiplier (float) for ice/too_steep/too_dark.

All three are no-ops here so the routing core (router.py) and the API
(app.py, explain.py) can be built and tested against their contracts
directly, without a live Supabase connection.
"""

from __future__ import annotations

# Set by get_active_reports() (once P6 implements it) if the Supabase read
# fails, so app.py can add the warning "Live reports unavailable; routing
# without them." instead of silently returning no reports. Stays None in
# this stub since get_active_reports() always "succeeds" (by returning []).
last_error: str | None = None


def get_active_reports() -> list:
    return []


def edge_effects_for(G, reports) -> dict:
    return {}


def report_edges(G, reports) -> dict[str, list]:
    """Map each report's id to the list of edge_keys it touches.

    Used by explain.py to compute `reports_avoided`: a report counts as
    avoided by route B (relative to route A) if its edge list intersects
    A's edges but not B's. Stub returns {} (same no-op contract as the
    other two functions) until P6 implements the ~20 m spatial match.
    """
    return {}
