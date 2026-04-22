#!/usr/bin/env python3
"""
Polymarket Trader Analysis — reverse-engineer any wallet's weather strategy.

Pulls closed positions, open positions, and market metadata from the public
Polymarket Data API, filters for weather markets, and reports patterns:
  - Total P&L, win rate, trade count
  - Favorite cities / metrics / bucket sizes
  - D+0 vs D+1+ breakdown
  - Position sizing patterns
  - Best and worst trades

Usage:
    python scripts/polymarket_analyze.py 0x56687bf447db6ffa42ffe2204a05edaa20f55839
    python scripts/polymarket_analyze.py <wallet> --all       # all categories, not just weather
    python scripts/polymarket_analyze.py <wallet> --json      # raw JSON dump
"""

import json
import re
import sys
import time
from collections import Counter, defaultdict
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

WEATHER_KEYWORDS = ("temperature", "temp", "weather", "°f", "°c", "highest", "lowest")
CITIES = [
    "nyc", "new york", "chicago", "seattle", "atlanta", "dallas", "miami",
    "houston", "san francisco", "phoenix", "los angeles", "denver", "austin",
    "las vegas", "tel aviv", "munich", "london", "tokyo", "seoul", "ankara",
    "lucknow", "wellington", "toronto", "paris", "milan", "sao paulo", "warsaw",
    "singapore", "shanghai", "beijing", "shenzhen", "chengdu", "chongqing",
    "wuhan", "hong kong", "buenos aires",
]


