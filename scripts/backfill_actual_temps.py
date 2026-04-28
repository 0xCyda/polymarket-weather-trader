#!/usr/bin/env python3
"""Backfill actual_temp into paper_trades.jsonl from Polymarket/Gamma resolution.

The actual temperature is derived from which Polymarket bucket resolved YES.
Pass --force to overwrite existing actual_temp values (e.g. to correct values
that were previously sourced from a different provider).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))
from paper_journal import fetch_historical_temp, _historical_fallback_settlement


def _resolve_actual_temp(trade: dict) -> float | None:
    """Best-effort actual temp lookup for a resolved trade."""
    loc_name = trade.get("location", "")
    date_str = trade.get("target_date") or (trade.get("resolution_date", "") or "")[:10]
    if not loc_name or not date_str:
        return None
    metric = trade.get("metric", "high")
    actual = fetch_historical_temp(loc_name, date_str, metric, unit="F")
    if actual is not None:
        return actual
    fb = _historical_fallback_settlement(trade, force=True)
    if fb:
        return fb.get("actual_temp")
    return None

PAPER_TRADES = _BASE.parent / "data" / "paper_trades.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing actual_temp values instead of skipping them",
    )
    args = parser.parse_args()

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
    unchanged = 0
    no_resolution = 0
    for t in trades:
        if t.get("status") != "resolved":
            continue
        prev = t.get("actual_temp")
        if prev is not None and not args.force:
            skipped += 1
            continue
        loc_name = t.get("location", "")
        date_str = t.get("target_date") or (t.get("resolution_date", "") or "")[:10]
        if not loc_name or not date_str:
            continue
        actual = _resolve_actual_temp(t)
        if actual is None:
            no_resolution += 1
            print(f"  [no resolution] {loc_name} {date_str}")
            continue
        if prev == actual:
            unchanged += 1
            continue
        t["actual_temp"] = actual
        updated += 1
        prev_str = f"{prev}°F" if prev is not None else "—"
        print(f"  {loc_name} {date_str}: {prev_str} -> {actual}°F (forecast={t.get('forecast_temp')}°F)")

    if updated:
        with PAPER_TRADES.open("w", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t) + "\n")
    print(
        f"\nBackfill complete. Updated={updated}, unchanged={unchanged}, "
        f"skipped(had-value)={skipped}, no-resolution={no_resolution}."
    )


if __name__ == "__main__":
    main()
