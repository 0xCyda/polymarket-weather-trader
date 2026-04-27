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
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from weather_trader import (
    CONFIG_SCHEMA, fetch_weather_markets, parse_market_bucket,
    parse_weather_event, execute_trade, log_error,
    validate_live_trading_prereqs, get_client,
)
from paper_journal import (
    _HISTORICAL_LOCATIONS as LOCATIONS,
    log_paper_trade,
)

# TWC (Wunderground) station codes for intraday observations. Used for late-entry
# signal generation (current temp within the day) — distinct from the resolved
# actual_temp, which is sourced from Polymarket via paper_journal.
STATIONS = {
    "NYC":           "KLGA:9:US",
    "Chicago":       "KORD:9:US",
    "Seattle":       "KSEA:9:US",
    "Atlanta":       "KATL:9:US",
    "Dallas":        "KDFW:9:US",
    "Miami":         "KMIA:9:US",
    "Houston":       "KIAH:9:US",
    "San Francisco": "KSFO:9:US",
    "Phoenix":       "KPHX:9:US",
    "Los Angeles":   "KLAX:9:US",
    "Denver":        "KDEN:9:US",
    "Austin":        "KAUS:9:US",
    "Las Vegas":     "KLAS:9:US",
    "Tokyo":         "RJTT:9:JP",
    "Seoul":         "RKSS:9:KR",
    "Munich":        "EDDM:9:DE",
    "Warsaw":        "EPWA:9:PL",
    "London":        "EGLL:9:GB",
    "Paris":         "LFPG:9:FR",
    "Ankara":        "LTAC:9:TR",
    "Toronto":       "CYYZ:9:CA",
    "Wellington":    "NZWN:9:NZ",
    "Sao Paulo":     "SBGR:9:BR",
    "Shanghai":      "ZSPD:9:CN",
    "Tel Aviv":      "LLBG:9:IL",
    "Singapore":     "WSSS:9:SG",
    "Hong Kong":     "VHHH:9:HK",
    "Buenos Aires":  "SAEZ:9:AR",
    "Beijing":       "ZBAA:9:CN",
    "Chengdu":       "ZUUU:9:CN",
    "Chongqing":     "ZUCK:9:CN",
    "Lucknow":       "VILK:9:IN",
    "Milan":         "LIMC:9:IT",
    "Shenzhen":      "ZGSZ:9:CN",
    "Wuhan":         "ZHHH:9:CN",
}

# Use the same config loading path as weather_trader so config.json, env, and
# CLI overrides all resolve identically to the CORE/PUNT pipeline.
from simmer_sdk.skill import load_config
_cfg = load_config(CONFIG_SCHEMA, str(_HERE / "weather_trader.py"), slug="polymarket-weather-trader")

LATE_MODE            = bool(_cfg.get("late_mode", True))
LATE_PRICE_CEILING   = float(_cfg.get("late_price_ceiling", 0.90))
LATE_PRICE_FLOOR     = float(_cfg.get("late_price_floor", 0.55))
LATE_MAX_POSITION    = float(_cfg.get("late_max_position_usd", 125.0))
LATE_ENTRY_HOUR      = int(_cfg.get("late_entry_hour", 15))
LATE_EDGE_BUFFER_C   = float(_cfg.get("late_edge_buffer_c", 0.3))
LATE_ALLOWED_CITIES  = [c.strip() for c in str(_cfg.get("late_cities", "")).split(",") if c.strip()]

# Per-city price ceilings derived from the Jan-Apr 2026 backtest hit rate
# (DST-corrected 3pm-local snapshot) with a 3¢ safety margin.
# Formula: hit * 0.9 / (hit * 0.9 + 1 - hit) - 0.03, capped at 0.95.
# The effective ceiling for any city is min(LATE_PRICE_CEILING, city), so
# the global knob still acts as a portfolio-wide cap.
LATE_CITY_CEILINGS: dict[str, float] = {
    "Los Angeles": 0.95,  # 100.0% hit
    "Miami":       0.94,  # 97.7%
    "London":      0.92,  # 95.5%
    "Seattle":     0.92,  # 95.4%
    "Singapore":   0.91,  # 94.9%
    "Sao Paulo":   0.88,  # 92.3%
    "Shanghai":    0.88,  # 92.3%
    "Chicago":     0.87,  # 90.9%
    "Toronto":     0.82,  # 86.4%
    "Dallas":      0.81,  # 85.1%
    "Tokyo":       0.73,  # 78.6%
    "Beijing":     0.72,  # 77.4%
    # Paris dropped from whitelist (69.7% post-DST fix, below 70% threshold).
}

