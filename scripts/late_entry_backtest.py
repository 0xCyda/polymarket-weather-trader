#!/usr/bin/env python3
"""
Late-entry intraday backtest for Polymarket weather markets.

Strategy: check TWC intraday observations at "entry time" (~3pm local).
If the running max is locked into a specific bucket, enter YES on that bucket.
Simulate P&L at various assumed entry prices.

Data source: polymarket_events.jsonl (resolved markets) + TWC hourly obs.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

_BASE = Path(__file__).resolve().parent.parent
EVENTS_FILE = _BASE / "data" / "polymarket_events.jsonl"
CACHE_FILE = _BASE / "data" / "twc_intraday_cache.json"
RESULTS_FILE = _BASE / "data" / "late_entry_backtest_results.jsonl"

TWC_API_KEY = "6532d6454b8aa370768e63d6ba5a832e"

# Station codes: city name (as it appears in event titles) -> TWC station
STATIONS = {
    "New York":      "KLGA:9:US",
    "Chicago":       "KORD:9:US",
    "Seattle":       "KSEA:9:US",
    "Dallas":        "KDFW:9:US",
    "Miami":         "KMIA:9:US",
    "Houston":       "KIAH:9:US",
    "Phoenix":       "KPHX:9:US",
    "Los Angeles":   "KLAX:9:US",
    "London":        "EGLC:9:GB",
    "Tokyo":         "RJTT:9:JP",
    "Seoul":         "RKSS:9:KR",
    "Singapore":     "WSSS:9:SG",
    "Hong Kong":     "VHHH:9:HK",
    "Beijing":       "ZBAA:9:CN",
    "Shanghai":      "ZSPD:9:CN",
    "Shenzhen":      "ZGSZ:9:CN",
    "Sydney":        "YSSY:9:AU",
    "Dubai":         "OMDB:9:AE",
    "Bangkok":       "VTBS:9:TH",
    "Mumbai":        "VABB:9:IN",
    "Paris":         "LFPG:9:FR",
    "Toronto":       "CYYZ:9:CA",
    "São Paulo":     "SBGR:9:BR",
    "Sao Paulo":     "SBGR:9:BR",
}

# Local time offset from UTC for "entry time" at 15:00 local (UTC hour of 3pm local)
# We check obs up to this UTC hour on the measurement date
CITY_ENTRY_UTC_HOUR = {
    "New York":   20,   # 15:00 ET (UTC-5)
    "Chicago":    21,   # 15:00 CT (UTC-6)
    "Seattle":    23,   # 15:00 PT (UTC-8)
    "Dallas":     21,
    "Miami":      20,
    "Houston":    21,
    "Phoenix":    22,   # 15:00 MT (UTC-7)
    "Los Angeles": 23,
    "Toronto":    20,
    "Sao Paulo":  18,   # 15:00 BRT (UTC-3)
    "São Paulo":  18,
    "London":     15,   # 15:00 GMT (UTC+0) / 14:00 BST (UTC+1) -- use 15 as baseline
    "Paris":      14,   # 15:00 CET (UTC+1)
    "Dubai":      11,   # 15:00 GST (UTC+4)
    "Bangkok":     8,   # 15:00 ICT (UTC+7)
    "Singapore":   7,   # 15:00 SGT (UTC+8)
    "Hong Kong":   7,
    "Shanghai":    7,
    "Shenzhen":    7,
    "Beijing":     7,
    "Seoul":       6,   # 15:00 KST (UTC+9)
    "Tokyo":       6,
    "Sydney":      5,   # 15:00 AEDT (UTC+11) / AEST (UTC+10) -- use 5
    "Mumbai":      9,   # 15:00 IST (UTC+5:30) -- use 9
}


def fetch_twc_intraday(station: str, date_str: str) -> list[dict]:
    """Fetch all hourly obs for a station on a date. Returns list of obs dicts."""
    date_compact = date_str.replace("-", "")
    url = (
        f"https://api.weather.com/v1/location/{station}/observations/historical.json"
        f"?apiKey={TWC_API_KEY}&units=m&startDate={date_compact}&endDate={date_compact}"
    )
    resp = requests.get(url, timeout=15)
    if resp.status_code != 200:
        return []
    return resp.json().get("observations", [])


def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache))


def get_intraday(station: str, date_str: str, cache: dict) -> list[dict]:
    key = f"{station}:{date_str}"
    if key in cache:
        return cache[key]
    time.sleep(0.4)
    obs = fetch_twc_intraday(station, date_str)
    cache[key] = obs
    return obs


def parse_winning_temp(bucket_title: str) -> tuple[float | None, float | None, bool, bool]:
    """
    Parse a Polymarket bucket title like '47-48°F', '32–33°F', '44°F or below',
    '55°F or higher', '9°C', '-2°C or below', '-5 to -3°C'.
    Returns (low_f, high_f, is_bottom_bucket, is_top_bucket) in Fahrenheit.
    Returns (None, None, False, False) on parse failure.
    """
    import re
    s = bucket_title.strip()
    is_bottom = "or below" in s or "or lower" in s
    is_top = "or higher" in s or "or above" in s
    is_celsius = "°C" in s

    # Normalize dashes to ASCII hyphen for consistent parsing, strip unit glyphs.
    norm = s.replace("–", "-").replace("—", "-")
    norm = norm.replace("°F", "").replace("°C", "")
    norm = re.sub(r"(?i)\bor (below|lower|higher|above)\b", "", norm).strip()
    # Strip a stray " to "
    norm = norm.replace(" to ", " - ")

    def to_f(v: float) -> float:
        return v * 9 / 5 + 32 if is_celsius else v

    # Range: "A-B" where A and B may be negative. Allow both hyphen and spaces.
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*", norm)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        lo_raw, hi_raw = (a, b) if a <= b else (b, a)
        return to_f(lo_raw), to_f(hi_raw), is_bottom, is_top

    # Single value
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*", norm)
    if m:
        val = to_f(float(m.group(1)))
        if is_bottom:
            return -999.0, val, True, False
        if is_top:
            return val, 999.0, False, True
        return val, val + (1.8 if is_celsius else 1.0), False, False

    return None, None, False, False


def _obs_utc_hour(o: dict) -> int | None:
    """Extract UTC hour from observation using valid_time_gmt (Unix epoch)."""
    vt = o.get("valid_time_gmt")
    if vt is None:
        return None
    try:
        return datetime.fromtimestamp(int(vt), tz=timezone.utc).hour
    except Exception:
        return None


def running_max_at_utc_hour(obs: list[dict], utc_hour: int) -> float | None:
    """Return max observed temp (°C) from obs up to and including utc_hour."""
    temps = []
    for o in obs:
        h = _obs_utc_hour(o)
        if h is None or h > utc_hour:
            continue
        t = o.get("temp")
        if t is not None:
            temps.append(float(t))
    return max(temps) if temps else None


def temp_in_bucket(temp_c: float, lo_f: float, hi_f: float) -> bool:
    temp_f = temp_c * 9 / 5 + 32
    return lo_f - 1.0 <= temp_f <= hi_f + 1.0


def find_city_in_title(title: str) -> str | None:
    for city in STATIONS:
        if city in title:
            return city
    return None


def parse_events() -> list[dict]:
    cutoff = datetime(2026, 1, 23, tzinfo=timezone.utc)
    events = []
    for line in EVENTS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        title = ev.get("title", "")
        end_date = ev.get("endDate", "")
        if not end_date:
            continue

        # Only high/low temp markets
        is_high = "Highest temperature" in title or "highest temperature" in title
        is_low = "Lowest temperature" in title or "lowest temperature" in title
        if not is_high and not is_low:
            continue

        city = find_city_in_title(title)
        if not city:
            continue

        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except Exception:
            continue

        if end_dt < cutoff:
            continue

        # The measurement date is the endDate date (market measures temp on that day)
        measure_date = end_dt.date().isoformat()

        # Find winning bucket
        winner_title = None
        winner_lo_f = None
        winner_hi_f = None
        winner_is_bottom = False
        winner_is_top = False
        for m in ev.get("markets", []):
            prices = json.loads(m.get("outcomePrices", '["0","1"]'))
            if len(prices) >= 1 and prices[0] == "1":
                wt = m.get("groupItemTitle", "")
                lo, hi, ib, it = parse_winning_temp(wt)
                if lo is not None:
                    winner_title = wt
                    winner_lo_f = lo
                    winner_hi_f = hi
                    winner_is_bottom = ib
                    winner_is_top = it
                break

        if winner_title is None:
            continue

        events.append({
            "city": city,
            "metric": "high" if is_high else "low",
            "measure_date": measure_date,
            "winner_title": winner_title,
            "winner_lo_f": winner_lo_f,
            "winner_hi_f": winner_hi_f,
            "winner_is_bottom": winner_is_bottom,
            "winner_is_top": winner_is_top,
            "title": title,
        })
    return events


def backtest(events: list[dict], entry_prices: list[float], stake: float = 100.0) -> None:
    cache = load_cache()
    results = []
    total = len(events)
    print(f"Backtesting {total} events...")

    seen = set()
    for i, ev in enumerate(events):
        city = ev["city"]
        date_str = ev["measure_date"]
        metric = ev["metric"]
        key = f"{city}:{date_str}:{metric}"
        if key in seen:
            continue
        seen.add(key)

        station = STATIONS[city]
        entry_utc_hour = CITY_ENTRY_UTC_HOUR.get(city, 14)

        obs = get_intraday(station, date_str, cache)
        if not obs:
            continue

        # Compute running temp at entry time
        if metric == "high":
            running_val = running_max_at_utc_hour(obs, entry_utc_hour)
        else:
            temps = []
            for o in obs:
                ts = o.get("obsTimeUtc", "")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    continue
                if dt.hour <= entry_utc_hour:
                    t = o.get("temp")
                    if t is not None:
                        temps.append(float(t))
            running_val = min(temps) if temps else None

        if running_val is None:
            continue

        # All temps for the full day (for determining actual daily max)
        all_temps = [float(o["temp"]) for o in obs if o.get("temp") is not None]
        if not all_temps:
            continue
        actual_temp_c = max(all_temps) if metric == "high" else min(all_temps)
        actual_temp_f = actual_temp_c * 9 / 5 + 32

        # Would we predict the correct bucket at entry time?
        predicted_in_winner = temp_in_bucket(running_val, ev["winner_lo_f"], ev["winner_hi_f"])

        # How many obs available before entry time
        obs_before = sum(
            1 for o in obs
            if o.get("obsTimeUtc") and datetime.fromisoformat(o["obsTimeUtc"].replace("Z", "+00:00")).hour <= entry_utc_hour
        )

        # Confidence: if running_val is clearly in a 2°F bucket (not borderline)
        running_f = running_val * 9 / 5 + 32
        borderline = (ev["winner_lo_f"] - running_f) > -0.5 or (running_f - ev["winner_hi_f"]) > -0.5
        confident = obs_before >= 4 and not borderline

        results.append({
            "city": city,
            "date": date_str,
            "metric": metric,
            "winner": ev["winner_title"],
            "winner_lo_f": ev["winner_lo_f"],
            "winner_hi_f": ev["winner_hi_f"],
            "running_temp_c": round(running_val, 1),
            "running_temp_f": round(running_f, 1),
            "actual_temp_f": round(actual_temp_f, 1),
            "obs_before_entry": obs_before,
            "predicted_correct": predicted_in_winner,
            "confident": confident,
        })

        if (i + 1) % 20 == 0:
            save_cache(cache)
            print(f"  {i+1}/{total} processed, {len(results)} with data...")

    save_cache(cache)

    if not results:
        print("No results with TWC data.")
        return

    # Save raw results
    with RESULTS_FILE.open("w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    # Stats
    n = len(results)
    correct = sum(1 for r in results if r["predicted_correct"])
    confident_results = [r for r in results if r["confident"]]
    n_conf = len(confident_results)
    correct_conf = sum(1 for r in confident_results if r["predicted_correct"])

    print(f"\n{'='*60}")
    print(f"LATE-ENTRY INTRADAY BACKTEST RESULTS")
    print(f"{'='*60}")
    print(f"Events with TWC data:  {n}")
    print(f"Correct bucket at 3pm: {correct}/{n} = {correct/n*100:.1f}%")
    if n_conf:
        print(f"Confident signals:     {n_conf} events")
        print(f"Correct (confident):   {correct_conf}/{n_conf} = {correct_conf/n_conf*100:.1f}%")

    print(f"\nP&L simulation (${stake:.0f} stake per trade, ALL signals):")
    print(f"{'Entry Price':>12} {'Trades':>8} {'Wins':>6} {'Win%':>6} {'P&L':>10} {'ROI':>8}")
    for ep in entry_prices:
        shares = stake / ep
        win_pnl = shares * 1.0 - stake  # net on correct
        loss_pnl = -stake                # total loss on wrong
        total_pnl = correct * win_pnl + (n - correct) * loss_pnl
        roi = total_pnl / (n * stake) * 100
        print(f"  {ep:.2f}         {n:>8} {correct:>6} {correct/n*100:>5.1f}%  ${total_pnl:>9,.0f}  {roi:>7.1f}%")

    if n_conf:
        print(f"\nP&L simulation (${stake:.0f} stake per trade, CONFIDENT signals only):")
        print(f"{'Entry Price':>12} {'Trades':>8} {'Wins':>6} {'Win%':>6} {'P&L':>10} {'ROI':>8}")
        for ep in entry_prices:
            shares = stake / ep
            win_pnl = shares * 1.0 - stake
            loss_pnl = -stake
            total_pnl = correct_conf * win_pnl + (n_conf - correct_conf) * loss_pnl
            roi = total_pnl / (n_conf * stake) * 100
            print(f"  {ep:.2f}         {n_conf:>8} {correct_conf:>6} {correct_conf/n_conf*100:>5.1f}%  ${total_pnl:>9,.0f}  {roi:>7.1f}%")

    # City breakdown
    print(f"\nBreakdown by city:")
    from collections import defaultdict
    by_city = defaultdict(list)
    for r in results:
        by_city[r["city"]].append(r)
    for city in sorted(by_city):
        cr = by_city[city]
        c = sum(1 for r in cr if r["predicted_correct"])
        print(f"  {city:<15} {c}/{len(cr)} = {c/len(cr)*100:.0f}%")


def main():
    print("Parsing events file...")
    events = parse_events()
    print(f"Found {len(events)} resolved high/low temp events (last 90 days, cities with stations)")

    # Limit to avoid too many API calls in one run
    # Deduplicate by city+date+metric (one fetch per combo)
    unique = {}
    for ev in events:
        k = f"{ev['city']}:{ev['measure_date']}:{ev['metric']}"
        if k not in unique:
            unique[k] = ev
    deduped = list(unique.values())
    print(f"Unique city-date-metric combos: {len(deduped)}")

    # Only process combos where station exists
    deduped = [e for e in deduped if e["city"] in STATIONS]
    print(f"With TWC station:  {len(deduped)}")

    if not deduped:
        print("No events to process.", file=sys.stderr)
        sys.exit(1)

    backtest(
        deduped,
        entry_prices=[0.55, 0.60, 0.65, 0.70, 0.75, 0.80],
        stake=100.0,
    )


if __name__ == "__main__":
    main()
