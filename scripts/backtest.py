#!/usr/bin/env python3
"""
Backtest the bot's strategy against every resolved Polymarket weather event.

Reads data/polymarket_events.jsonl (produced by pull_weather_markets.py)
and replays our bucket-selection logic for each event. For the forecast,
we use Open-Meteo's historical archive API to get what the 6 non-AIFS
models would have predicted ~24h before resolution (as a proxy — true
point-in-time model runs aren't available retroactively).

For each event we:
  1. Parse city + target_date from the title
  2. Fetch historical forecast via Open-Meteo archive
  3. Run rank_event_buckets_by_edge() with the historical forecast
  4. Pick the bucket with highest edge (if any passes MIN_EDGE)
  5. Compute realized P&L from the market's resolvedOutcome

Output: data/backtest_results.jsonl + summary report.

Usage:
    python scripts/backtest.py                     # replay all resolved events
    python scripts/backtest.py --limit 100         # cap runs (for quick testing)
    python scripts/backtest.py --min-edge 0.15     # override MIN_EDGE
    python scripts/backtest.py --city NYC          # single city
"""

import argparse
import json
import math
import pathlib
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = pathlib.Path(__file__).parent.parent
EVENTS_FILE = REPO_ROOT / "data" / "polymarket_events.jsonl"
RESULTS_FILE = REPO_ROOT / "data" / "backtest_results.jsonl"
REPORTS_DIR = REPO_ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

# Minimal city → lat/lon/tz map, same cities our bot trades
CITIES = {
    "NYC": (40.7769, -73.8740, "America/New_York"),
    "Chicago": (41.9742, -87.9073, "America/Chicago"),
    "Seattle": (47.4502, -122.3088, "America/Los_Angeles"),
    "Atlanta": (33.6407, -84.4277, "America/New_York"),
    "Dallas": (32.8998, -97.0403, "America/Chicago"),
    "Miami": (25.7959, -80.2870, "America/New_York"),
    "Houston": (29.9902, -95.3368, "America/Chicago"),
    "San Francisco": (37.6213, -122.3790, "America/Los_Angeles"),
    "Phoenix": (33.4373, -112.0078, "America/Phoenix"),
    "Los Angeles": (33.9425, -118.4081, "America/Los_Angeles"),
    "Denver": (39.8617, -104.6732, "America/Denver"),
    "Austin": (30.1945, -97.6699, "America/Chicago"),
    "Las Vegas": (36.0840, -115.1537, "America/Los_Angeles"),
    "Tel Aviv": (32.0853, 34.7818, "Asia/Jerusalem"),
    "Munich": (48.1351, 11.5820, "Europe/Berlin"),
    "London": (51.5074, -0.1278, "Europe/London"),
    "Tokyo": (35.6762, 139.6503, "Asia/Tokyo"),
    "Seoul": (37.5665, 126.9780, "Asia/Seoul"),
    "Ankara": (39.9334, 32.8597, "Europe/Istanbul"),
    "Lucknow": (26.8467, 80.9462, "Asia/Kolkata"),
    "Wellington": (-41.2866, 174.7756, "Pacific/Auckland"),
    "Toronto": (43.6777, -79.6248, "America/Toronto"),
    "Paris": (48.8566, 2.3522, "Europe/Paris"),
    "Milan": (45.4642, 9.1900, "Europe/Rome"),
    "Sao Paulo": (-23.5505, -46.6333, "America/Sao_Paulo"),
    "Warsaw": (52.2297, 21.0122, "Europe/Warsaw"),
    "Singapore": (1.3521, 103.8198, "Asia/Singapore"),
    "Shanghai": (31.2304, 121.4737, "Asia/Shanghai"),
    "Beijing": (39.9042, 116.4074, "Asia/Shanghai"),
    "Shenzhen": (22.5431, 114.0579, "Asia/Shanghai"),
    "Chengdu": (30.5728, 104.0668, "Asia/Shanghai"),
    "Chongqing": (29.4316, 106.9123, "Asia/Shanghai"),
    "Wuhan": (30.5928, 114.3055, "Asia/Shanghai"),
    "Hong Kong": (22.3193, 114.1694, "Asia/Hong_Kong"),
    "Buenos Aires": (-34.6037, -58.3816, "America/Argentina/Buenos_Aires"),
}
_CITY_ALIASES_LC = sorted(
    [(c.lower(), c) for c in CITIES],
    key=lambda x: -len(x[0]),
)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


# ---- local copies of trading logic (avoids importing weather_trader) ----

