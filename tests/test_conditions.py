"""Tests for service/conditions.py (Prompt 7: real darkness + ice).

Darkness is checked with fixed `now` values before/after sunset (no network
involved -- astral is a pure local computation). Ice is checked with a
monkeypatched `_fetch_hourly_forecast` (never the real network, per
CLAUDE.md's "no real network in tests" rule). Every test resets the module's
15-minute forecast cache so mocked responses don't leak between tests.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from service import conditions
from service.conditions import Conditions, get_conditions


@pytest.fixture(autouse=True)
def _reset_nws_cache():
    conditions._CACHE["periods"] = None
    conditions._CACHE["fetched_at"] = None
    yield
    conditions._CACHE["periods"] = None
    conditions._CACHE["fetched_at"] = None


# Ithaca, 2026-10-04 (computed with astral for CAMPUS_LAT/CAMPUS_LON,
# America/New_York): sunrise ~07:06, sunset ~18:41.
DAYTIME = datetime(2026, 10, 4, 12, 0, 0)  # well after sunrise, before sunset
EVENING_AFTER_SUNSET = datetime(2026, 10, 4, 20, 0, 0)  # after ~18:41 sunset
EARLY_MORNING_BEFORE_SUNRISE = datetime(2026, 10, 4, 5, 0, 0)  # before ~07:06 sunrise


def _clear_warm_period(**overrides) -> dict:
    period = {
        "temperature": 60,
        "temperatureUnit": "F",
        "shortForecast": "Sunny",
    }
    period.update(overrides)
    return period


# ---------------------------------------------------------------------------
# Darkness (astral), auto mode -- no network involved
# ---------------------------------------------------------------------------


def test_darkness_auto_daytime_is_light():
    c = get_conditions({"ice": "off"}, now=DAYTIME)
    assert c.darkness is False
    assert c.source["darkness"] == "astral"
    assert "sunrise" in c.source and "sunset" in c.source


def test_darkness_auto_after_sunset_is_dark():
    c = get_conditions({"ice": "off"}, now=EVENING_AFTER_SUNSET)
    assert c.darkness is True
    assert c.source["darkness"] == "astral"


def test_darkness_auto_before_sunrise_is_dark():
    c = get_conditions({"ice": "off"}, now=EARLY_MORNING_BEFORE_SUNRISE)
    assert c.darkness is True
    assert c.source["darkness"] == "astral"


def test_darkness_source_reports_sunrise_and_sunset_even_when_overridden():
    c = get_conditions({"darkness": "on", "ice": "off"}, now=DAYTIME)
    assert c.darkness is True
    assert c.source["darkness"] == "override"
    # sunrise/sunset are still reported for the UI even though darkness
    # itself came from the override.
    assert c.source["sunrise"].startswith("2026-10-04")
    assert c.source["sunset"].startswith("2026-10-04")


# ---------------------------------------------------------------------------
# Ice (NWS), auto mode -- mocked forecast responses, no network
# ---------------------------------------------------------------------------


def test_ice_auto_cold_temperature_triggers_ice(monkeypatch):
    cold_period = _clear_warm_period(temperature=20, shortForecast="Clear")
    monkeypatch.setattr(conditions, "_fetch_hourly_forecast", lambda: [cold_period])

    c = get_conditions({"darkness": "off"}, now=DAYTIME)
    assert c.ice is True
    assert c.source["ice"] == "nws"
    assert c.source["temperature_f"] == 20


def test_ice_auto_warm_with_snow_in_forecast_triggers_ice(monkeypatch):
    snow_period = _clear_warm_period(temperature=50, shortForecast="Snow Showers")
    monkeypatch.setattr(conditions, "_fetch_hourly_forecast", lambda: [snow_period])

    c = get_conditions({"darkness": "off"}, now=DAYTIME)
    assert c.ice is True
    assert c.source["ice"] == "nws"
    assert c.source["temperature_f"] == 50


def test_ice_auto_warm_and_clear_is_no_ice(monkeypatch):
    warm_period = _clear_warm_period(temperature=60, shortForecast="Sunny")
    monkeypatch.setattr(conditions, "_fetch_hourly_forecast", lambda: [warm_period])

    c = get_conditions({"darkness": "off"}, now=DAYTIME)
    assert c.ice is False
    assert c.source["ice"] == "nws"
    assert c.source["temperature_f"] == 60


def test_ice_auto_nws_timeout_fails_safe(monkeypatch):
    def _raise_timeout():
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(conditions, "_fetch_hourly_forecast", _raise_timeout)

    c = get_conditions({"darkness": "off"}, now=DAYTIME)
    assert c.ice is False
    assert c.source["ice"] == "unavailable"
    assert "Weather unavailable; ice detection off." in c.warnings


def test_ice_auto_nws_generic_failure_fails_safe(monkeypatch):
    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(conditions, "_fetch_hourly_forecast", _raise)

    c = get_conditions({"darkness": "off"}, now=DAYTIME)
    assert c.ice is False
    assert c.source["ice"] == "unavailable"
    assert "Weather unavailable; ice detection off." in c.warnings


def test_ice_mentions_sleet_and_freezing_also_trigger(monkeypatch):
    for phrase in ("Sleet", "Freezing Rain", "Ice Storm"):
        period = _clear_warm_period(temperature=50, shortForecast=phrase)
        monkeypatch.setattr(conditions, "_fetch_hourly_forecast", lambda p=period: [p])
        conditions._CACHE["periods"] = None
        conditions._CACHE["fetched_at"] = None

        c = get_conditions({"darkness": "off"}, now=DAYTIME)
        assert c.ice is True, f"expected ice for forecast {phrase!r}"


# ---------------------------------------------------------------------------
# Override precedence ("on"/"off" always win over "auto", and over live data)
# ---------------------------------------------------------------------------


def test_override_on_wins_over_warm_clear_nws_data(monkeypatch):
    warm_period = _clear_warm_period(temperature=70, shortForecast="Sunny")
    monkeypatch.setattr(conditions, "_fetch_hourly_forecast", lambda: [warm_period])

    c = get_conditions({"ice": "on"}, now=DAYTIME)
    assert c.ice is True
    assert c.source["ice"] == "override"
    # No NWS call was needed/made for an explicit override.
    assert c.source["temperature_f"] is None


def test_override_off_wins_over_cold_nws_data(monkeypatch):
    cold_period = _clear_warm_period(temperature=10, shortForecast="Clear")
    monkeypatch.setattr(conditions, "_fetch_hourly_forecast", lambda: [cold_period])

    c = get_conditions({"ice": "off"}, now=DAYTIME)
    assert c.ice is False
    assert c.source["ice"] == "override"


def test_override_on_wins_over_daytime_darkness():
    c = get_conditions({"darkness": "on", "ice": "off"}, now=DAYTIME)
    assert c.darkness is True
    assert c.source["darkness"] == "override"


def test_override_off_wins_over_nighttime_darkness():
    c = get_conditions({"darkness": "off", "ice": "off"}, now=EVENING_AFTER_SUNSET)
    assert c.darkness is False
    assert c.source["darkness"] == "override"


def test_unrecognized_override_treated_as_auto_with_warning():
    c = get_conditions({"darkness": "sideways", "ice": "off"}, now=DAYTIME)
    assert c.source["darkness"] == "astral"
    assert c.darkness is False  # DAYTIME is light
    assert any("unrecognized darkness override" in w for w in c.warnings)


def test_missing_overrides_default_to_auto(monkeypatch):
    warm_period = _clear_warm_period(temperature=60, shortForecast="Sunny")
    monkeypatch.setattr(conditions, "_fetch_hourly_forecast", lambda: [warm_period])

    c = get_conditions({}, now=DAYTIME)
    assert c.darkness is False
    assert c.ice is False
    assert c.source["darkness"] == "astral"
    assert c.source["ice"] == "nws"


# ---------------------------------------------------------------------------
# Caching: a second call within 15 minutes should not re-fetch.
# ---------------------------------------------------------------------------


def test_forecast_is_cached_within_ttl(monkeypatch):
    calls = {"count": 0}

    def _fake_fetch():
        calls["count"] += 1
        return [_clear_warm_period(temperature=60, shortForecast="Sunny")]

    monkeypatch.setattr(conditions, "_fetch_hourly_forecast", _fake_fetch)

    get_conditions({"darkness": "off"}, now=DAYTIME)
    get_conditions({"darkness": "off"}, now=DAYTIME)

    assert calls["count"] == 1


# ---------------------------------------------------------------------------
# Conditions dataclass shape stays P3-compatible (cost.py depends on this).
# ---------------------------------------------------------------------------


def test_conditions_default_shape_unchanged():
    c = Conditions()
    assert c.darkness is False
    assert c.ice is False
    assert c.source == {}
    assert c.warnings == []
