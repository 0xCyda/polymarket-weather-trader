#!/usr/bin/env python3
"""
Backfill `actual_temp` on every forecast_history.jsonl entry whose target
date has passed. Uses Open-Meteo historical archive via paper_journal's
fetch_historical_temp helper.

Without this, the per-model accuracy report (analytics.py --model-report)
has nothing to compute against — it needs both forecast_temp and actual_temp
on the same row.

Run manually any time:
    python scripts/backfill_forecast_actuals.py

Or wire to cron once a day:
    0 1 * * *  python /path/to/scripts/backfill_forecast_actuals.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from forecast_history import update_resolutions
from paper_journal import fetch_historical_temp, _fetch_archive_actual_temp


def _resolver(location: str, date_str: str, metric: str) -> float | None:
    """forecast_history's update_resolutions callback. Returns °F."""
    actual = fetch_historical_temp(location, date_str, metric, unit="F")
    if actual is not None:
        return actual
    return _fetch_archive_actual_temp(location, date_str, metric, unit="F")


def main() -> int:
    n = update_resolutions(_resolver)
    print(f"Backfilled {n} forecast row(s) with actual_temp.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
