#!/usr/bin/env python3
"""
Backfill resolved skips from skip_events.jsonl against Open-Meteo archive.
Filters to STRONG signals only. Sizes by city tier (EASY 3% / MEDIUM 2% / HARD 1%).

Usage: analyze_skip_backfill.py <target_date YYYY-MM-DD>
"""
import json
import os
import sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paper_journal import fetch_historical_temp, _parse_bucket_range, _load_trades
import requests

TARGET_DATE = sys.argv[1]
SKIP_LOG = "/home/brandon/projects/polymarket-weather-trader/scripts/data/skip_events.jsonl"
PAPER_BALANCE = 10532.23  # current paper balance

# City difficulty tiers
RISK_PCT = {"easy": 0.03, "medium": 0.02, "hard": 0.01}
CITY_DIFFICULTY = {
    "TEL AVIV": "easy", "WARSAW": "easy", "SAN FRANCISCO": "easy",
    "LOS ANGELES": "easy", "MILAN": "easy", "CHENGDU": "easy",
    "HOUSTON": "easy", "MUNICH": "easy",
    "TOKYO": "hard", "SHANGHAI": "hard", "BEIJING": "hard", "WUHAN": "hard",
}

def city_tier(location):
    return CITY_DIFFICULTY.get(location.upper(), "medium")

def stake_for_city(location):
    tier = city_tier(location)
    return PAPER_BALANCE * RISK_PCT[tier]

def fetch_question(mid):
    KEY = os.environ.get("SIMMER_API_KEY", "")
    try:
        r = requests.get(
            f"https://api.simmer.markets/api/sdk/context/{mid}",
            headers={"Authorization": f"Bearer {KEY}"}, timeout=10,
        )
        if r.status_code != 200:
            return None
        return r.json().get("market", {}).get("question", "")
    except Exception:
        return None

# Load all skips for target date, filter to STRONG signals only
skips = []
with open(SKIP_LOG) as f:
    for line in f:
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("date") == TARGET_DATE:
            # STRONG signal filter
            if e.get("signal_strength") != "strong":
                continue
            skips.append(e)

print(f"Strong-signal skips targeting {TARGET_DATE}: {len(skips)}")

per_reason = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
resolved_ct = 0
errors = 0

for s in skips:
    reason = (s.get("reason") or "?").split(":")[0]
    mid = s.get("market_id")
    loc = s.get("location", "")
    metric = s.get("metric", "high")
    price = s.get("price")
    if not mid or price is None:
        errors += 1
        continue
    q = fetch_question(mid)
    if not q:
        errors += 1
        continue
    rng = _parse_bucket_range(q)
    if not rng:
        errors += 1
        continue
    lo_f, hi_f, _ = rng
    actual = fetch_historical_temp(loc, TARGET_DATE, metric, unit="F")
    if actual is None:
        errors += 1
        continue
    resolved_ct += 1
    won = lo_f <= actual <= hi_f
    stake = stake_for_city(loc)
    shares = stake / price if price > 0 else 0
    settle = 1.0 if won else 0.0
    pnl = (settle - price) * shares
    tier = city_tier(loc)
    per_reason[reason]["n"] += 1
    if won:
        per_reason[reason]["wins"] += 1
    per_reason[reason]["pnl"] += pnl
    print(f"  {loc} ({tier}) | stake=${stake:.0f} | price={price:.3f} | actual={actual}°F | {'WIN' if won else 'LOSS'} | pnl=${pnl:+.2f}")

total_pnl = sum(v["pnl"] for v in per_reason.values())
total_n = sum(v["n"] for v in per_reason.values())
total_w = sum(v["wins"] for v in per_reason.values())

lines = []
lines.append(f"**Skip Backfill (STRONG only) — {TARGET_DATE} resolution**")
lines.append("")
lines.append(f"- Strong skips targeting {TARGET_DATE}: {len(skips)}")
lines.append(f"- Resolved via Open-Meteo: {resolved_ct} (errors: {errors})")
if resolved_ct < 40:
    lines.append(f"- ⚠️ Sample small (<40 resolved). Treat directionally only.")
wr_all = 100 * total_w / total_n if total_n else 0
lines.append(f"- Win rate if we'd bought all: {total_w}/{total_n} ({wr_all:.1f}%)")
lines.append(f"- Net implied P&L (city-tier sizing): ${total_pnl:+,.2f}")
lines.append("")
lines.append("**By skip reason:**")
for reason, v in sorted(per_reason.items(), key=lambda kv: -kv[1]["pnl"]):
    wr = 100 * v["wins"] / v["n"] if v["n"] else 0
    avg_pnl = v["pnl"] / v["n"] if v["n"] else 0
    lines.append(f"- `{reason}`: n={v['n']}, wr={wr:.0f}%, pnl=${v['pnl']:+,.2f}, avg=${avg_pnl:+.2f}/trade")

print("\n" + "\n".join(lines))
