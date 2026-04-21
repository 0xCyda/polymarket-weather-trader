#!/usr/bin/env python3
"""
Multi-Model Ensemble Forecast — Degen Doppler style.

Fetches Open-Meteo models plus AIFS ENS and returns a weighted ensemble forecast.

Models & weights (normalized to 1.0):
  aifs_ens          0.20  (ECMWF AIFS ensemble mean)
  ecmwf_ifs025      0.28  (Open-Meteo ECMWF deterministic)
  gfs_seamless      0.16  (NOAA GFS, good global coverage)
  icon_global       0.12  (DWD ICON, strong in Europe)
  gem_global        0.08  (Canadian GEM)
  jma_seamless      0.08  (JMA, strong in Asia-Pacific)
  bom_access_global 0.08  (BOM ACCESS, strong in Southern Hemisphere)

Signal strength:
  "strong"        = >=4 models, agreement_pct>=70%, max_delta<=6°
  "moderate"      = >=3 models, max_delta<=10°
  "weak"          = max_delta>10° or <3 models
  "single_source" = only 1 model returned data

METAR downgrades (D+0 only) are applied only after 14:00 local, since morning
obs are typically 10-15°F below the daily high and would spuriously downgrade.

Usage:
  from scripts.ensemble_forecast import get_ensemble_forecast
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
    "NYC":           {"lat": 40.7769, "lon": -73.8740, "tz": "America/New_York"},
    "Chicago":       {"lat": 41.9742, "lon": -87.9073, "tz": "America/Chicago"},
    "Seattle":       {"lat": 47.4502, "lon": -122.3088, "tz": "America/Los_Angeles"},
    "Atlanta":       {"lat": 33.6407, "lon": -84.4277, "tz": "America/New_York"},
    "Dallas":        {"lat": 32.8998, "lon": -97.0403, "tz": "America/Chicago"},
    "Miami":         {"lat": 25.7959, "lon": -80.2870, "tz": "America/New_York"},
    "Houston":       {"lat": 29.9902, "lon": -95.3368, "tz": "America/Chicago"},
    "San Francisco": {"lat": 37.6213, "lon": -122.3790, "tz": "America/Los_Angeles"},
    "Phoenix":       {"lat": 33.4373, "lon": -112.0078, "tz": "America/Phoenix"},
    "Los Angeles":   {"lat": 33.9425, "lon": -118.4081, "tz": "America/Los_Angeles"},
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
    "Singapore":     {"lat": 1.3521,  "lon": 103.8198,  "tz": "Asia/Singapore"},
    "Shanghai":      {"lat": 31.2304, "lon": 121.4737,  "tz": "Asia/Shanghai"},
    "Denver":        {"lat": 39.8561, "lon": -104.6737, "tz": "America/Denver"},
    "Austin":        {"lat": 30.1975, "lon": -97.6664, "tz": "America/Chicago"},
    "Las Vegas":     {"lat": 36.0840, "lon": -115.1537, "tz": "America/Los_Angeles"},
    # Chinese cities
    "Beijing":       {"lat": 39.9042, "lon": 116.4074, "tz": "Asia/Shanghai"},
    "Shenzhen":      {"lat": 22.5431, "lon": 114.0579, "tz": "Asia/Shanghai"},
    "Chengdu":       {"lat": 30.5728, "lon": 104.0668, "tz": "Asia/Shanghai"},
    "Chongqing":     {"lat": 29.4316, "lon": 106.9123, "tz": "Asia/Shanghai"},
    "Wuhan":         {"lat": 30.5928, "lon": 114.3055, "tz": "Asia/Shanghai"},
    "Hong Kong":     {"lat": 22.3193, "lon": 114.1694, "tz": "Asia/Hong_Kong"},
    "Buenos Aires":  {"lat": -34.6037, "lon": -58.3816, "tz": "America/Argentina/Buenos_Aires"},
}

# METAR station IDs per city (ICAO codes — these are the airport stations
# that Polymarket weather markets resolve against)
METAR_STATIONS = {
    "NYC":           "KLGA",   # LaGuardia
    "Chicago":       "KORD",   # O'Hare
    "Seattle":       "KSEA",   # Sea-Tac
    "Atlanta":       "KATL",   # Hartsfield
    "Dallas":        "KDFW",   # DFW
    "Miami":         "KMIA",   # Miami Intl
    "Houston":       "KIAH",   # IAH
    "San Francisco": "KSFO",   # SFO
    "Phoenix":       "KPHX",   # PHX
    "Los Angeles":   "KLAX",   # LAX
    "Denver":        "KDEN",   # Denver Intl
    "Austin":        "KAUS",   # Austin-Bergstrom
    "Las Vegas":     "KLAS",   # McCarran
    "Tokyo":         "RJTT",   # Haneda
    "Seoul":         "RKSS",   # Gimpo
    "Munich":        "EDDM",   # Munich Intl
    "Warsaw":        "EPWA",   # Warsaw Chopin
    "London":        "EGLL",   # Heathrow
    "Paris":         "LFPG",   # CDG
    "Ankara":        "LTAC",   # Esenboga
    "Toronto":       "CYYZ",   # Pearson
    "Wellington":    "NZWN",   # Wellington Intl
    "Sao Paulo":     "SBGR",   # Guarulhos
    "Shanghai":      "ZSPD",   # Pudong
    "Tel Aviv":      "LLBG",   # Ben Gurion
    "Singapore":     "WSSS",   # Changi
    "Hong Kong":     "VHHH",   # Hong Kong Intl
    "Buenos Aires":  "SAEZ",   # Ezeiza Intl
    "Beijing":       "ZBAA",   # Capital Intl
    "Chengdu":       "ZUUU",   # Shuangliu
    "Chongqing":     "ZUCK",   # Jiangbei
    "Lucknow":       "VILK",   # Chaudhary Charan Singh
    "Milan":         "LIMC",   # Malpensa
    "Shenzhen":      "ZGSZ",   # Bao'an
    "Wuhan":         "ZHHH",   # Tianhe
}

METAR_API_BASE = "https://aviationweather.gov/api/data/metar"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

# Model definitions: name -> base weight (normalized to sum to 1.0)
ENSEMBLE_MODELS = {
    "aifs_ens":          0.20,
    "ecmwf_ifs025":      0.28,
    "gfs_seamless":      0.16,
    "icon_global":       0.12,
    "gem_global":        0.08,
    "jma_seamless":      0.08,
    "bom_access_global": 0.08,
}


def _fetch_aifs_temp(city: str, date_str: str, metric: str, unit: str) -> float:
    """Fetch the AIFS ENS ensemble mean for the target city/date."""
    loc = _loc(city)
    if not loc:
        return None

    result = get_aifs_ens_forecast(
        lat=loc["lat"],
        lon=loc["lon"],
        date_str=date_str,
        metric=metric,
        unit=unit,
        timezone_name=loc.get("tz", "UTC"),
    )
    return result.get("ensemble_mean")


def _fetch_json(url, headers=None):
    """Fetch JSON from URL."""
    try:
        req = Request(url, headers=headers or {})
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except (HTTPError, URLError, Exception):
        return None


def get_metar_temp(city: str, unit: str = "C") -> float:
    """
    Fetch the latest real-time surface temperature from METAR for a city's airport station.

    Returns current observed temperature in the requested unit, or None if unavailable.
    Only meaningful for D+0 (today) — used as ground truth anchor.
    """
    icao = METAR_STATIONS.get(city)
    if not icao:
        return None

    url = f"{METAR_API_BASE}?ids={icao}&format=json&hours=2"
    data = _fetch_json(url, headers={"User-Agent": "SimmerWeatherBot/1.0"})
    if not data or not isinstance(data, list) or len(data) == 0:
        return None

    temp_c = data[0].get("temp")
    if temp_c is None:
        return None

    if unit == "F":
        return round(temp_c * 9 / 5 + 32, 1)
    return round(float(temp_c), 1)


def _loc(city: str) -> dict:
    """Look up city coordinates, case-insensitive."""
    if city in ALL_LOCATIONS:
        return ALL_LOCATIONS[city]
    for name, loc in ALL_LOCATIONS.items():
        if name.upper() == city.upper():
            return loc
    return None


def _fetch_model_temp(city: str, date_str: str, metric: str, unit: str,
                      model_name: str) -> float:
    """Fetch a single model's forecast temp for a city/date."""
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
        f"&models={model_name}"
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
            return round(t, 1)

    return None