LATE_CITY_PRIORS: dict[str, float] = {
    "Los Angeles": 1.000,
    "Miami": 0.977,
    "London": 0.955,
    "Seattle": 0.954,
    "Singapore": 0.949,
    "Sao Paulo": 0.923,
    "Shanghai": 0.923,
    "Chicago": 0.909,
    "Toronto": 0.864,
    "Dallas": 0.851,
    "Tokyo": 0.786,
    "Beijing": 0.774,
}
LATE_DEFAULT_CITY_PRIOR = 0.74

TWC_API_KEY = os.environ.get("TWC_API_KEY", "6532d6454b8aa370768e63d6ba5a832e")


def _effective_ceiling(city: str) -> float:
    """Tighter of the global LATE_PRICE_CEILING and this city's backtest-derived cap."""
    return min(LATE_PRICE_CEILING, LATE_CITY_CEILINGS.get(city, LATE_PRICE_CEILING))


def _late_city_prior(city: str) -> float:
    return LATE_CITY_PRIORS.get(city, LATE_DEFAULT_CITY_PRIOR)


def _late_maturity_bonus(local_hour: int) -> float:
    if local_hour >= LATE_ENTRY_HOUR + 3:
        return 0.04
    if local_hour >= LATE_ENTRY_HOUR + 2:
        return 0.03
    if local_hour >= LATE_ENTRY_HOUR + 1:
        return 0.02
    return 0.01


def _late_lock_bonus(edge_c: float) -> float:
    if edge_c >= 1.0:
        return 0.06
    if edge_c >= 0.8:
        return 0.045
    if edge_c >= 0.6:
        return 0.03
    if edge_c >= 0.45:
        return 0.02
    return 0.01


def _estimate_late_probability(city: str, edge_c: float, local_hour: int) -> float:
    """Approximate true win probability for a LATE setup.

    Base = city hit rate from the Jan-Apr backtest. Then bump it modestly for:
      - how far inside the bucket the running max already sits
      - how mature the local day is beyond the 3pm snapshot
    """
    p_true = _late_city_prior(city) + _late_lock_bonus(edge_c) + _late_maturity_bonus(local_hour)
    return max(0.55, min(0.985, p_true))


def _size_late_trade(model_edge: float) -> float:
    """Edge-banded LATE sizing.

    Confidence decides entry. Mispricing decides size.
    """
    if model_edge < 0.02:
        return 0.0
    if model_edge < 0.04:
        return 35.0
    if model_edge < 0.06:
        return 60.0
    if model_edge < 0.08:
        return 85.0
    return 125.0


# ----- TWC intraday fetch -----

def _fetch_twc_intraday(station: str, date_str: str, retries: int = 3) -> list[dict]:
    """Fetch hourly obs for a station on a date (YYYY-MM-DD).
    Retries with exponential backoff on transient errors and 429s."""
    import time as _time
    compact = date_str.replace("-", "")
    url = (
        f"https://api.weather.com/v1/location/{station}/observations/historical.json"
        f"?apiKey={TWC_API_KEY}&units=m&startDate={compact}&endDate={compact}"
    )
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 429:
                _time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                log_error("twc_http", f"status={r.status_code}", station=station, date=date_str)
                return []
            return r.json().get("observations", []) or []
        except Exception as e:
            if attempt == retries - 1:
                log_error("twc_exc", str(e), station=station, date=date_str)
                return []
            _time.sleep(2 ** attempt)
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


