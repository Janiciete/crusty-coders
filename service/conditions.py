"""Automatic condition detection (darkness, ice/snow).

Prompt 7: darkness is computed from sunrise/sunset for the demo-zone center
(astral, America/New_York); ice is computed from the NWS hourly forecast for
grid BGM 45,70 (CLAUDE.md §6: current hourly temperature <=34F or snow/ice/
sleet/freezing mentioned in the short forecast). User overrides ("on"/"off")
always win over "auto" and skip the corresponding data source entirely.

Network calls (NWS only) happen at runtime, never in tests: tests monkeypatch
`_fetch_hourly_forecast` instead of hitting the network.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from astral import LocationInfo
from astral.sun import sun
from dotenv import load_dotenv

load_dotenv()

# --- demo-zone fixed location (CLAUDE.md §6 bbox center / campus point) ---
CAMPUS_LAT = 42.4475
CAMPUS_LON = -76.4848
TZ_NAME = "America/New_York"

_LOCATION = LocationInfo(
    name="Ithaca",
    region="USA",
    timezone=TZ_NAME,
    latitude=CAMPUS_LAT,
    longitude=CAMPUS_LON,
)

# --- NWS hourly forecast (CLAUDE.md §2: grid BGM 45,70, User-Agent required) ---
NWS_HOURLY_URL = "https://api.weather.gov/gridpoints/BGM/45,70/forecast/hourly"
NWS_TIMEOUT_SECONDS = 5.0

ICE_TEMP_MAX_F = 34.0
ICE_KEYWORDS = ("snow", "sleet", "ice", "freezing")

_CACHE_TTL_SECONDS = 15 * 60
# Module-level cache so repeated /route calls within 15 minutes don't re-hit
# NWS. Keyed on nothing (single fixed grid point) -- just a 2-slot cache.
_CACHE: dict = {"periods": None, "fetched_at": None}


@dataclass
class Conditions:
    darkness: bool = False
    ice: bool = False
    source: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def _fetch_hourly_forecast() -> list[dict]:
    """Hit the real NWS hourly forecast endpoint. Raises on any failure.

    Never called from tests -- monkeypatch this function instead.
    """
    user_agent = os.environ.get("NWS_USER_AGENT") or "crusty-coders-hackathon"
    resp = httpx.get(
        NWS_HOURLY_URL,
        headers={"User-Agent": user_agent},
        timeout=NWS_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["properties"]["periods"]


def _get_forecast_periods() -> list[dict]:
    """Cached (15 min) wrapper around `_fetch_hourly_forecast`. Raises on
    failure (cache is not updated on failure, so the next call retries).
    """
    now_mono = time.monotonic()
    fetched_at = _CACHE.get("fetched_at")
    cached = _CACHE.get("periods")
    if (
        cached is not None
        and fetched_at is not None
        and (now_mono - fetched_at) < _CACHE_TTL_SECONDS
    ):
        return cached

    periods = _fetch_hourly_forecast()
    _CACHE["periods"] = periods
    _CACHE["fetched_at"] = now_mono
    return periods


def _period_ice_conditions(period: dict) -> tuple[bool, float | None]:
    """Whether a single NWS hourly period indicates ice, and its temp in F."""
    temp = period.get("temperature")
    unit = (period.get("temperatureUnit") or "F").upper()
    temp_f: float | None
    if temp is None:
        temp_f = None
    elif unit == "F":
        temp_f = float(temp)
    elif unit == "C":
        temp_f = float(temp) * 9.0 / 5.0 + 32.0
    else:
        temp_f = float(temp)  # unknown unit [VERIFY]: NWS has only used F/C

    short = (period.get("shortForecast") or "").lower()
    is_cold = temp_f is not None and temp_f <= ICE_TEMP_MAX_F
    mentions_ice = any(keyword in short for keyword in ICE_KEYWORDS)
    return (is_cold or mentions_ice), temp_f


def _sun_times(now: datetime) -> tuple[datetime, datetime]:
    """Sunrise/sunset (tz-aware, America/New_York) for `now`'s local date."""
    s = sun(_LOCATION.observer, date=now.date(), tzinfo=now.tzinfo)
    return s["sunrise"], s["sunset"]


def _resolve_now(now: datetime | None) -> datetime:
    tz = ZoneInfo(TZ_NAME)
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def get_conditions(overrides: dict, now: datetime | None = None) -> Conditions:
    """Resolve darkness/ice flags from live data, respecting overrides.

    overrides: e.g. {"darkness": "auto"|"on"|"off", "ice": "auto"|"on"|"off"}.
    now: optional tz-aware (or naive, assumed America/New_York) timestamp to
         evaluate darkness against; defaults to the real current time. Lets
         tests and callers pin "now" without mocking the clock.
    """
    overrides = overrides or {}
    now = _resolve_now(now)
    warnings: list[str] = []
    source: dict = {}

    # Sunrise/sunset are always computed (local, no network) so `source`
    # can report them regardless of whether darkness itself is overridden.
    sunrise, sunset = _sun_times(now)
    source["sunrise"] = sunrise.isoformat()
    source["sunset"] = sunset.isoformat()
    is_dark_now = now < sunrise or now >= sunset

    darkness_raw = overrides.get("darkness", "auto")
    if darkness_raw == "on":
        darkness = True
        source["darkness"] = "override"
    elif darkness_raw == "off":
        darkness = False
        source["darkness"] = "override"
    else:
        if darkness_raw != "auto":
            warnings.append(
                f"unrecognized darkness override {darkness_raw!r}; treated as auto"
            )
        darkness = is_dark_now
        source["darkness"] = "astral"

    # Temperature is only populated when we actually talk to NWS.
    source["temperature_f"] = None

    ice_raw = overrides.get("ice", "auto")
    if ice_raw == "on":
        ice = True
        source["ice"] = "override"
    elif ice_raw == "off":
        ice = False
        source["ice"] = "override"
    else:
        if ice_raw != "auto":
            warnings.append(
                f"unrecognized ice override {ice_raw!r}; treated as auto"
            )
        try:
            periods = _get_forecast_periods()
            current = periods[0] if periods else {}
            ice, temp_f = _period_ice_conditions(current)
            source["ice"] = "nws"
            source["temperature_f"] = temp_f
        except Exception:
            ice = False
            source["ice"] = "unavailable"
            warnings.append("Weather unavailable; ice detection off.")

    return Conditions(darkness=darkness, ice=ice, source=source, warnings=warnings)
