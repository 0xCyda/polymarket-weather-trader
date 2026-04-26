#!/usr/bin/env python3
"""
Backfill resolved skips from skip_events.jsonl against Open-Meteo archive.
Filters to STRONG signals only. Sizes by city tier (EASY 3% / MEDIUM 2% / HARD 1%).

Usage: analyze_skip_backfill.py <target_date YYYY-MM-DD>
"""
import json
import os
import sys
import time
import requests
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paper_journal import fetch_historical_temp, _parse_bucket_range

TARGET_DATE = sys.argv[1]
ALLOW_ALL_SIGNALS = "--all-signals" in sys.argv
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

# Retry config
INITIAL_DELAY = 1.0    # seconds before first retry
MAX_RETRIES = 4
MAX_DELAY = 32.0       # cap on backoff
RATE_LIMIT_CODES = {429, 500, 502, 503, 504}
CALL_DELAY = 0.25      # seconds between API calls (avoid rate limits)

# Cache for resolved questions (market_id -> question)
_question_cache = {}

def city_tier(location):
    return CITY_DIFFICULTY.get(location.upper(), "medium")

def stake_for_city(location):
    tier = city_tier(location)
    return PAPER_BALANCE * RISK_PCT[tier]

def fetch_question_with_retry(mid, attempt=0):
    """
    Fetch question from Simmer API with exponential backoff retry.
    Caches results to avoid duplicate calls.
    """
    if mid in _question_cache:
        return _question_cache[mid]

    url = f"https://api.simmer.markets/api/sdk/context/{mid}"
    headers = {"Authorization": f"Bearer {os.environ.get('SIMMER_API_KEY', '')}"}

    delay = INITIAL_DELAY * (2 ** attempt)

    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            question = r.json().get("market", {}).get("question", "")
            _question_cache[mid] = question
            return question
        elif r.status_code in RATE_LIMIT_CODES and attempt < MAX_RETRIES:
            print(f"  [rate limit] attempt {attempt+1}/{MAX_RETRIES}, backing off {delay:.1f}s...")
            time.sleep(delay)
            return fetch_question_with_retry(mid, attempt + 1)
        else:
            # 404, 401, etc. — don't retry
            _question_cache[mid] = None
            return None
    except requests.exceptions.Timeout:
        if attempt < MAX_RETRIES:
            print(f"  [timeout] attempt {attempt+1}/{MAX_RETRIES}, backing off {delay:.1f}s...")
            time.sleep(delay)
            return fetch_question_with_retry(mid, attempt + 1)
        _question_cache[mid] = None
        return None
    except Exception as e:
        _question_cache[mid] = None
        return None


def fetch_question(mid):
    """Thin wrapper with rate-limit delay baked in."""
    time.sleep(CALL_DELAY)
    return fetch_question_with_retry(mid)


# Load all skips for target date, filter to STRONG signals only
skips = []
with open(SKIP_LOG) as f:
    for line in f:
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("date") == TARGET_DATE:
            if not ALLOW_ALL_SIGNALS and e.get("signal_strength") != "strong":
                continue
            skips.append(e)

print(f"Strong-signal skips targeting {TARGET_DATE}: {len(skips)}")

per_reason = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
resolved_ct = 0
errors = 0
skipped_missing_bucket = 0

# Deduplicate by market_id (same bucket can appear multiple times from multiple scans)
seen_mids = set()

for s in skips:
    reason = (s.get("reason") or "?").split(":")[0]
    mid = s.get("market_id")
    loc = s.get("location", "")
    metric = s.get("metric", "high")
    price = s.get("price")
    if not mid or price is None:
        errors += 1
        continue

    # Skip duplicates (same market scanned multiple times)
    if mid in seen_mids:
        continue
    seen_mids.add(mid)

    q = fetch_question(mid)
    if not q:
        errors += 1
        continue

    rng = _parse_bucket_range(q)
    if not rng:
        errors += 1
        skipped_missing_bucket += 1
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
lines.append(f"- Strong skips: {len(skips)} | Deduplicated: {len(seen_mids)}")
lines.append(f"- Resolved via Open-Meteo: {resolved_ct} | Errors: {errors} | No bucket parsed: {skipped_missing_bucket}")
if resolved_ct < 40:
    lines.append(f"- Sample small (<40 resolved). Treat directionally only.")
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
