#!/usr/bin/env python3
"""
Per-city forecast bias audit.

For each (location, target_date, metric) in forecast_history.jsonl whose date is
in the past, look up the resolved YES bucket from Polymarket Gamma (local cache
+ live fallback) and compute (forecast - actual) in °C. Mean over a city is the
suggested LOCATION_BIAS_C adjustment to APPLY (subtract from raw forecast).

Output: per-city mean bias, sample count, std, and current LOCATION_BIAS_C value.
"""
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from statistics import mean, pstdev

import requests

sys.path.insert(0, "/home/brandon/projects/polymarket-weather-trader/scripts")
from paper_journal import (  # noqa: E402
    _MONTH_NAMES, _location_to_slug_cities, _parse_outcome_prices,
)

FH = "/home/brandon/projects/polymarket-weather-trader/data/forecast_history.jsonl"
EVENTS_CACHE = "/home/brandon/projects/polymarket-weather-trader/data/polymarket_events.jsonl"
GAMMA = "https://gamma-api.polymarket.com/events"
TODAY = date.today()


def parse_bucket_to_center_f(label: str) -> float | None:
    """Bucket label → center-of-bucket temp in °F. For open-ended buckets (e.g.
    '21°C or higher'), use the threshold itself (best available estimate)."""
    if not label:
        return None
    s = label.strip()
    unit = "C" if re.search(r"°?\s*C\b", s, re.IGNORECASE) else "F"
    to_f = lambda v: v * 9 / 5 + 32 if unit == "C" else v
    m = re.match(r"(-?\d+(?:\.\d+)?)\s*°?[FC]?\s*(?:-|–|to)\s*(-?\d+(?:\.\d+)?)", s, re.IGNORECASE)
    if m:
        return to_f((float(m.group(1)) + float(m.group(2))) / 2)
    m = re.match(r"(-?\d+(?:\.\d+)?)\s*°?[FC]?\s*(?:or\s+)?(?:below|less|lower)", s, re.IGNORECASE)
    if m:
        return to_f(float(m.group(1)) - 0.5)
    m = re.match(r"(-?\d+(?:\.\d+)?)\s*°?[FC]?\s*(?:or\s+)?(?:above|higher|more)", s, re.IGNORECASE)
    if m:
        return to_f(float(m.group(1)) + 0.5)
    m = re.match(r"(-?\d+(?:\.\d+)?)", s)
    if m:
        return to_f(float(m.group(1)))
    return None


# Index local Polymarket events by slug for cheap lookups
SLUG_INDEX: dict[str, dict] = {}
with open(EVENTS_CACHE) as f:
    for line in f:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        slug = e.get("slug")
        if slug:
            SLUG_INDEX[slug] = e


def yes_bucket_for_event(e: dict) -> str | None:
    for m in e.get("markets", []):
        prices = _parse_outcome_prices(m.get("outcomePrices"))
        if prices and len(prices) >= 2:
            try:
                if float(prices[0]) >= 0.99:
                    return m.get("groupItemTitle")
            except (TypeError, ValueError):
                continue
    return None


def fetch_event_live(location: str, date_str: str, metric: str) -> dict | None:
    year, mo, day = date_str.split("-")
    month = _MONTH_NAMES[int(mo) - 1]
    prefix = "highest" if metric == "high" else "lowest"
    for slug_city in _location_to_slug_cities(location):
        slug = f"{prefix}-temperature-in-{slug_city}-on-{month}-{int(day)}-{year}"
        # local cache first
        if slug in SLUG_INDEX:
            return SLUG_INDEX[slug]
        try:
            r = requests.get(GAMMA, params={"slug": slug}, timeout=12)
            if r.status_code == 200:
                evs = r.json()
                if evs:
                    SLUG_INDEX[slug] = evs[0]
                    return evs[0]
        except requests.RequestException:
            pass
        time.sleep(0.1)
    return None


# Load forecasts, group by city, take latest per (loc, date, metric)
by_event: dict[tuple, dict] = {}
with open(FH) as f:
    for line in f:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            d = datetime.strptime(e.get("target_date", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d > TODAY:
            continue
        if e.get("forecast_temp") is None:
            continue
        key = (e["location"], e["target_date"], e.get("metric", "high"))
        prev = by_event.get(key)
        if prev is None or (e.get("logged_at", "") > prev.get("logged_at", "")):
            by_event[key] = e

print(f"Resolved-date forecasts to audit: {len(by_event)}")
print()

# Gather (forecast, actual_center) per city
deltas_c: dict[str, list[tuple[float, str]]] = defaultdict(list)  # city → [(delta_c, date)]
no_event = 0
no_yes = 0
no_parse = 0

for (loc, dstr, metric), e in by_event.items():
    ev = fetch_event_live(loc, dstr, metric)
    if not ev:
        no_event += 1
        continue
    yes_label = yes_bucket_for_event(ev)
    if not yes_label:
        no_yes += 1
        continue
    actual_f = parse_bucket_to_center_f(yes_label)
    if actual_f is None:
        no_parse += 1
        continue
    fc_f = float(e["forecast_temp"])
    delta_c = (fc_f - actual_f) * 5 / 9  # forecast minus actual, in °C
    deltas_c[loc].append((delta_c, dstr))

print(f"Skipped — no_event: {no_event}, no_yes: {no_yes}, no_parse: {no_parse}")
print()

# Current LOCATION_BIAS_C
CURRENT_BIAS = {"Hong Kong": 0.8, "Shenzhen": 1.0}

# Output
print("=" * 80)
print(f"{'City':<16}{'n':>4} {'mean Δ°C':>10} {'std':>7} {'min':>6} {'max':>6}  current → suggested")
print("-" * 80)
rows = []
for city in sorted(deltas_c, key=lambda k: -abs(mean(d[0] for d in deltas_c[k]))):
    samples = deltas_c[city]
    if len(samples) < 2:
        continue
    vals = [s[0] for s in samples]
    m = mean(vals)
    sd = pstdev(vals) if len(vals) > 1 else 0
    cur = CURRENT_BIAS.get(city, 0.0)
    # Recommended bias: NEGATIVE of mean delta, since bot ADDS bias to raw forecast.
    # If forecast - actual = +5°C, raw forecast is too high → subtract 5°C → bias = -5°C.
    suggested = round(-m, 1)
    flag = ""
    if abs(m) >= 3:
        flag = "  ←← LARGE"
    elif abs(m) >= 1.5:
        flag = "  ← notable"
    rows.append((city, len(vals), m, sd, min(vals), max(vals), cur, suggested, flag))

rows.sort(key=lambda r: -abs(r[2]))
for r in rows:
    city, n, m, sd, lo, hi, cur, sug, flag = r
    print(f"{city:<16}{n:>4} {m:>+10.2f} {sd:>7.2f} {lo:>+6.1f} {hi:>+6.1f}  "
          f"{cur:+5.1f} → {sug:+5.1f}{flag}")

print()
print("Notes:")
print("- Δ°C = forecast − actual (positive = forecast too warm).")
print("- 'suggested' is the LOCATION_BIAS_C value to APPLY (bot adds this to raw forecast).")
print("- Open-ended buckets (e.g. '21°C or higher') use threshold ±0.5°C as actual proxy.")
print("- Apply only to cities with n ≥ 5 and |Δ| ≥ 1.5°C unless there is a known reason.")