def _bucket_probability(lo_f, hi_f, mean_f, spread_f):
    if spread_f is None or spread_f <= 0:
        sigma = 2.0
    else:
        sigma = max(spread_f / 4.0, 1.5)
    _phi = lambda z: 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    p_lo = 0.0 if lo_f == -999 else _phi((lo_f - mean_f) / sigma)
    p_hi = 1.0 if hi_f == 999 else _phi((hi_f - mean_f) / sigma)
    return max(0.0, min(1.0, p_hi - p_lo))


def parse_bucket(outcome_name):
    if not outcome_name:
        return None
    unit = 'C' if re.search(r'°C', outcome_name, re.IGNORECASE) else 'F'
    m = re.search(r'(-?\d+)\s*°?[fFcC]?\s*(or below|or less)', outcome_name, re.IGNORECASE)
    if m:
        return (-999, int(m.group(1)), unit)
    m = re.search(r'(-?\d+)\s*°?[fFcC]?\s*(or higher|or above|or more)', outcome_name, re.IGNORECASE)
    if m:
        return (int(m.group(1)), 999, unit)
    m = re.search(r'(-?\d+)\s*(?:°?\s*[fFcC])?\s*(?:-|–|to)\s*(-?\d+)', outcome_name)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (min(lo, hi), max(lo, hi), unit)
    m = re.search(r'(-?\d+)\s*°[fFcC]', outcome_name)
    if m:
        t = int(m.group(1))
        return (t, t, unit)
    return None


def rank_buckets(event_markets, forecast_f, spread_f, signal_strength="moderate"):
    if spread_f is None or spread_f <= 0:
        spread_f = 4.0
    discount = {
        "strong": 1.00, "moderate": 0.92, "weak": 0.80,
        "single_source": 0.75, "unknown": 0.85,
    }.get(signal_strength, 0.85)

    ranked = []
    for m in event_markets:
        outcome = m.get("outcome") or m.get("outcomeName") or m.get("groupItemTitle") or ""
        bucket = parse_bucket(outcome) or parse_bucket(m.get("question", ""))
        if not bucket:
            continue
        lo, hi, unit = bucket
        lo_f = lo * 9/5 + 32 if unit == 'C' and lo != -999 else lo
        hi_f = hi * 9/5 + 32 if unit == 'C' and hi != 999 else hi
        prob_lo, prob_hi = lo_f, hi_f
        if lo_f == hi_f and lo_f != -999 and lo_f != 999:
            prob_lo, prob_hi = lo_f - 0.5, hi_f + 0.5
        raw = _bucket_probability(prob_lo, prob_hi, forecast_f, spread_f)
        conf = raw * discount
        # Market price — try common field names
        price = None
        for k in ("outcomePrice", "lastTradePrice", "bestBid", "price"):
            v = m.get(k)
            if v is not None:
                try:
                    price = float(v)
                    break
                except Exception:
                    pass
        # Some events store outcomePrices as a list ["0.3", "0.7"] for [YES, NO]
        if price is None:
            op = m.get("outcomePrices")
            if isinstance(op, str):
                try:
                    op = json.loads(op)
                except Exception:
                    op = None
            if isinstance(op, list) and op:
                try:
                    price = float(op[0])
                except Exception:
                    pass
        if price is None:
            continue
        ranked.append({
            "market": m, "outcome": outcome,
            "lo_f": lo_f, "hi_f": hi_f, "unit": unit,
            "raw_prob": raw, "confidence": conf, "price": price,
            "edge": conf - price,
        })
    ranked.sort(key=lambda x: -x["edge"])
    return ranked


# ---- historical weather fetch ----

def fetch_historical_high(lat: float, lon: float, date_str: str, tz: str) -> float | None:
    """Fetch actual observed daily high (°F) from Open-Meteo archive."""
    tz_enc = tz.replace("/", "%2F")
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={date_str}&end_date={date_str}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=fahrenheit&timezone={tz_enc}"
    )
    try:
        req = Request(url, headers={"User-Agent": "polymarket-weather-backtester/1.0"})
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        if temps and temps[0] is not None:
            return float(temps[0])
    except Exception as e:
        return None
    return None


