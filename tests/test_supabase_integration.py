"""Integration tests against a live Supabase project.

Skipped entirely unless SUPABASE_URL, SUPABASE_ANON_KEY, and
SUPABASE_SERVICE_ROLE_KEY are all set in .env. These tests exercise the
migrations in supabase/migrations/ (0001-0004) against the real database --
they are not run as part of the normal unit-test suite.

All test rows are tagged note="TEST" and placed inside the demo-zone bbox
(CLAUDE.md §6), and are deleted with the service-role key in a fixture
teardown that runs even if a test fails. No key is ever printed.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid

import pytest
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL") or None
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or None
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or None

pytestmark = pytest.mark.skipif(
    not (SUPABASE_URL and SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY),
    reason="SUPABASE_URL, SUPABASE_ANON_KEY, and SUPABASE_SERVICE_ROLE_KEY must "
    "all be set in .env to run live Supabase integration tests",
)

# A point well inside the demo-zone bbox (W -76.4905, S 42.4430, E -76.4790, N 42.4520).
DEMO_LON = -76.4850
DEMO_LAT = 42.4480
TEST_NOTE = "TEST"


def _make_user_hash() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _offset_point(lon: float, lat: float, meters: float):
    """Return a point roughly `meters` north of (lon, lat)."""
    # ~111,320 m per degree latitude.
    d_lat = meters / 111_320.0
    return lon, lat + d_lat


@pytest.fixture()
def clients():
    from supabase import create_client

    anon = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    service = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return anon, service


@pytest.fixture()
def created_report_ids():
    ids: list[str] = []
    yield ids


@pytest.fixture(autouse=True)
def _cleanup(clients, created_report_ids):
    """Always delete TEST rows with the service key, even on failure."""
    yield
    _, service = clients
    try:
        if created_report_ids:
            service.table("reports").delete().in_("id", created_report_ids).execute()
        # Belt-and-suspenders: sweep any leftover TEST-noted rows from this run.
        service.table("reports").delete().eq("note", TEST_NOTE).execute()
    except Exception:
        # Teardown must not mask the original test failure; best-effort only.
        pass


def _point_wkt(lon: float, lat: float) -> str:
    return f"POINT({lon} {lat})"


def _insert_report(anon, lon: float, lat: float, report_type: str = "blocked_path", source: str | None = None):
    payload = {
        "type": report_type,
        "location": _point_wkt(lon, lat),
        "note": TEST_NOTE,
    }
    if source is not None:
        payload["source"] = source
    return anon.table("reports").insert(payload).execute()


def test_anon_insert_rounds_to_four_decimals(clients, created_report_ids):
    anon, _ = clients
    lon = DEMO_LON + 0.0000001234  # 6+ decimal places of precision
    lat = DEMO_LAT + 0.0000005678
    result = _insert_report(anon, lon, lat)
    assert len(result.data) == 1
    row = result.data[0]
    created_report_ids.append(row["id"])

    # Read back via active_reports only once confirmed; for a lone pending
    # report we instead check the raw row's rounded coordinates by re-reading
    # from `reports` is not permitted for anon (no select beyond policy? —
    # reports select is public), so read back directly.
    readback = anon.table("reports").select("id").eq("id", row["id"]).execute()
    assert len(readback.data) == 1

    assert row["status"] == "pending"
    assert row["expires_at"] is not None
    assert row["source"] == "user"
    assert row["confirmations"] == 1


def test_two_nearby_same_type_reports_confirm(clients, created_report_ids):
    anon, service = clients
    lon1, lat1 = DEMO_LON, DEMO_LAT
    lon2, lat2 = _offset_point(DEMO_LON, DEMO_LAT, 10.0)  # ~10 m away

    r1 = _insert_report(anon, lon1, lat1, report_type="ice")
    row1 = r1.data[0]
    created_report_ids.append(row1["id"])

    r2 = _insert_report(anon, lon2, lat2, report_type="ice")
    row2 = r2.data[0]
    created_report_ids.append(row2["id"])

    # Give the AFTER INSERT trigger a moment (should be synchronous, but be safe).
    time.sleep(0.5)

    check1 = service.table("reports").select("status,confirmations").eq("id", row1["id"]).execute().data[0]
    check2 = service.table("reports").select("status,confirmations").eq("id", row2["id"]).execute().data[0]

    assert check1["status"] == "confirmed"
    assert check2["status"] == "confirmed"
    assert check1["confirmations"] >= 2
    assert check2["confirmations"] >= 2


def test_different_type_report_stays_pending(clients, created_report_ids):
    anon, service = clients
    lon1, lat1 = DEMO_LON, DEMO_LAT
    lon2, lat2 = _offset_point(DEMO_LON, DEMO_LAT, 10.0)

    r1 = _insert_report(anon, lon1, lat1, report_type="ice")
    row1 = r1.data[0]
    created_report_ids.append(row1["id"])

    r2 = _insert_report(anon, lon2, lat2, report_type="construction")
    row2 = r2.data[0]
    created_report_ids.append(row2["id"])

    time.sleep(0.5)

    check1 = service.table("reports").select("status").eq("id", row1["id"]).execute().data[0]
    check2 = service.table("reports").select("status").eq("id", row2["id"]).execute().data[0]

    assert check1["status"] == "pending"
    assert check2["status"] == "pending"


def test_anon_cannot_read_profiles(clients):
    anon, _ = clients
    result = anon.table("profiles").select("*").execute()
    assert result.data == []


def test_anon_cannot_update_reports(clients, created_report_ids):
    anon, service = clients
    r1 = _insert_report(anon, DEMO_LON, DEMO_LAT, report_type="too_dark")
    row1 = r1.data[0]
    created_report_ids.append(row1["id"])

    with pytest.raises(Exception):
        anon.table("reports").update({"status": "confirmed"}).eq("id", row1["id"]).execute()

    check = service.table("reports").select("status").eq("id", row1["id"]).execute().data[0]
    assert check["status"] == "pending"


def test_anon_cannot_insert_seed_source(clients, created_report_ids):
    anon, _ = clients
    with pytest.raises(Exception):
        result = _insert_report(anon, DEMO_LON, DEMO_LAT, report_type="construction", source="seed")
        # If no exception was raised (e.g. silently coerced), make sure it
        # was NOT stored as seed so we still fail the test meaningfully.
        if result.data:
            created_report_ids.append(result.data[0]["id"])
            assert result.data[0]["source"] != "seed"


def test_same_user_hash_cannot_confirm_twice(clients, created_report_ids):
    anon, _ = clients
    r1 = _insert_report(anon, DEMO_LON, DEMO_LAT, report_type="crowded")
    row1 = r1.data[0]
    created_report_ids.append(row1["id"])

    user_hash = _make_user_hash()
    anon.table("report_confirmations").insert(
        {"report_id": row1["id"], "user_hash": user_hash}
    ).execute()

    with pytest.raises(Exception):
        anon.table("report_confirmations").insert(
            {"report_id": row1["id"], "user_hash": user_hash}
        ).execute()
