#!/usr/bin/env python3
"""
LATE mode: day-of intraday weather trader.

At ~3pm local (or any hour configured), pulls TWC/Wunderground intraday
observations for a set of whitelisted cities, finds the Polymarket bucket
containing the running daily max/min, and buys it if:

  * the observed running value is >= `late_edge_buffer_c` from both bucket edges
    (i.e. "locked in" rather than borderline)
  * the current Simmer price is <= `late_price_ceiling`

Unlike CORE mode, signal comes from actual observations, not model forecasts.
Unlike PUNT, it's directional on the most-likely winner and uses realistic
entry prices around $0.70-$0.85.

Run:
  python3 scripts/late_trader.py              # dry run, auto-detect cities in window
  python3 scripts/late_trader.py --live       # execute real trades
  python3 scripts/late_trader.py --city London --force  # process one city now

Cron (hourly):
  0 * * * *  python3 /path/to/late_trader.py --live

The hourly cron processes whichever cities are currently in their 3pm-local
window; one invocation typically hits 1-3 cities.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from weather_trader import (
    CONFIG_SCHEMA, get_client, simmer_call, fetch_weather_markets, parse_market_bucket,
    parse_weather_event, execute_trade, log_error,
)
from paper_journal import (
    _HISTORICAL_LOCATIONS as LOCATIONS,
    _TWC_STATION_CODES as STATIONS,
    log_paper_trade,
)

# Use the same config loading path as weather_trader so config.json, env, and
# CLI overrides all resolve identically to the CORE/PUNT pipeline.
from simmer_sdk.skill import load_config
_cfg = load_config(CONFIG_SCHEMA, str(_HERE / "weather_trader.py"), slug="polymarket-weather-trader")

LATE_MODE            = bool(_cfg.get("late_mode", True))
LATE_PRICE_CEILING   = float(_cfg.get("late_price_ceiling", 0.90))
LATE_MAX_POSITION    = float(_cfg.get("late_max_position_usd", 100.0))
LATE_DAILY_BUDGET    = float(_cfg.get("late_daily_budget_usd", 500.0))
LATE_ENTRY_HOUR      = int(_cfg.get("late_entry_hour", 15))
LATE_EDGE_BUFFER_C   = float(_cfg.get("late_edge_buffer_c", 0.3))
LATE_ALLOWED_CITIES  = [c.strip() for c in str(_cfg.get("late_cities", "")).split(",") if c.strip()]

TWC_API_KEY = os.environ.get("TWC_API_KEY", "6532d6454b8aa370768e63d6ba5a832e")

_BUDGET_FILE = _HERE / "data" / "late_daily_budget.json"


# ----- TWC intraday fetch -----

def _fetch_twc_intraday(station: str, date_str: str) -> list[dict]:
    """Fetch hourly obs for a station on a date (YYYY-MM-DD)."""
    compact = date_str.replace("-", "")
    url = (
        f"https://api.weather.com/v1/location/{station}/observations/historical.json"
        f"?apiKey={TWC_API_KEY}&units=m&startDate={compact}&endDate={compact}"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            log_error("twc_http", f"status={r.status_code}", station=station, date=date_str)
            return []
        return r.json().get("observations", []) or []
    except Exception as e:
        log_error("twc_exc", str(e), station=station, date=date_str)
        return []


def _running_extreme(obs: list[dict], local_tz: ZoneInfo, up_to_hour: int, metric: str) -> float | None:
    """Return max (for metric='high') or min (for metric='low') of obs in
    local-time hours 0..up_to_hour inclusive. Units: °C."""
    temps = []
    for o in obs:
        vt = o.get("valid_time_gmt")
        if vt is None:
            continue
        try:
            local_hour = datetime.fromtimestamp(int(vt), tz=timezone.utc).astimezone(local_tz).hour
        except Exception:
            continue
        if local_hour > up_to_hour:
            continue
        t = o.get("temp")
        if t is None:
            continue
        temps.append(float(t))
    if not temps:
        return None
    return max(temps) if metric == "high" else min(temps)


# ----- Bucket math -----

def _bucket_contains(temp_c: float, bucket: tuple) -> bool:
    lo, hi, unit = bucket
    temp = temp_c if unit == "C" else temp_c * 9 / 5 + 32
    if lo == -999:
        return temp < hi + 0.5
    if hi == 999:
        return temp >= lo - 0.5
    return lo - 0.5 <= temp < hi + 0.5


def _edge_distance_c(temp_c: float, bucket: tuple) -> float:
    """Min distance from temp_c to either bucket edge, in °C."""
    lo, hi, unit = bucket
    if unit == "F":
        lo_c = (lo - 32) * 5 / 9 if lo != -999 else -999
        hi_c = (hi - 32) * 5 / 9 if hi != 999 else 999
    else:
        lo_c, hi_c = lo, hi
    if lo == -999:
        return (hi_c + (0.5 / 1.8 if unit == "F" else 0.5)) - temp_c
    if hi == 999:
        return temp_c - (lo_c - (0.5 / 1.8 if unit == "F" else 0.5))
    edge = 0.5 / 1.8 if unit == "F" else 0.5
    return min(temp_c - (lo_c - edge), (hi_c + edge) - temp_c)


def _bucket_label(bucket: tuple) -> str:
    lo, hi, unit = bucket
    if lo == -999:
        return f"{hi}°{unit} or below"
    if hi == 999:
        return f"{lo}°{unit} or above"
    if lo == hi:
        return f"{lo}°{unit}"
    return f"{lo}-{hi}°{unit}"


# ----- Budget tracking -----

def _load_budget() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        b = json.loads(_BUDGET_FILE.read_text())
        if b.get("date") != today:
            return {"date": today, "spent": 0.0}
        return b
    except Exception:
        return {"date": today, "spent": 0.0}


def _save_budget(b: dict) -> None:
    _BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BUDGET_FILE.write_text(json.dumps(b))


# ----- Core scan -----

def _cities_in_window(force: bool, specific: str | None) -> list[str]:
    """Return allowed cities whose local hour currently equals LATE_ENTRY_HOUR.
    With force=True, return all allowed. With specific, return just that one."""
    if specific:
        return [specific] if specific in LOCATIONS else []
    now_utc = datetime.now(timezone.utc)
    out = []
    for city in LATE_ALLOWED_CITIES:
        loc = LOCATIONS.get(city)
        if not loc:
            continue
        tz = ZoneInfo(loc[2])
        local = now_utc.astimezone(tz)
        if force or local.hour == LATE_ENTRY_HOUR:
            out.append(city)
    return out


def _scan_city(city: str, dry_run: bool, log=print) -> dict:
    result = {"city": city, "status": "skip", "reason": None, "price": None, "bucket": None}
    loc = LOCATIONS.get(city)
    station = STATIONS.get(city)
    if not loc or not station:
        result["reason"] = "no_station_or_tz"
        return result

    tz = ZoneInfo(loc[2])
    local_now = datetime.now(timezone.utc).astimezone(tz)
    date_str = local_now.date().isoformat()
    cur_hour = local_now.hour

    obs = _fetch_twc_intraday(station, date_str)
    if not obs:
        result["reason"] = "twc_empty"
        return result

    # Today's markets for this city
    markets = fetch_weather_markets()
    candidates = []
    for m in markets:
        info = parse_weather_event(m.get("event_name") or m.get("question", "") or "")
        if not info:
            continue
        if info["location"] != city:
            continue
        if info["date"] != date_str:
            continue
        candidates.append((m, info["metric"]))

    if not candidates:
        result["reason"] = "no_market_for_today"
        return result

    # Assume one active metric per city for the day; use 'high' if present.
    high_mkts = [(m, met) for m, met in candidates if met == "high"]
    target_markets = high_mkts or candidates
    metric = target_markets[0][1]

    running_c = _running_extreme(obs, tz, cur_hour, metric)
    if running_c is None:
        result["reason"] = "no_obs_before_cutoff"
        return result

    # Find the bucket containing running_c
    pick_market = None
    pick_bucket = None
    for m, _ in target_markets:
        b, _ = parse_market_bucket(m)
        if b and _bucket_contains(running_c, b):
            pick_market = m
            pick_bucket = b
            break

    if not pick_market:
        result["reason"] = "no_bucket_match"
        result["running_c"] = running_c
        return result

    edge_c = _edge_distance_c(running_c, pick_bucket)
    result["running_c"] = round(running_c, 2)
    result["edge_c"] = round(edge_c, 2)
    result["bucket"] = _bucket_label(pick_bucket)

    price = pick_market.get("external_price_yes")
    if price is None:
        result["reason"] = "no_price"
        return result
    price = float(price)
    result["price"] = round(price, 4)

    if edge_c < LATE_EDGE_BUFFER_C:
        result["reason"] = f"borderline_edge_{edge_c:.2f}C"
        return result
    if price > LATE_PRICE_CEILING:
        result["reason"] = f"price_too_high_{price:.3f}"
        return result

    # Budget check
    budget = _load_budget()
    remaining = LATE_DAILY_BUDGET - budget["spent"]
    size = min(LATE_MAX_POSITION, remaining)
    if size < 5:
        result["reason"] = "daily_budget_exhausted"
        return result

    result["status"] = "buy"
    result["size_usd"] = size
    result["market_id"] = pick_market.get("id")
    result["question"] = pick_market.get("question") or pick_market.get("event_name")
    result["metric"] = metric

    if dry_run:
        log(f"  [DRY] {city}: running={running_c:.2f}°C locked in {result['bucket']} "
            f"@ ${price:.3f} (edge {edge_c:.2f}°C) size=${size:.0f}")
        return result

    # Live entry
    reasoning = (
        f"LATE: {city} running {metric} at {local_now.strftime('%H:%M %Z')} = "
        f"{running_c:.1f}°C, locked into {result['bucket']} (edge {edge_c:.2f}°C). "
        f"Entry price ${price:.3f} <= ceiling ${LATE_PRICE_CEILING:.2f}."
    )
    signal_data = {
        "mode": "late",
        "city": city,
        "date": date_str,
        "metric": metric,
        "running_temp_c": round(running_c, 2),
        "bucket": result["bucket"],
        "edge_c": round(edge_c, 3),
        "local_entry_hour": cur_hour,
        "price": round(price, 4),
    }
    trade = execute_trade(
        market_id=pick_market.get("id"), side="yes", amount=size,
        reasoning=reasoning, signal_data=signal_data,
    )

    if not trade.get("success"):
        result["status"] = "error"
        result["reason"] = trade.get("error", "unknown")
        log(f"  [ERR] {city}: trade failed - {result['reason']}")
        return result

    shares = trade.get("shares_bought") or trade.get("shares") or 0
    log(f"  [BUY] {city}: {shares:.0f} @ ${price:.3f} bucket={result['bucket']} "
        f"({'paper' if trade.get('simulated') else 'live'})")

    # Update budget
    budget["spent"] += size
    _save_budget(budget)

    # Paper journal entry (mirrors CORE/PUNT pattern)
    if trade.get("simulated"):
        try:
            log_paper_trade(
                market_id=pick_market.get("id"),
                question=pick_market.get("question", "") or pick_market.get("event_name", ""),
                side="yes", entry_price=price, shares=shares,
                cost=size, bucket=result["bucket"],
                forecast_temp=running_c * 9 / 5 + 32,  # store as °F (journal convention)
                signal_strength="late_locked",
                location=city, date_str=date_str, metric=metric,
                models_used=0, agreement_pct=100.0, spread=0.0,
                model_temps={"twc_running": round(running_c, 2)},
                strategy="late",
                confidence=None,
            )
        except Exception as e:
            log_error("journal_late", str(e), city=city)

    return result


def main():
    ap = argparse.ArgumentParser(description="LATE mode: intraday day-of weather trader")
    ap.add_argument("--live", action="store_true", help="Execute real trades (default: dry run)")
    ap.add_argument("--city", help="Process only this city, ignore time window")
    ap.add_argument("--force", action="store_true", help="Ignore time-of-day window")
    ap.add_argument("--hour", type=int, help="Override LATE_ENTRY_HOUR for this run")
    args = ap.parse_args()

    if args.hour is not None:
        global LATE_ENTRY_HOUR
        LATE_ENTRY_HOUR = args.hour

    if not LATE_MODE:
        print("LATE_MODE disabled via SIMMER_WEATHER_LATE_MODE=0")
        return 0

    dry = not args.live
    cities = _cities_in_window(force=args.force or bool(args.city), specific=args.city)
    if not cities:
        print(f"LATE: no allowed cities currently at local hour {LATE_ENTRY_HOUR} (whitelist: {LATE_ALLOWED_CITIES})")
        return 0

    print(f"LATE mode ({'DRY' if dry else 'LIVE'}): scanning {len(cities)} cities at entry window")
    print(f"  ceiling=${LATE_PRICE_CEILING:.2f}  max_size=${LATE_MAX_POSITION:.0f}  "
          f"edge>=${LATE_EDGE_BUFFER_C:.2f}°C  hour={LATE_ENTRY_HOUR}")

    n_buy = 0
    for city in cities:
        r = _scan_city(city, dry_run=dry)
        if r["status"] == "buy":
            n_buy += 1
        elif r["status"] == "skip":
            print(f"  [SKIP] {city}: {r.get('reason')}"
                  + (f" (running={r.get('running_c')}°C bucket={r.get('bucket')} price=${r.get('price')})"
                     if r.get("bucket") else ""))
        elif r["status"] == "error":
            print(f"  [ERR] {city}: {r.get('reason')}")

    print(f"LATE done: {n_buy}/{len(cities)} entered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