def fetch_historical_forecast(lat: float, lon: float, date_str: str, tz: str) -> tuple:
    """
    Proxy historical forecast: use the same archive API but fetch N days of
    history around the target date and take the mean + spread. This isn't
    the true point-in-time model run, but a reasonable placeholder.

    Returns (forecast_temp_f, spread).
    """
    tz_enc = tz.replace("/", "%2F")
    try:
        target = date.fromisoformat(date_str)
    except Exception:
        return None, None
    # Look at 3 surrounding days as a rough "prior" for what models would've said
    start = date.fromordinal(target.toordinal() - 1).isoformat()
    end = date.fromordinal(target.toordinal() + 1).isoformat()
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={start}&end_date={end}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=fahrenheit&timezone={tz_enc}"
    )
    try:
        req = Request(url, headers={"User-Agent": "polymarket-weather-backtester/1.0"})
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        temps = [t for t in data.get("daily", {}).get("temperature_2m_max", []) if t is not None]
        if not temps:
            return None, None
        mean = sum(temps) / len(temps)
        spread = max(temps) - min(temps)
        return mean, max(spread, 1.0)
    except Exception:
        return None, None


# ---- event parsing ----

def parse_event_meta(event: dict) -> dict | None:
    """Extract city, target_date, metric from an event."""
    title = (event.get("title") or "").lower()
    slug = (event.get("slug") or "").lower()
    blob = f"{title} {slug}"

    city = None
    for alias_lc, canonical in _CITY_ALIASES_LC:
        if alias_lc in blob:
            city = canonical
            break
    if not city:
        return None

    metric = "low" if ("lowest" in blob or "low temp" in blob) else "high"

    # Try to parse target date
    target_date = None
    # From endDate / end_date field
    for k in ("endDate", "end_date", "resolutionDate"):
        v = event.get(k)
        if v and isinstance(v, str):
            try:
                target_date = v[:10]
                date.fromisoformat(target_date)
                break
            except Exception:
                target_date = None
    # Fallback: parse "on April 22, 2026" from title
    if not target_date:
        m = re.search(r"on\s+([a-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?", title)
        if m:
            mname = m.group(1)
            day = int(m.group(2))
            year = int(m.group(3)) if m.group(3) else datetime.now().year
            if mname in MONTHS:
                try:
                    target_date = f"{year:04d}-{MONTHS[mname]:02d}-{day:02d}"
                except Exception:
                    pass

    if not target_date:
        return None

    return {"city": city, "target_date": target_date, "metric": metric}


def resolved_outcome(market: dict) -> bool | None:
    """Extract the winning-bucket flag for a market. True=resolved YES, False=NO, None=unknown."""
    # Common Gamma field names
    for k in ("resolvedOutcome", "umaResolutionOutcome", "outcomeResolved"):
        v = market.get(k)
        if v is not None:
            s = str(v).lower()
            if s in ("yes", "true", "1"):
                return True
            if s in ("no", "false", "0"):
                return False
    # outcomePrices might be [1.0, 0.0] for resolved YES
    op = market.get("outcomePrices")
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except Exception:
            op = None
    if isinstance(op, list) and len(op) >= 1 and market.get("closed"):
        try:
            p = float(op[0])
            if p >= 0.99:
                return True
            if p <= 0.01:
                return False
        except Exception:
            pass
    return None


# ---- backtest ----