def _scan_city(city: str, dry_run: bool, markets: list | None = None, log=print) -> dict:
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

    # Check for duplicate: skip if core/punt already holds this city+date
    try:
        from paper_journal import get_open_positions
        for pos in get_open_positions():
            if (pos.get("location") == city
                    and pos.get("target_date") == date_str
                    and pos.get("strategy") != "late"):
                result["reason"] = f"already_held_by_{pos.get('strategy', 'core')}"
                return result
    except Exception:
        pass

    obs = _fetch_twc_intraday(station, date_str)
    if not obs:
        result["reason"] = "twc_empty"
        return result

    # Today's markets for this city. Accept a preloaded list so the main loop
    # only hits Simmer once per scan across all cities.
    if markets is None:
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
    ceiling = _effective_ceiling(city)
    result["ceiling"] = ceiling
    if price > ceiling:
        result["reason"] = f"price_too_high_{price:.3f}_vs_{ceiling:.2f}"
        return result
    # LATE thesis requires the market to already agree the bucket is locked in
    # (running max sits inside it post-peak). If the market is pricing the
    # bucket below LATE_PRICE_FLOOR, it expects the day to climb past our
    # running max into a different bucket — usually correct, since most of these
    # signals fire pre-peak when running max is just an early-day reading.
    if price < LATE_PRICE_FLOOR:
        result["reason"] = f"price_too_low_{price:.3f}_vs_{LATE_PRICE_FLOOR:.2f}"
        return result

    est_prob = _estimate_late_probability(city, edge_c, cur_hour)
    model_edge = est_prob - price
    result["estimated_prob"] = round(est_prob, 4)
    result["model_edge"] = round(model_edge, 4)

    size = min(LATE_MAX_POSITION, _size_late_trade(model_edge))
    if size < 5:
        result["reason"] = f"edge_too_thin_{model_edge:.3f}"
        return result

    # Respect paper balance — but no daily LATE budget anymore.
    try:
        from paper_journal import get_open_positions, get_stats
        stats = get_stats()
        paper_balance = float(_cfg.get("paper_balance", 10000.0))
        open_exposure = sum(float(p.get("cost", 0)) for p in get_open_positions())
        available = paper_balance + stats.get("total_pnl", 0) - open_exposure
        size = min(size, max(0, available * 0.5))
    except Exception:
        pass
    if size < 5:
        result["reason"] = "insufficient_balance"
        return result

    result["status"] = "buy"
    result["size_usd"] = round(size, 2)
    result["market_id"] = pick_market.get("id")
    result["question"] = pick_market.get("question") or pick_market.get("event_name")
    result["metric"] = metric

    # Default (no --live) is paper mode: execute_trade still runs via the Simmer
    # SDK, which simulates when WALLET_PRIVATE_KEY is unset — same pattern as
    # CORE/PUNT in weather_trader.py. The journal entry below is gated on
    # trade.simulated, not on dry_run, so paper trades land in paper_trades.jsonl
    # while real wallet trades go through --live (validated in main()).
    mode_tag = "PAPER" if dry_run else "LIVE"
    log(f"  [{mode_tag}] {city}: running={running_c:.2f}°C locked in {result['bucket']} "
        f"@ ${price:.3f} (lock {edge_c:.2f}°C, p≈{est_prob:.3f}, misprice={model_edge:+.3f}) size=${size:.0f}")

    reasoning = (
        f"LATE: {city} running {metric} at {local_now.strftime('%H:%M %Z')} = "
        f"{running_c:.1f}°C, locked into {result['bucket']} (edge {edge_c:.2f}°C). "
        f"Estimated true win probability {est_prob:.1%} vs market price ${price:.3f} "
        f"(mispricing {model_edge:+.1%}); sized at ${size:.0f}."
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
        "estimated_prob": round(est_prob, 4),
        "model_edge": round(model_edge, 4),
        "size_usd": round(size, 2),
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
                confidence=est_prob,
                polymarket_token_id=pick_market.get("polymarket_token_id"),
                polymarket_no_token_id=pick_market.get("polymarket_no_token_id"),
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
    if not dry:
        validate_live_trading_prereqs()

    # Prime the SimmerClient singleton in paper or live mode BEFORE any
    # execute_trade call. Default is live=True; without this, _scan_city's
    # first execute_trade hits the live wallet path and fails with
    # "No Polymarket wallet found" — same fix weather_trader applies at
    # line ~2022 of run_weather_strategy.
    get_client(live=not dry)

    cities = _cities_in_window(force=args.force or bool(args.city), specific=args.city)
    if not cities:
        print(f"LATE: no allowed cities currently at local hour {LATE_ENTRY_HOUR} (whitelist: {LATE_ALLOWED_CITIES})")
        return 0

    print(f"LATE mode ({'DRY' if dry else 'LIVE'}): scanning {len(cities)} cities at entry window")
    print(f"  price=${LATE_PRICE_FLOOR:.2f}-${LATE_PRICE_CEILING:.2f}  max_size=${LATE_MAX_POSITION:.0f}  "
          f"lock>=${LATE_EDGE_BUFFER_C:.2f}°C  hour={LATE_ENTRY_HOUR}  sizing=edge-banded")

    # Fetch Simmer market list once per scan and reuse across cities.
    markets = fetch_weather_markets()

    n_buy = 0
    for city in cities:
        r = _scan_city(city, dry_run=dry, markets=markets)
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
