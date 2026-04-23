#!/usr/bin/env python3
"""Backfill actual_temp into paper_trades.jsonl using TWC (Wunderground) data."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))
from paper_journal import fetch_historical_temp

PAPER_TRADES = _BASE.parent / "data" / "paper_trades.jsonl"


def main():
    if not PAPER_TRADES.exists():
        print(f"Not found: {PAPER_TRADES}", file=sys.stderr)
        sys.exit(1)

    trades = []
    for line in PAPER_TRADES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        trades.append(json.loads(line))

    updated = 0
    skipped = 0
    for t in trades:
        if t.get("status") != "resolved":
            continue
        if t.get("actual_temp") is not None:
            skipped += 1
            continue
        loc_name = t.get("location", "")
        date_str = t.get("target_date") or t.get("resolution_date", "")[:10]
        if not loc_name or not date_str:
            continue
        metric = t.get("metric", "high")
        actual = fetch_historical_temp(loc_name, date_str, metric, unit="F")
        if actual is not None:
            t["actual_temp"] = actual
            updated += 1
            print(f"  {loc_name} {date_str}: actual={actual}°F (forecast={t.get('forecast_temp')}°F)")

    if updated:
        with PAPER_TRADES.open("w", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t) + "\n")
    print(f"\nBackfilled {updated} trades. Already had actual_temp: {skipped}.")


if __name__ == "__main__":
    main()
