#!/usr/bin/env python3
"""
Polymarket Trader Analysis — reverse-engineer any wallet's weather strategy.

Pulls closed positions, active positions, and redeemable positions from the
public Polymarket Data API, filters for weather markets, and reports patterns:
  - Total P&L, win rate, trade count
  - Winner vs loser avg size (critical: high win rate ≠ profit)
  - City breakdown with normalized names (NYC + New York merged)
  - Bucket type extracted from TITLE (not outcome field)
  - Metric (high vs low temp)
  - Best / worst trades
  - Monthly seasonality

Usage:
    python scripts/polymarket_analyze.py 0x594edb9112f526fa6a80b8f858a6379c8a2c1c11
    python scripts/polymarket_analyze.py <wallet> --all       # all categories
    python scripts/polymarket_analyze.py <wallet> --json      # raw JSON
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

# Force UTF-8 output on Windows (cp1252 can't encode many chars used in market titles)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

WEATHER_KEYWORDS = ("temperature", "temp", "weather", "°f", "°c", "highest", "lowest")

# City aliases → canonical name. Longest aliases must be matched first so
# "new york city" beats "new york" which beats "nyc".
CITY_ALIASES = {
    "new york city": "NYC",
    "new york":      "NYC",
    "nyc":           "NYC",
    "san francisco": "San Francisco",
    "los angeles":   "Los Angeles",
    "las vegas":     "Las Vegas",
    "hong kong":     "Hong Kong",
    "tel aviv":      "Tel Aviv",
    "sao paulo":     "Sao Paulo",
    "são paulo":     "Sao Paulo",
    "buenos aires":  "Buenos Aires",
    "chicago":       "Chicago",
    "seattle":       "Seattle",
    "atlanta":       "Atlanta",
    "dallas":        "Dallas",
    "miami":         "Miami",
    "houston":       "Houston",
    "phoenix":       "Phoenix",
    "denver":        "Denver",
    "austin":        "Austin",
    "munich":        "Munich",
    "london":        "London",
    "tokyo":         "Tokyo",
    "seoul":         "Seoul",
    "ankara":        "Ankara",
    "lucknow":       "Lucknow",
    "wellington":    "Wellington",
    "toronto":       "Toronto",
    "paris":         "Paris",
    "milan":         "Milan",
    "warsaw":        "Warsaw",
    "singapore":     "Singapore",
    "shanghai":      "Shanghai",
    "beijing":       "Beijing",
    "shenzhen":      "Shenzhen",
    "chengdu":       "Chengdu",
    "chongqing":     "Chongqing",
    "wuhan":         "Wuhan",
}
# Pre-sort aliases by length so longer ones match first
_SORTED_ALIASES = sorted(CITY_ALIASES.keys(), key=len, reverse=True)


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
    data = _get(f"{DATA_API}/traded", params={"user": wallet})
    if data is None:
        return None
    if isinstance(data, dict):
        return data.get("traded") or data.get("count") or data.get("total")
    if isinstance(data, (int, float)):
        return int(data)
    return None


def fetch_closed_positions(wallet: str) -> list[dict]:
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
    data = _get(f"{DATA_API}/positions", params={"user": wallet})
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("positions", []) or []


def _title_blob(p: dict) -> str:
    """Join all text fields lowercased for keyword searches."""
    return " ".join(str(p.get(k, "")).lower() for k in
                    ("title", "eventSlug", "slug", "question", "outcome"))


def is_weather(p: dict) -> bool:
    blob = _title_blob(p)
    if not any(kw in blob for kw in WEATHER_KEYWORDS):
        return False
    return any(alias in blob for alias in _SORTED_ALIASES)


def extract_city(p: dict) -> str:
    """Return canonical city name. NYC and New York collapse to 'NYC'."""
    blob = _title_blob(p)
    for alias in _SORTED_ALIASES:
        if alias in blob:
            return CITY_ALIASES[alias]
    return "Unknown"


def extract_metric(p: dict) -> str:
    blob = _title_blob(p)
    if "lowest" in blob or "low temp" in blob:
        return "low"
    return "high"


def extract_bucket_type(p: dict) -> str:
    """
    Parse bucket style from the TITLE (outcome field is 'Yes'/'No' — useless).
    Returns: 'exact' | 'range' | 'threshold' | 'unknown'
    """
    title = str(p.get("title", "") or p.get("question", "")).lower()
    if re.search(r"or (higher|above|more|below|less)", title):
        return "threshold"
    if re.search(r"between\s+-?\d+\s*[-–to]\s*-?\d+", title):
        return "range"
    if re.search(r"-?\d+\s*[-–to]\s*-?\d+\s*°", title):
        return "range"
    if re.search(r"be\s+-?\d+\s*°", title) or re.search(r"be\s+between", title):
        # "be 28°C" or "be between" — bare single degree is exact
        if "between" in title:
            return "range"
        return "exact"
    return "unknown"


def position_pnl(p: dict) -> float:
    """realizedPnl for closed, realizedPnl + cashPnl for redeemable."""
    r = float(p.get("realizedPnl") or 0)
    c = float(p.get("cashPnl") or 0)
    return r + c if p.get("redeemable") else r


def position_size(p: dict) -> float:
    """Dollar cost basis — size of the entry."""
    # Order matters: totalBought is most reliable, initialValue next, etc.
    for k in ("totalBought", "initialValue", "size"):
        v = p.get(k)
        if v is not None:
            try:
                f = float(v)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                pass
    # Fall back to avgPrice × shares
    try:
        avg = float(p.get("avgPrice") or 0)
        sh = float(p.get("size") or p.get("shares") or 0)
        if avg > 0 and sh > 0:
            return avg * sh
    except (TypeError, ValueError):
        pass
    return 0.0


def extract_month(p: dict) -> str:
    """Best-effort extraction of target month from title."""
    title = str(p.get("title", "")).lower()
    months = ["january", "february", "march", "april", "may", "june",
              "july", "august", "september", "october", "november", "december"]
    for m in months:
        if m in title:
            return m.title()
    return "Unknown"


def analyze(positions: list[dict], label: str) -> None:
    if not positions:
        print(f"\n  No {label} weather positions.")
        return

    pnls = [position_pnl(p) for p in positions]
    sizes = [position_size(p) for p in positions]
    wins = [p for p in positions if position_pnl(p) > 0]
    losses = [p for p in positions if position_pnl(p) < 0]
    total_pnl = sum(pnls)
    resolved = len(wins) + len(losses)

    print(f"\n  {label.upper()} ({len(positions)} positions)")
    print(f"  Total P&L:      ${total_pnl:,.2f}")
    print(f"  Win rate:       {len(wins)}/{resolved} "
          f"({100*len(wins)/max(1,resolved):.1f}%)")

    # Winner vs loser sizes — CRITICAL: high win rate can still lose if
    # losing trades are larger than winning trades (the ColdMath problem).
    winner_sizes = [position_size(p) for p in wins if position_size(p) > 0]
    loser_sizes = [position_size(p) for p in losses if position_size(p) > 0]
    if winner_sizes and loser_sizes:
        avg_w = sum(winner_sizes) / len(winner_sizes)
        avg_l = sum(loser_sizes) / len(loser_sizes)
        ratio = avg_l / avg_w if avg_w > 0 else 0
        print(f"  Avg winner:     ${avg_w:,.2f}  (n={len(winner_sizes)})")
        print(f"  Avg loser:      ${avg_l:,.2f}  (n={len(loser_sizes)})")
        if ratio > 1.2:
            print(f"  [!] Losers {ratio:.1f}x larger than winners - sizing problem")
        elif ratio < 0.8:
            print(f"  [OK] Winners {1/ratio:.1f}x larger than losers - good sizing")

    valid_sizes = [s for s in sizes if s > 0]
    if valid_sizes:
        print(f"  Size range:     ${min(valid_sizes):,.0f} – ${max(valid_sizes):,.0f}")

    # By city (merged NYC/New York)
    by_city = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0, "l": 0,
                                   "wsize": [], "lsize": []})
    for p in positions:
        c = extract_city(p)
        pnl = position_pnl(p)
        sz = position_size(p)
        by_city[c]["n"] += 1
        by_city[c]["pnl"] += pnl
        if pnl > 0:
            by_city[c]["w"] += 1
            if sz > 0: by_city[c]["wsize"].append(sz)
        elif pnl < 0:
            by_city[c]["l"] += 1
            if sz > 0: by_city[c]["lsize"].append(sz)

    print(f"\n  By city (top 15 by volume):")
    print(f"  {'City':<18s} {'n':>5s}  {'Win%':>6s}  {'P&L':>12s}  {'W.size':>8s}  {'L.size':>8s}")
    print("  " + "-" * 66)
    for city in sorted(by_city, key=lambda c: -by_city[c]["n"])[:15]:
        d = by_city[city]
        res = d["w"] + d["l"]
        wr = d["w"] / max(1, res) * 100
        avg_w = sum(d["wsize"])/len(d["wsize"]) if d["wsize"] else 0
        avg_l = sum(d["lsize"])/len(d["lsize"]) if d["lsize"] else 0
        print(f"  {city:<16s} {d['n']:5d}  {wr:5.1f}%  ${d['pnl']:>11,.2f}  "
              f"${avg_w:>7,.0f}  ${avg_l:>7,.0f}")

    # City difficulty ranking (min 10 resolved, hardest first)
    ranked = []
    for city, d in by_city.items():
        if city == "Unknown":
            continue
        res = d["w"] + d["l"]
        if res < 10:
            continue
        wr = d["w"] / res * 100
        ranked.append((city, wr, d["n"], d["pnl"], res))

    if ranked:
        print(f"\n  City difficulty (win rate, min 10 resolved):")
        print(f"  {'City':<18s} {'n':>5s}  {'Win%':>6s}  {'P&L':>12s}  {'Tier'}")
        print("  " + "-" * 58)
        for city, wr, n, pnl, _ in sorted(ranked, key=lambda x: x[1]):
            if wr < 55:   tier = "HARD"
            elif wr >= 75: tier = "EASY"
            else:          tier = "MEDIUM"
            print(f"  {city:<16s} {n:5d}  {wr:5.1f}%  ${pnl:>11,.2f}  {tier}")

    # By metric (high vs low)
    hi = [p for p in positions if extract_metric(p) == "high"]
    lo = [p for p in positions if extract_metric(p) == "low"]
    print(f"\n  High temp:  {len(hi):>5d} positions, P&L ${sum(position_pnl(p) for p in hi):>12,.2f}")
    print(f"  Low temp:   {len(lo):>5d} positions, P&L ${sum(position_pnl(p) for p in lo):>12,.2f}")

    # By bucket type (from TITLE, not outcome)
    bucket_types = Counter(extract_bucket_type(p) for p in positions)
    print(f"\n  Bucket type preference (from title):")
    for bt, n in bucket_types.most_common():
        bt_pnl = sum(position_pnl(p) for p in positions if extract_bucket_type(p) == bt)
        pct = n / len(positions) * 100
        print(f"    {bt:<12s}  n={n:5d}  ({pct:4.1f}%)  P&L=${bt_pnl:>11,.2f}")

    # Monthly seasonality (top 5 months by volume)
    by_month = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0, "l": 0})
    for p in positions:
        m = extract_month(p)
        pnl = position_pnl(p)
        by_month[m]["n"] += 1
        by_month[m]["pnl"] += pnl
        if pnl > 0: by_month[m]["w"] += 1
        elif pnl < 0: by_month[m]["l"] += 1
    if any(m != "Unknown" and d["n"] >= 5 for m, d in by_month.items()):
        print(f"\n  By target month (top 5 by volume):")
        print(f"  {'Month':<12s} {'n':>5s}  {'Win%':>6s}  {'P&L':>12s}")
        print("  " + "-" * 42)
        for month in sorted(by_month, key=lambda m: -by_month[m]["n"])[:5]:
            if month == "Unknown":
                continue
            d = by_month[month]
            res = d["w"] + d["l"]
            wr = d["w"] / max(1, res) * 100
            print(f"  {month:<12s} {d['n']:5d}  {wr:5.1f}%  ${d['pnl']:>11,.2f}")

    # Best / worst
    if pnls:
        best = max(positions, key=position_pnl)
        worst = min(positions, key=position_pnl)
        print(f"\n  Best trade:  ${position_pnl(best):,.2f}")
        print(f"    {(best.get('title') or best.get('eventSlug') or '')[:80]}")
        print(f"    size: ${position_size(best):,.0f}  outcome: {best.get('outcome', '?')}")
        print(f"  Worst trade: ${position_pnl(worst):,.2f}")
        print(f"    {(worst.get('title') or worst.get('eventSlug') or '')[:80]}")
        print(f"    size: ${position_size(worst):,.0f}  outcome: {worst.get('outcome', '?')}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Reverse-engineer a Polymarket trader")
    parser.add_argument("wallet", help="0x-prefixed 40-hex wallet address")
    parser.add_argument("--all", action="store_true", help="All categories, not just weather")
    parser.add_argument("--json", action="store_true", help="Raw JSON dump, no analysis")
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
            "wallet": args.wallet, "traded_count": traded_count,
            "closed": closed, "active": active, "redeemable": redeemable,
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

    # Combined resolved = closed + redeemable
    resolved = closed_subset + redeemable_subset
    total_pnl = sum(position_pnl(p) for p in resolved)
    active_exposure = sum(position_size(p) for p in active_subset)
    print(f"\n  Resolved total P&L:  ${total_pnl:,.2f}")
    print(f"  Active exposure:     ${active_exposure:,.2f}")

    # Analyze ALL resolved positions together (closed + redeemable) — this is
    # what pros actually reason about ("all my resolved trades").
    if resolved:
        analyze(resolved, "All Resolved (closed + redeemable)")
    analyze(active_subset, "Active (still running)")


if __name__ == "__main__":
    main()