def _get(url: str, params: dict | None = None, retries: int = 3) -> Any:
    """GET with basic retry and rate-limit tolerance. Uses stdlib urllib."""
    if params:
        url = f"{url}?{urlencode(params)}"
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "polymarket-weather-trader/1.0"})
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** attempt)
                continue
            print(f"  {e.code} on {url}: {e.reason}", file=sys.stderr)
            return None
        except URLError as e:
            if attempt == retries - 1:
                print(f"  error on {url}: {e.reason}", file=sys.stderr)
                return None
            time.sleep(2 ** attempt)
        except Exception as e:
            if attempt == retries - 1:
                print(f"  error on {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 ** attempt)
    return None


def fetch_traded_count(wallet: str) -> int | None:
    """Return total markets this wallet has traded in."""
    data = _get(f"{DATA_API}/traded", params={"user": wallet})
    if data is None:
        return None
    if isinstance(data, dict):
        return data.get("traded") or data.get("count") or data.get("total")
    if isinstance(data, (int, float)):
        return int(data)
    return None


def fetch_closed_positions(wallet: str) -> list[dict]:
    """Paginate /closed-positions until empty."""
    positions = []
    offset = 0
    limit = 50
    while True:
        page = _get(f"{DATA_API}/closed-positions",
                    params={"user": wallet, "limit": limit, "offset": offset})
        if not page or not isinstance(page, list):
            break
        positions.extend(page)
        if len(page) < limit:
            break
        offset += limit
        time.sleep(0.2)
    return positions


def fetch_open_positions(wallet: str) -> list[dict]:
    """Fetch active + redeemable positions."""
    data = _get(f"{DATA_API}/positions", params={"user": wallet})
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("positions", []) or []


def is_weather(position: dict) -> bool:
    """Heuristic: position relates to a weather market."""
    blob = " ".join(str(position.get(k, "")).lower() for k in
                    ("title", "eventSlug", "slug", "question", "outcome"))
    if not any(kw in blob for kw in WEATHER_KEYWORDS):
        return False
    return any(city in blob for city in CITIES)


def extract_city(position: dict) -> str | None:
    blob = " ".join(str(position.get(k, "")).lower() for k in
                    ("title", "eventSlug", "slug", "question"))
    # Longest city names first so "hong kong" wins over "hong"
    for city in sorted(CITIES, key=len, reverse=True):
        if city in blob:
            return city.title()
    return None


def extract_metric(position: dict) -> str:
    blob = " ".join(str(position.get(k, "")).lower() for k in
                    ("title", "eventSlug", "slug", "question"))
    if "lowest" in blob or "low temp" in blob:
        return "low"
    return "high"


def extract_bucket_size(position: dict) -> str:
    """Classify the bucket as 'exact', 'range', or 'threshold'."""
    outcome = str(position.get("outcome", "")).lower()
    if "or above" in outcome or "or higher" in outcome or "or below" in outcome or "or less" in outcome:
        return "threshold"
    if re.search(r"\d+\s*[-–to]\s*\d+", outcome):
        return "range"
    return "exact"


def extract_temp(position: dict) -> int | None:
    outcome = str(position.get("outcome", ""))
    m = re.search(r"(-?\d+)", outcome)
    return int(m.group(1)) if m else None


def position_pnl(p: dict) -> float:
    """realizedPnl for closed, realizedPnl + cashPnl for redeemable."""
    r = float(p.get("realizedPnl") or 0)
    c = float(p.get("cashPnl") or 0)
    return r + c if p.get("redeemable") else r


def position_size(p: dict) -> float:
    """Dollar cost basis — size of the entry."""
    for k in ("totalBought", "initialValue", "size", "avgPrice"):
        v = p.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def analyze(weather_positions: list[dict], label: str) -> None:
    if not weather_positions:
        print(f"\n  No {label} weather positions.")
        return

    pnls = [position_pnl(p) for p in weather_positions]
    sizes = [position_size(p) for p in weather_positions if position_size(p) > 0]
    wins = [p for p in weather_positions if position_pnl(p) > 0]
    losses = [p for p in weather_positions if position_pnl(p) < 0]
    total_pnl = sum(pnls)

    print(f"\n  {label.upper()} ({len(weather_positions)} positions)")
    print(f"  Total P&L:      ${total_pnl:,.2f}")
    print(f"  Win rate:       {len(wins)}/{len(wins)+len(losses)} "
          f"({100*len(wins)/max(1,len(wins)+len(losses)):.1f}%)")
    if sizes:
        print(f"  Avg size:       ${sum(sizes)/len(sizes):,.2f}")
        print(f"  Size range:     ${min(sizes):,.0f} – ${max(sizes):,.0f}")

    # By city
    by_city = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0, "l": 0})
    for p in weather_positions:
        c = extract_city(p) or "Unknown"
        pnl = position_pnl(p)
        by_city[c]["n"] += 1
        by_city[c]["pnl"] += pnl
        if pnl > 0: by_city[c]["w"] += 1
        elif pnl < 0: by_city[c]["l"] += 1

    print(f"\n  By city (top 10):")
    print(f"  {'City':<18s} {'n':>4s}  {'Win%':>6s}  {'P&L':>10s}")
    print("  " + "-" * 44)
    for city in sorted(by_city, key=lambda c: -by_city[c]["n"])[:10]:
        d = by_city[city]
        wr = d["w"] / max(1, d["w"] + d["l"]) * 100
        print(f"  {city:<16s} {d['n']:4d}  {wr:5.1f}%  ${d['pnl']:>9,.2f}")

    # By city, ordered by difficulty (lowest win rate first = hardest).
    # Require at least 3 resolved trades to filter out noise.
    ranked = []
    for city, d in by_city.items():
        resolved = d["w"] + d["l"]
        if resolved < 3:
            continue
        wr = d["w"] / resolved * 100
        ranked.append((city, wr, d["n"], d["pnl"], resolved))

    if ranked:
        print(f"\n  City difficulty (win rate, hardest first — min 3 resolved):")
        print(f"  {'City':<18s} {'n':>4s}  {'Win%':>6s}  {'P&L':>10s}")
        print("  " + "-" * 44)
        for city, wr, n, pnl, _ in sorted(ranked, key=lambda x: x[1]):
            marker = "  HARD" if wr < 40 else "  EASY" if wr >= 70 else ""
            print(f"  {city:<16s} {n:4d}  {wr:5.1f}%  ${pnl:>9,.2f}{marker}")

    # By metric
    hi = [p for p in weather_positions if extract_metric(p) == "high"]
    lo = [p for p in weather_positions if extract_metric(p) == "low"]
    print(f"\n  High temp: {len(hi)} positions, P&L ${sum(position_pnl(p) for p in hi):,.2f}")
    print(f"  Low temp:  {len(lo)} positions, P&L ${sum(position_pnl(p) for p in lo):,.2f}")

    # By bucket type
    bucket_types = Counter(extract_bucket_size(p) for p in weather_positions)
    print(f"\n  Bucket type preference:")
    for bt, n in bucket_types.most_common():
        bt_pnl = sum(position_pnl(p) for p in weather_positions
                     if extract_bucket_size(p) == bt)
        print(f"    {bt:<12s}  n={n:3d}  P&L=${bt_pnl:,.2f}")

    # Best / worst
    if pnls:
        best = max(weather_positions, key=position_pnl)
        worst = min(weather_positions, key=position_pnl)
        print(f"\n  Best trade:  ${position_pnl(best):,.2f}")
        print(f"    {best.get('title', best.get('eventSlug', ''))[:70]}")
        print(f"    outcome: {best.get('outcome', '?')}")
        print(f"  Worst trade: ${position_pnl(worst):,.2f}")
        print(f"    {worst.get('title', worst.get('eventSlug', ''))[:70]}")
        print(f"    outcome: {worst.get('outcome', '?')}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Reverse-engineer a Polymarket trader's strategy")
    parser.add_argument("wallet", help="0x-prefixed 40-hex wallet address")
    parser.add_argument("--all", action="store_true",
                        help="Analyze all categories, not just weather")
    parser.add_argument("--json", action="store_true",
                        help="Dump raw positions as JSON (no analysis)")
    args = parser.parse_args()

    if not re.match(r"^0x[a-fA-F0-9]{40}$", args.wallet):
        print(f"Invalid wallet: {args.wallet}", file=sys.stderr)
        sys.exit(1)

    print(f"Pulling Polymarket data for {args.wallet}...")
    traded_count = fetch_traded_count(args.wallet)
    closed = fetch_closed_positions(args.wallet)
    open_pos = fetch_open_positions(args.wallet)
    redeemable = [p for p in open_pos if p.get("redeemable")]
    active = [p for p in open_pos if not p.get("redeemable")]

    print(f"  Total markets traded: {traded_count}")
    print(f"  Closed positions:     {len(closed)}")
    print(f"  Active positions:     {len(active)}")
    print(f"  Redeemable positions: {len(redeemable)}")

    if args.json:
        print(json.dumps({
            "wallet": args.wallet,
            "traded_count": traded_count,
            "closed": closed,
            "active": active,
            "redeemable": redeemable,
        }, indent=2, default=str))
        return

    if args.all:
        closed_subset = closed
        active_subset = active
        redeemable_subset = redeemable
        label = "All-category"
    else:
        closed_subset = [p for p in closed if is_weather(p)]
        active_subset = [p for p in active if is_weather(p)]
        redeemable_subset = [p for p in redeemable if is_weather(p)]
        label = "Weather"

    print(f"\n{label} breakdown:")
    print(f"  Closed:      {len(closed_subset)}/{len(closed)}")
    print(f"  Active:      {len(active_subset)}/{len(active)}")
    print(f"  Redeemable:  {len(redeemable_subset)}/{len(redeemable)}")

    # Combined resolved = closed + redeemable (redeemable = resolved but unclaimed)
    resolved = closed_subset + redeemable_subset
    total_resolved_pnl = sum(position_pnl(p) for p in resolved)
    active_exposure = sum(position_size(p) for p in active_subset)

    print(f"\n  Resolved total P&L:  ${total_resolved_pnl:,.2f}")
    print(f"  Active exposure:     ${active_exposure:,.2f}")

    analyze(closed_subset, "Closed")
    analyze(redeemable_subset, "Redeemable (resolved but unclaimed)")
    analyze(active_subset, "Active (still running)")


if __name__ == "__main__":
    main()