def get_ensemble_forecast(city: str, date_str: str, metric: str = "high",
                          unit: str = "F") -> dict:
    """
    Fetch multi-model ensemble forecast for a city on a date.

    For same-day (D+0) markets, also fetches live METAR station observations
    as a real-time ground truth anchor. METAR divergence > 5° downgrades signal.

    Args:
        city: City name (must be in ALL_LOCATIONS)
        date_str: Target date YYYY-MM-DD
        metric: "high" or "low"
        unit: "F" or "C"

    Returns:
        {
            "weighted_temp": float,      # weighted average of models that returned data
            "model_temps": dict,         # {model_name: temp}
            "models_count": int,         # how many returned data
            "max_delta": float,          # worst disagreement between any two models
            "agreement_pct": float,      # % of models within 3° of weighted avg
            "signal_strength": str,      # "strong" | "moderate" | "weak" | "single_source"
            "metar_temp": float|None,    # live station observation (D+0 only)
            "metar_delta": float|None,   # abs difference between ensemble and METAR
        }
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    model_temps = {}
    metar_temp = None
    metar_delta = None

    # Check if target date is today (D+0) in the city's local timezone
    loc = _loc(city)
    city_tz_name = loc.get("tz", "UTC") if loc else "UTC"
    try:
        city_tz = ZoneInfo(city_tz_name)
    except Exception:
        city_tz = timezone.utc
    today_str = datetime.now(city_tz).strftime("%Y-%m-%d")
    is_today = (date_str == today_str)

    # Fetch all models + METAR (if D+0) concurrently
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {}
        for model_name in ENSEMBLE_MODELS:
            if model_name == "aifs_ens":
                future = executor.submit(_fetch_aifs_temp, city, date_str, metric, unit)
            else:
                future = executor.submit(
                    _fetch_model_temp,
                    city,
                    date_str,
                    metric,
                    unit,
                    model_name,
                )
            futures[future] = model_name
        metar_future = None
        if is_today and metric == "high":
            # METAR gives current temp — only relevant for high on D+0
            metar_future = executor.submit(get_metar_temp, city, unit)

        for future in as_completed(futures):
            model_name = futures[future]
            try:
                temp = future.result(timeout=15)
                if temp is not None:
                    model_temps[model_name] = temp
            except Exception:
                pass

        if metar_future is not None:
            try:
                metar_temp = metar_future.result(timeout=15)
            except Exception:
                metar_temp = None

    models_count = len(model_temps)

    # No data at all
    if models_count == 0:
        return {
            "weighted_temp": None,
            "model_temps": {},
            "models_count": 0,
            "max_delta": None,
            "agreement_pct": 0.0,
            "signal_strength": "no_data",
            "metar_temp": metar_temp,
            "metar_delta": None,
        }

    # Single source
    if models_count == 1:
        only_temp = list(model_temps.values())[0]
        if metar_temp is not None:
            metar_delta = round(abs(only_temp - metar_temp), 1)
        return {
            "weighted_temp": float(only_temp),
            "model_temps": model_temps,
            "models_count": 1,
            "max_delta": 0.0,
            "agreement_pct": 100.0,
            "signal_strength": "single_source",
            "metar_temp": metar_temp,
            "metar_delta": metar_delta,
        }

    # Compute weighted average — renormalize weights for models that returned data
    total_weight = sum(ENSEMBLE_MODELS[m] for m in model_temps)
    weighted_temp = sum(
        model_temps[m] * (ENSEMBLE_MODELS[m] / total_weight)
        for m in model_temps
    )
    weighted_temp = round(weighted_temp, 1)

    # Max delta: worst disagreement between any two models
    all_temps = list(model_temps.values())
    max_delta = round(max(all_temps) - min(all_temps), 1)

    # Agreement: % of models within 3° of weighted average
    within_3 = sum(1 for t in all_temps if abs(t - weighted_temp) <= 3)
    agreement_pct = round(100.0 * within_3 / models_count, 1)

    # METAR ground truth cross-check (D+0 only)
    local_hour = datetime.now(city_tz).hour
    metar_adjusted = False
    if metar_temp is not None:
        # After 15:00 local, METAR is a hard lower bound on the daily high.
        # Peak heating is typically 14:00-16:00; by 15:00+ the current temp
        # is at or very near the day's max.
        if is_today and metric == "high" and local_hour >= 15 and metar_temp > weighted_temp:
            weighted_temp = round(metar_temp, 1)
            metar_adjusted = True
        metar_delta = round(abs(weighted_temp - metar_temp), 1)

    # Signal strength classification.
    # METAR downgrade: after 14:00 local, divergence from ensemble → lower confidence.
    # METAR upgrade:   after 14:00 local, agreement with ensemble → higher confidence.
    metar_check_active = is_today and metric == "high" and local_hour >= 14
    if models_count >= 4 and agreement_pct >= 70.0 and max_delta <= 6:
        signal_strength = "strong"
        if metar_check_active and metar_delta is not None and metar_delta > 5:
            signal_strength = "moderate"
    elif models_count >= 3 and max_delta <= 10:
        signal_strength = "moderate"
        if metar_check_active and metar_delta is not None and metar_delta > 8:
            signal_strength = "weak"
        elif metar_check_active and metar_delta is not None and metar_delta <= 3:
            signal_strength = "strong"  # upgrade: afternoon METAR confirms ensemble
    else:
        signal_strength = "weak"

    return {
        "weighted_temp": weighted_temp,
        "model_temps": model_temps,
        "models_count": models_count,
        "max_delta": max_delta,
        "agreement_pct": agreement_pct,
        "signal_strength": signal_strength,
        "metar_temp": metar_temp,
        "metar_delta": metar_delta,
        "metar_adjusted": metar_adjusted,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Multi-model ensemble forecast")
    parser.add_argument("--city", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--metric", default="high", choices=["high", "low"])
    parser.add_argument("--unit", default="F", choices=["F", "C"])
    args = parser.parse_args()

    result = get_ensemble_forecast(args.city, args.date, args.metric, args.unit)
    print(json.dumps(result, indent=2))

    s = result["signal_strength"]
    wt = result["weighted_temp"]
    mc = result["models_count"]
    md = result["max_delta"]
    ap = result["agreement_pct"]

    if s == "strong":
        print(f"\nSignal: STRONG — {mc} models, {ap:.0f}% agree, delta={md}°, ensemble={wt}°")
    elif s == "moderate":
        print(f"\nSignal: MODERATE — {mc} models, {ap:.0f}% agree, delta={md}°, ensemble={wt}°")
    elif s == "weak":
        print(f"\nSignal: WEAK — {mc} models, delta={md}°, ensemble={wt}° — CAUTION")
    elif s == "single_source":
        print(f"\nSignal: SINGLE SOURCE — only 1 model returned data, ensemble={wt}°")
    else:
        print("\nSignal: NO DATA")
