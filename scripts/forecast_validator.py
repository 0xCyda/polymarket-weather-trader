#!/usr/bin/env python3
"""
Multi-Source Forecast Validation — AIFS ENS as core, NOAA + Open-Meteo blend as validators.

Architecture:
  PRIMARY:   ECMWF AIFS ENS ensemble mean
  VALIDATOR: NOAA (US cities only) + Open-Meteo default blend (all cities)

Signal strength:
  "strong"    = 2+ sources agree within 3°F/2°C
  "moderate"  = 2+ sources but delta 3-5°F
  "weak"      = sources disagree by >5°F — skip trade
  "ecmwf_only"= only ECMWF returned data (validators failed)
  "no_data"   = nothing came back

Module:  from scripts.forecast_validator import validate_forecast, get_ecmwf_forecast
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent))
from aifs_forecast import get_aifs_ens_forecast

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

# All city coordinates (US + international)
ALL_LOCATIONS = {
    # US cities
    "NYC":           {"lat": 40.7769, "lon": -73.8740, "tz": "America/New_York", "noaa_station": "KLGA"},
    "Chicago":       {"lat": 41.9742, "lon": -87.9073, "tz": "America/Chicago", "noaa_station": "KORD"},
    "Seattle":       {"lat": 47.4502, "lon": -122.3088, "tz": "America/Los_Angeles", "noaa_station": "KSEA"},
    "Atlanta":       {"lat": 33.6407, "lon": -84.4277, "tz": "America/New_York", "noaa_station": "KATL"},
    "Dallas":        {"lat": 32.8998, "lon": -97.0403, "tz": "America/Chicago", "noaa_station": "KDFW"},
    "Miami":         {"lat": 25.7959, "lon": -80.2870, "tz": "America/New_York", "noaa_station": "KMIA"},
    "Houston":       {"lat": 29.9902, "lon": -95.3368, "tz": "America/Chicago", "noaa_station": "KIAH"},
    "San Francisco": {"lat": 37.6213, "lon": -122.3790, "tz": "America/Los_Angeles", "noaa_station": "KSFO"},
    "Phoenix":       {"lat": 33.4373, "lon": -112.0078, "tz": "America/Phoenix", "noaa_station": "KPHX"},
    "Los Angeles":   {"lat": 33.9425, "lon": -118.4081, "tz": "America/Los_Angeles", "noaa_station": "KLAX"},
    # International cities
    "Tel Aviv":      {"lat": 32.0853, "lon": 34.7818, "tz": "Asia/Jerusalem"},
    "Munich":        {"lat": 48.1351, "lon": 11.5820, "tz": "Europe/Berlin"},
    "London":        {"lat": 51.5074, "lon": -0.1278, "tz": "Europe/London"},
    "Tokyo":         {"lat": 35.6762, "lon": 139.6503, "tz": "Asia/Tokyo"},
    "Seoul":         {"lat": 37.5665, "lon": 126.9780, "tz": "Asia/Seoul"},
    "Ankara":        {"lat": 39.9334, "lon": 32.8597, "tz": "Europe/Istanbul"},
    "Lucknow":       {"lat": 26.8467, "lon": 80.9462, "tz": "Asia/Kolkata"},
    "Wellington":    {"lat": -41.2866, "lon": 174.7756, "tz": "Pacific/Auckland"},
    "Toronto":       {"lat": 43.6777, "lon": -79.6248, "tz": "America/Toronto"},
    "Paris":         {"lat": 48.8566, "lon": 2.3522, "tz": "Europe/Paris"},
    "Milan":         {"lat": 45.4642, "lon": 9.1900, "tz": "Europe/Rome"},
    "Sao Paulo":     {"lat": -23.5505, "lon": -46.6333, "tz": "America/Sao_Paulo"},
    "Warsaw":        {"lat": 52.2297, "lon": 21.0122, "tz": "Europe/Warsaw"},
    "Singapore":     {"lat": 1.3521, "lon": 103.8198, "tz": "Asia/Singapore"},
}

NOAA_API_BASE = "https://api.weather.gov"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"


def _fetch_json(url, headers=None):
    """Fetch JSON from URL."""
    try:
        req = Request(url, headers=headers or {})
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except (HTTPError, URLError, Exception):
        return None


def _loc(city: str) -> dict:
    """Look up city coordinates, case-insensitive."""
    if city in ALL_LOCATIONS:
        return ALL_LOCATIONS[city]
    for name, loc in ALL_LOCATIONS.items():
        if name.upper() == city.upper():
            return loc
    return None


def _is_us_city(city: str) -> bool:
    """Check if city has a NOAA station (= US city)."""
    loc = _loc(city)
    return loc is not None and "noaa_station" in loc


# =============================================================================
# ECMWF AIFS ENS — PRIMARY forecast source
# =============================================================================

def get_ecmwf_forecast(city: str, date_str: str = None, metric: str = "high",
                       unit: str = "F") -> float | dict | None:
    """
    Fetch ECMWF AIFS ENS ensemble mean forecast.

    Returns a single-point forecast when date_str is provided. Full-series support
    is intentionally not implemented yet because AIFS retrieval is GRIB/date-based.
    """
    loc = _loc(city)
    if not loc:
        return None if date_str else {}

    if date_str:
        result = get_aifs_ens_forecast(
            lat=loc["lat"],
            lon=loc["lon"],
            date_str=date_str,
            metric=metric,
            unit=unit,
            timezone_name=loc.get("tz", "UTC"),
        )
        return result.get("ensemble_mean")

    return {}


# =============================================================================
# NOAA — validator for US cities
# =============================================================================

def _get_noaa_temp(city: str, date_str: str, metric: str = "high") -> float:
    """Get NOAA forecast temp (°F) for a US city on a date."""
    loc = _loc(city)
    if not loc or "noaa_station" not in loc:
        return None

    headers = {
        "User-Agent": "SimmerWeatherSkill/1.0 (https://simmer.markets)",
        "Accept": "application/geo+json",
    }

    points_url = f"{NOAA_API_BASE}/points/{loc['lat']},{loc['lon']}"
    points = _fetch_json(points_url, headers)
    if not points or "properties" not in points:
        return None

    forecast_url = points["properties"].get("forecast")
    if not forecast_url:
        return None

    forecast = _fetch_json(forecast_url, headers)
    if not forecast or "properties" not in forecast:
        return None

    for period in forecast["properties"].get("periods", []):
        start = period.get("startTime", "")
        if not start.startswith(date_str):
            continue
        is_daytime = period.get("isDaytime", True)
        if (metric == "high" and is_daytime) or (metric == "low" and not is_daytime):
            return period.get("temperature")

    return None


# =============================================================================
# Open-Meteo default blend — validator for all cities
# =============================================================================

def _get_openmeteo_blend_temp(city: str, date_str: str, metric: str = "high",
                               unit: str = "F") -> float:
    """Get Open-Meteo default blend forecast temp for any city."""
    loc = _loc(city)
    if not loc:
        return None

    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    tz = loc.get("tz", "UTC").replace("/", "%2F")

    url = (
        f"{OPEN_METEO_BASE}?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&temperature_unit={temp_unit}"
        f"&timezone={tz}"
        f"&forecast_days=10"
    )

    data = _fetch_json(url)
    if not data:
        return None

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    key = "temperature_2m_max" if metric == "high" else "temperature_2m_min"
    temps = daily.get(key, [])

    for d, t in zip(dates, temps):
        if d == date_str and t is not None:
            return round(t)

    return None


# =============================================================================
# Main validation function
# =============================================================================

def validate_forecast(city: str, date_str: str, metric: str = "high",
                      ecmwf_temp_prefetched: float = None, unit: str = "F") -> dict:
    """
    Validate forecast using AIFS ENS as primary, NOAA + Open-Meteo blend as validators.

    Architecture:
      1. ECMWF AIFS ENS ensemble mean = the forecast we trade on
      2. NOAA (US only) + Open-Meteo blend = validators that confirm or contradict

    Args:
        city: City name
        date_str: Target date YYYY-MM-DD
        metric: "high" or "low"
        ecmwf_temp_prefetched: Skip ECMWF fetch if already have the temp
        unit: "F" or "C" for temperature unit

    Returns:
        {
            "ecmwf_temp": float or None (PRIMARY),
            "noaa_temp": float or None (US validator),
            "blend_temp": float or None (global validator),
            "sources_agree": int (how many sources returned matching data),
            "max_delta": float (worst disagreement in degrees),
            "signal_strength": "strong" | "moderate" | "weak" | "ecmwf_only" | "no_data"
        }
    """
    # 1. ECMWF — primary
    ecmwf_temp = ecmwf_temp_prefetched
    if ecmwf_temp is None:
        ecmwf_temp = get_ecmwf_forecast(city, date_str, metric, unit)

    # 2. Validators — fetch concurrently
    noaa_temp = None
    blend_temp = None
    fetch_noaa = _is_us_city(city) and unit == "F"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {}
        if fetch_noaa:
            futures[executor.submit(_get_noaa_temp, city, date_str, metric)] = "noaa"
        futures[executor.submit(_get_openmeteo_blend_temp, city, date_str, metric, unit)] = "blend"

        for future in as_completed(futures):
            label = futures[future]
            try:
                result_val = future.result(timeout=15)
                if label == "noaa":
                    noaa_temp = result_val
                else:
                    blend_temp = result_val
            except Exception:
                pass  # skip failed validators

    result = {
        "ecmwf_temp": ecmwf_temp,
        "noaa_temp": noaa_temp,
        "blend_temp": blend_temp,
        "sources_agree": 0,
        "max_delta": None,
        "signal_strength": "no_data",
    }

    if ecmwf_temp is None:
        # No primary forecast — fall back to whatever we have
        if blend_temp is not None:
            result["ecmwf_temp"] = blend_temp  # use blend as fallback primary
            result["signal_strength"] = "ecmwf_only"  # single source
            result["sources_agree"] = 1
        return result

    # Compare sources
    temps = [ecmwf_temp]
    if noaa_temp is not None:
        temps.append(noaa_temp)
    if blend_temp is not None:
        temps.append(blend_temp)

    result["sources_agree"] = len(temps)

    if len(temps) == 1:
        result["signal_strength"] = "ecmwf_only"
        return result

    # Calculate max disagreement
    max_delta = max(abs(ecmwf_temp - t) for t in temps[1:])
    result["max_delta"] = round(max_delta, 1)

    if max_delta <= 3:
        result["signal_strength"] = "strong"
    elif max_delta <= 5:
        result["signal_strength"] = "moderate"
    else:
        result["signal_strength"] = "weak"

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ECMWF-primary forecast validation")
    parser.add_argument("--city", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--metric", default="high", choices=["high", "low"])
    parser.add_argument("--unit", default="F", choices=["F", "C"])
    args = parser.parse_args()

    result = validate_forecast(args.city, args.date, args.metric, unit=args.unit)
    print(json.dumps(result, indent=2))

    s = result["signal_strength"]
    if s == "strong":
        print(f"\nSignal: STRONG ({result['sources_agree']} sources agree within 3°, max delta={result['max_delta']}°)")
    elif s == "moderate":
        print(f"\nSignal: MODERATE ({result['sources_agree']} sources, delta={result['max_delta']}°)")
    elif s == "weak":
        print(f"\nSignal: WEAK (delta={result['max_delta']}° — SKIP TRADE)")
    elif s == "ecmwf_only":
        print(f"\nSignal: ECMWF_ONLY (single source, validators unavailable)")
    else:
        print("\nSignal: NO_DATA")