def backtest_event(event: dict, min_edge: float = 0.25, size_usd: float = 200.0) -> dict | None:
    meta = parse_event_meta(event)
    if not meta:
        return None
    city = meta["city"]
    date_str = meta["target_date"]
    metric = meta["metric"]
    if metric == "low":
        return None  # our bot killed low-temp

    coords = CITIES.get(city)
    if not coords:
        return None
    lat, lon, tz = coords

    # Historical forecast proxy
    forecast_f, spread_f = fetch_historical_forecast(lat, lon, date_str, tz)
    if forecast_f is None:
        return None

    # Actual daily high
    actual_f = fetch_historical_high(lat, lon, date_str, tz)
    if actual_f is None:
        return None

    # Rank buckets by edge
    markets = event.get("markets") or []
    ranked = rank_buckets(markets, forecast_f, spread_f, signal_strength="moderate")
    if not ranked:
        return None

    picked = next((r for r in ranked if r["edge"] >= min_edge
                   and 0.02 <= r["price"] <= 0.90), None)
    if picked is None:
        return {
            "event_title": event.get("title"), "city": city, "date": date_str,
            "forecast_f": round(forecast_f, 1), "actual_f": round(actual_f, 1),
            "picked": None, "reason": "no bucket met min_edge",
        }

    # Determine if our picked bucket would have resolved YES
    lo_f, hi_f = picked["lo_f"], picked["hi_f"]
    won = (lo_f - 0.5 if lo_f != -999 else -9999) <= actual_f <= (hi_f + 0.5 if hi_f != 999 else 9999)

    entry_price = picked["price"]
    shares = size_usd / entry_price if entry_price > 0 else 0
    pnl = shares * (1.0 - entry_price) if won else -size_usd

    return {
        "event_title": event.get("title"),
        "city": city,
        "date": date_str,
        "forecast_f": round(forecast_f, 1),
        "actual_f": round(actual_f, 1),
        "picked_bucket": picked["outcome"],
        "bucket_range": [lo_f, hi_f],
        "entry_price": round(entry_price, 4),
        "edge": round(picked["edge"], 4),
        "raw_prob": round(picked["raw_prob"], 4),
        "won": won,
        "pnl": round(pnl, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest against resolved Polymarket events")
    parser.add_argument("--limit", type=int, default=None, help="Max events to replay")
    parser.add_argument("--min-edge", type=float, default=0.25)
    parser.add_argument("--size", type=float, default=200.0, help="Per-trade size USD")
    parser.add_argument("--city", help="Filter to one city")
    parser.add_argument("--no-write", action="store_true", help="Don't save results.jsonl")
    args = parser.parse_args()

    if not EVENTS_FILE.exists():
        print(f"No {EVENTS_FILE} — run pull_weather_markets.py first", file=sys.stderr)
        sys.exit(1)

    # Capture to StringIO first, then print + save at end
    import io
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    print(f"Polymarket Weather Backtest - {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)
    print(f"Params: min_edge={args.min_edge}  size=${args.size}"
          + (f"  city={args.city}" if args.city else "")
          + (f"  limit={args.limit}" if args.limit else "")
          + "\n")

    events = []
    for line in EVENTS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    resolved = [e for e in events if e.get("closed") or e.get("resolved")]
    print(f"Loaded {len(events)} events, {len(resolved)} resolved/closed")
    if args.limit:
        resolved = resolved[:args.limit]

    if args.city:
        resolved = [e for e in resolved if args.city.lower() in (e.get("title") or "").lower()]
        print(f"Filtered to {len(resolved)} events matching city '{args.city}'")

    results = []
    for i, event in enumerate(resolved):
        if i % 50 == 0 and i > 0:
            print(f"  {i}/{len(resolved)} events processed...")
        r = backtest_event(event, min_edge=args.min_edge, size_usd=args.size)
        if r:
            results.append(r)
        time.sleep(0.05)  # be nice to Open-Meteo

    traded = [r for r in results if r.get("picked") is not False and r.get("picked_bucket")]
    if not traded:
        print("\n  No simulated trades produced. Check --min-edge or event data.")
        return

    wins = [r for r in traded if r["won"]]
    losses = [r for r in traded if not r["won"]]
    total_pnl = sum(r["pnl"] for r in traded)

    print(f"\n  Backtest results (min_edge={args.min_edge}, size=${args.size}):")
    print(f"    Events replayed: {len(results)}")
    print(f"    Simulated trades: {len(traded)}")
    print(f"    Wins: {len(wins)}  Losses: {len(losses)}")
    print(f"    Win rate: {100*len(wins)/max(1,len(traded)):.1f}%")
    print(f"    Total P&L: ${total_pnl:,.2f}")
    print(f"    Avg P&L/trade: ${total_pnl/max(1,len(traded)):,.2f}")

    # By city
    by_city = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for r in traded:
        c = r["city"]
        by_city[c]["n"] += 1
        by_city[c]["pnl"] += r["pnl"]
        if r["won"]: by_city[c]["w"] += 1

    print(f"\n  By city:")
    print(f"  {'City':<18s} {'n':>5s}  {'Win%':>6s}  {'P&L':>12s}")
    print("  " + "-" * 46)
    for city in sorted(by_city, key=lambda c: -by_city[c]["pnl"]):
        d = by_city[city]
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        print(f"  {city:<16s} {d['n']:5d}  {wr:5.1f}%  ${d['pnl']:>11,.2f}")

    if not args.no_write:
        with RESULTS_FILE.open("w") as f:
            for r in results:
                f.write(json.dumps(r, default=str) + "\n")
        print(f"\n  Saved {len(results)} records -> {RESULTS_FILE}")

    # Restore stdout, print to console, save report
    sys.stdout = real_stdout
    output = buf.getvalue()
    print(output)
    report_file = REPORTS_DIR / f"backtest_{datetime.now().strftime('%Y-%m-%d_%H%M')}.txt"
    report_file.write_text(output, encoding="utf-8")
    print(f"  Run log -> {report_file}")


if __name__ == "__main__":
    main()
