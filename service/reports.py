"""Live reports (Supabase-backed). Stub per CLAUDE.md §5 until Prompt 6.

`get_active_reports` will eventually read the `active_reports` Supabase view.
`edge_effects_for` will eventually spatially match reports to graph edges
(~20 m, per CLAUDE.md §6) and return "remove" for blocked_path/construction
or a penalty multiplier (float) for ice/too_steep/too_dark.

Both are no-ops here so the routing core (router.py) can be built and tested
against the `edge_effects` parameter directly, without a live Supabase
connection.
"""

from __future__ import annotations


def get_active_reports() -> list:
    return []


def edge_effects_for(G, reports) -> dict:
    return {}
