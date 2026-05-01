#!/usr/bin/env python3
"""
Forecast Accuracy Journal

Logs each ensemble forecast to a local JSONL file so we can later compare
forecasts to actual resolved outcomes and measure per-model accuracy.

This does NOT affect trading — it's a passive observability tool. Run
`python scripts/forecast_history.py --report` to see accuracy stats.

Usage:
    from forecast_history import log_forecast, update_resolutions, print_report
"""

import json
import pathlib
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(line_buffering=True)

HISTORY_DIR = pathlib.Path(__file__).parent.parent / "data"
HISTORY_FILE = HISTORY_DIR / "forecast_history.jsonl"
HISTORY_DIR.mkdir(exist_ok=True)


def log_forecast(
    location: str,
    date_str: str,
    metric: str,
    forecast_temp: float,
    signal_strength: str,
    models_used: int,
    agreement_pct: float,
    spread: float,
    model_temps: dict | None = None,
    market_id: str | None = None,
) -> None:
    """
    Log one forecast observation. Idempotent on (location, target_date, metric):
    if an unresolved entry already exists for this key, it is updated in place
    with the latest forecast rather than appended. Resolved entries (actual_temp
    populated) are left untouched so accuracy stats remain accurate.
    """
    entry = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "location": location,
        "target_date": date_str,
        "metric": metric,
        "forecast_temp": forecast_temp,
        "signal_strength": signal_strength,
        "models_used": models_used,
        "agreement_pct": agreement_pct,
        "spread": spread,
        "model_temps": model_temps or {},
        "market_id": market_id,
        "actual_temp": None,
        "forecast_error": None,
    }
    try:
        entries = _load_entries()
    except Exception:
        entries = []
    # Find existing unresolved entry for the same (location, target_date, metric)
    target_idx = None
    for i, e in enumerate(entries):
        if (e.get("location") == location
            and e.get("target_date") == date_str
            and e.get("metric") == metric
            and e.get("actual_temp") is None):
            target_idx = i
            break
    if target_idx is not None:
        # Update in place — preserve any existing resolution fields
        existing = entries[target_idx]
        existing.update(entry)
        try:
            _save_entries(entries)
        except OSError:
            pass
        return
    # No existing unresolved entry — append fresh
    try:
        with HISTORY_FILE.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass


def _load_entries() -> list:
    if not HISTORY_FILE.exists():
        return []
    entries = []
    for line in HISTORY_FILE.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def _save_entries(entries: list) -> None:
    HISTORY_FILE.write_text("\n".join(json.dumps(e, default=str) for e in entries) + "\n")


def update_resolutions(fetch_actual_temp, save_every: int = 25) -> int:
    """
    Backfill actual_temp for forecasts whose target date has passed.

    `fetch_actual_temp(location, date_str, metric)` is a callback that returns
    the observed temperature or None. Typical implementation uses METAR or
    NOAA historical data.

    Duplicate forecast_history rows are common because we log repeated scans for
    the same (location, date, metric). Cache resolver results per key so one
    missing actual does not trigger the same slow network lookup over and over.
    Also checkpoint progress during long backfills so an interrupted run still
    preserves completed work.

    Returns number of entries updated.
    """
    entries = _load_entries()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    updated = 0
    cache: dict[tuple[str, str, str], float | None] = {}
    dirty = False
    for entry in entries:
        if entry.get("actual_temp") is not None:
            continue
        target = entry.get("target_date", "")
        if not target or target >= today:
            continue  # Date hasn't passed yet
        key = (entry["location"], target, entry["metric"])
        if key in cache:
            actual = cache[key]
        else:
            try:
                actual = fetch_actual_temp(*key)
            except Exception:
                actual = None
            cache[key] = actual
        if actual is None:
            continue
        forecast = entry.get("forecast_temp")
        entry["actual_temp"] = actual
        entry["forecast_error"] = round(actual - forecast, 2) if forecast is not None else None
        entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
        updated += 1
        dirty = True
        if save_every and updated % save_every == 0:
            _save_entries(entries)
    if dirty:
        _save_entries(entries)
    return updated


def get_accuracy_stats() -> dict:
    """Compute per-signal accuracy stats from resolved forecasts."""
    entries = [e for e in _load_entries() if e.get("actual_temp") is not None]
    if not entries:
        return {"resolved_count": 0}

    errors = [e["forecast_error"] for e in entries if e.get("forecast_error") is not None]
    abs_errors = [abs(err) for err in errors]

    by_signal = {}
    for entry in entries:
        sig = entry.get("signal_strength", "unknown")
        err = entry.get("forecast_error")
        if err is None:
            continue
        by_signal.setdefault(sig, []).append(abs(err))

    return {
        "resolved_count": len(entries),
        "mean_abs_error": round(sum(abs_errors) / len(abs_errors), 2) if abs_errors else None,
        "max_abs_error": round(max(abs_errors), 2) if abs_errors else None,
        "bias": round(sum(errors) / len(errors), 2) if errors else None,
        "by_signal": {
            sig: {
                "count": len(errs),
                "mean_abs_error": round(sum(errs) / len(errs), 2),
            }
            for sig, errs in by_signal.items()
        },
    }


def print_report() -> None:
    stats = get_accuracy_stats()
    print("\n📈 Forecast Accuracy Report")
    print("=" * 50)
    if stats["resolved_count"] == 0:
        print("  No resolved forecasts yet.")
        return
    print(f"  Resolved forecasts: {stats['resolved_count']}")
    print(f"  Mean abs error:     {stats['mean_abs_error']}°")
    print(f"  Max abs error:      {stats['max_abs_error']}°")
    print(f"  Bias (actual-fcst): {stats['bias']}°")
    by_sig = stats.get("by_signal", {})
    if by_sig:
        print("\n  By signal strength:")
        for sig, data in sorted(by_sig.items(), key=lambda kv: -kv[1]["count"]):
            print(f"    {sig:14s}  n={data['count']:4d}  MAE={data['mean_abs_error']}°")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Forecast accuracy tracker")
    parser.add_argument("--report", action="store_true", help="Print accuracy stats")
    args = parser.parse_args()
    if args.report:
        print_report()
    else:
        print_report()
