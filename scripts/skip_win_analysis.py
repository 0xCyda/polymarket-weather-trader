#!/usr/bin/env python3
"""
Skip-trade win analysis (Gamma-only, no Simmer).
For each (location, date, metric) where the bot generated a forecast,
fetch the Polymarket event from Gamma, find the resolved YES bucket,
and determine whether the bot's forecast pointed to that bucket.

Then cross-reference with skip_events.jsonl to find:
  - signals that were skipped despite being CORRECT (would have won)
  - signals that were skipped and were WRONG (skip was right)

Pure Gamma API. Uses local forecast_history.jsonl + skip_events.jsonl.
"""
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime

import requests

sys.path.insert(0, "/home/brandon/projects/polymarket-weather-trader/scripts")
from paper_journal import (  # noqa: E402
    _MONTH_NAMES, _location_to_slug_cities, _parse_outcome_prices,
    _bucket_label_to_temp_f,
)

FORECAST_LOG = "/home/brandon/projects/polymarket-weather-trader/data/forecast_history.jsonl"
SKIP_LOG = "/home/brandon/projects/polymarket-weather-trader/scripts/data/skip_events.jsonl"
PAPER = "/home/brandon/projects/polymarket-weather-trader/data/paper_trades.jsonl"
DATES = {"2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-01", "2026-05-02"}
GAMMA = "https://gamma-api.polymarket.com/events"
TODAY = date.today()


def fetch_event(location: str, date_str: str, metric: str) -> dict | None:
    year, mo, day = date_str.split("-")
    month = _MONTH_NAMES[int(mo) - 1]
    prefix = "highest" if metric == "high" else "lowest"
    for slug_city in _location_to_slug_cities(location):
        slug = f"{prefix}-temperature-in-{slug_city}-on-{month}-{int(day)}-{year}"
        try:
            r = requests.get(GAMMA, params={"slug": slug}, timeout=15)
            if r.status_code == 200:
                evs = r.json()
                if evs:
                    return evs[0]
        except requests.RequestException:
            pass
        time.sleep(0.1)
    return None


def parse_bucket_to_range_f(label: str) -> tuple[float, float] | None:
    """Bucket label → (lo_f, hi_f). Returns None if not parseable."""
    if not label:
        return None
    s = label.strip()
    unit = "C" if re.search(r"°?\s*C\b", s, re.IGNORECASE) else "F"
    to_f = lambda v: v * 9 / 5 + 32 if unit == "C" else v
    m = re.match(r"(-?\d+(?:\.\d+)?)\s*°?[FC]?\s*(?:-|–|to)\s*(-?\d+(?:\.\d+)?)", s, re.IGNORECASE)
    if m:
        return (to_f(float(m.group(1))), to_f(float(m.group(2))))
    m = re.match(r"(-?\d+(?:\.\d+)?)\s*°?[FC]?\s*(?:or\s+)?(?:below|less|lower)", s, re.IGNORECASE)
    if m:
        return (-1e9, to_f(float(m.group(1))))
    m = re.match(r"(-?\d+(?:\.\d+)?)\s*°?[FC]?\s*(?:or\s+)?(?:above|higher|more)", s, re.IGNORECASE)
    if m:
        return (to_f(float(m.group(1))), 1e9)
    m = re.match(r"(-?\d+(?:\.\d+)?)", s)
    if m:
        v = to_f(float(m.group(1)))
        return (v, v)
    return None


def event_yes_bucket(event: dict) -> tuple[str | None, list[dict]]:
    markets = event.get("markets", [])
    yes = None
    for m in markets:
        prices = _parse_outcome_prices(m.get("outcomePrices"))
        if prices and len(prices) >= 2:
            try:
                if float(prices[0]) >= 0.99:
                    yes = m.get("groupItemTitle")
                    break
            except (TypeError, ValueError):
                continue
    return yes, markets


def load_forecasts() -> list[dict]:
    rows = []
    with open(FORECAST_LOG) as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("target_date") in DATES:
                rows.append(e)
    return rows


def load_skips_by_event() -> dict[tuple, list[dict]]:
    out = defaultdict(list)
    with open(SKIP_LOG) as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("date") not in DATES:
                continue
            key = (e["location"], e["date"], e.get("metric", "high"))
            out[key].append(e)
    return out


def load_traded_events() -> set[tuple]:
    out = set()
    try:
        with open(PAPER) as f:
            for line in f:
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if t.get("target_date") in DATES:
                    out.add((t["location"], t["target_date"], t.get("metric", "high")))
    except FileNotFoundError:
        pass
    return out


def main():
    forecasts = load_forecasts()
    skips_by_evt = load_skips_by_event()
    traded_evts = load_traded_events()

    # Group forecasts by event — keep latest forecast per event
    by_event: dict[tuple, dict] = {}
    for f in forecasts:
        key = (f["location"], f["target_date"], f.get("metric", "high"))
        prev = by_event.get(key)
        if prev is None or f.get("logged_at", "") > prev.get("logged_at", ""):
            by_event[key] = f

    print(f"Unique events with forecasts (24-26 Apr): {len(by_event)}")
    print(f"Events also seen in skip log: "
          f"{len(set(by_event) & set(skips_by_evt))}")
    print(f"Events the bot traded (excluded): {len(set(by_event) & traded_evts)}")
    print()

    rows = []
    for key, fc in sorted(by_event.items()):
        loc, date_str, metric = key
        if key in traded_evts:
            continue
        # Skip future events
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d > TODAY:
            rows.append({"key": key, "fc": fc, "status": "future"})
            continue

        ev = fetch_event(loc, date_str, metric)
        if not ev:
            rows.append({"key": key, "fc": fc, "status": "no_event"})
            continue
        yes_label, markets = event_yes_bucket(ev)
        if not yes_label:
            rows.append({"key": key, "fc": fc, "status": "unresolved"})
            continue

        yes_range = parse_bucket_to_range_f(yes_label)
        ftemp = fc.get("forecast_temp")
        if yes_range is None or ftemp is None:
            rows.append({"key": key, "fc": fc, "status": "parse_fail",
                         "yes_label": yes_label})
            continue

        lo, hi = yes_range
        # Forecast "correct" if it lands in the YES bucket range (with 0.5° wiggle)
        in_bucket = (lo - 0.5) <= ftemp <= (hi + 0.5)

        # Compute distance from forecast to YES bucket midpoint for context
        if lo > -1e8 and hi < 1e8:
            mid = (lo + hi) / 2
        elif lo <= -1e8:
            mid = hi
        else:
            mid = lo
        miss = ftemp - mid

        # Was this event skipped? Get the skip reasons
        skip_reasons = sorted({
            (s.get("reason") or "?").split(":")[0]
            for s in skips_by_evt.get(key, [])
        })

        rows.append({
            "key": key, "fc": fc, "status": "resolved",
            "yes_label": yes_label,
            "yes_range": yes_range,
            "in_bucket": in_bucket,
            "miss_f": miss,
            "skip_reasons": skip_reasons,
        })

    # Print results
    print("=" * 115)
    print(f"{'Loc':<13}{'Date':<12}{'Sig':<10}{'Fcst°F':>7}  "
          f"{'YES bucket':<18}{'Miss°F':>7}  {'Hit':<4}  Skip reasons")
    print("-" * 115)

    correct_skipped = []
    correct_skipped_actionable = []  # excluded "no_bucket_*" since no bucket was matched
    n_resolved = 0
    n_in_bucket = 0
    by_reason_correct = defaultdict(int)
    by_reason_total = defaultdict(int)

    for r in rows:
        if r["status"] != "resolved":
            continue
        n_resolved += 1
        loc, d, m = r["key"]
        fc = r["fc"]
        sig = fc.get("signal_strength", "?")
        ftemp = fc.get("forecast_temp")
        miss = r["miss_f"]
        in_b = r["in_bucket"]
        if in_b:
            n_in_bucket += 1
        reasons = r["skip_reasons"]

        if in_b and reasons:
            correct_skipped.append(r)
            actionable = [
                rs for rs in reasons
                if rs not in {"no_bucket_low_edge", "no_bucket_price_extreme",
                              "no_bucket_parseable", "stale_event"}
            ]
            if actionable:
                correct_skipped_actionable.append({**r, "actionable": actionable})

        for rs in reasons:
            by_reason_total[rs] += 1
            if in_b:
                by_reason_correct[rs] += 1

        ftemp_s = f"{ftemp:.1f}" if ftemp is not None else "-"
        miss_s = f"{miss:+.1f}" if miss is not None else "-"
        hit_s = "WIN" if in_b else "miss"
        print(f"{loc:<13}{d:<12}{sig:<10}{ftemp_s:>7}  "
              f"{r['yes_label'][:17]:<18}{miss_s:>7}  {hit_s:<4}  {','.join(reasons)[:50]}")

    print()
    print("=" * 115)
    print(f"SUMMARY (24-26 Apr 2026, ex-traded events)")
    print("=" * 115)
    print(f"Resolved events: {n_resolved}")
    if n_resolved:
        print(f"Forecast hit YES bucket: {n_in_bucket} ({100*n_in_bucket/n_resolved:.1f}%)")
    print(f"Correct forecasts that were ALSO skipped: {len(correct_skipped)}")
    print(f"  ...with at least one ACTIONABLE skip reason "
          f"(not no_bucket_*/stale): {len(correct_skipped_actionable)}")
    print()
    print("Win rate of forecasts grouped by skip reason (would the bucket have won if entered?):")
    for rs in sorted(by_reason_total, key=lambda k: -by_reason_total[k]):
        n = by_reason_total[rs]
        w = by_reason_correct[rs]
        print(f"  {rs:<28} {w:>3}/{n:<4}  ({100*w/n:>5.1f}%)")

    print()
    print("=" * 115)
    print("CORRECT FORECASTS WE SKIPPED — likely actionable (could have won)")
    print("=" * 115)
    print(f"{'Loc':<13}{'Date':<12}{'Sig':<10}{'Fcst':>7}  {'YES bucket':<18}  Skip reasons")
    print("-" * 115)
    for r in correct_skipped_actionable:
        loc, d, m = r["key"]
        fc = r["fc"]
        sig = fc.get("signal_strength", "?")
        print(f"{loc:<13}{d:<12}{sig:<10}{fc.get('forecast_temp'):>7.1f}  "
              f"{r['yes_label'][:17]:<18}  {','.join(r['actionable'])}")

    print()
    print("Notes:")
    print("- 'Hit YES bucket' = bot's weighted forecast landed in the resolved bucket (±0.5°F).")
    print("- Skip reasons starting with 'no_bucket_*' are usually correct skips: the bot did not")
    print("  pick a bucket because of pricing/edge, not because it was wrong on the forecast.")


if __name__ == "__main__":
    main()
