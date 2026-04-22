#!/usr/bin/env python3
"""
Analytics reports for Polymarket Weather Trader.

Reads resolved trades and skip events to produce:
  --model-report       Per-model accuracy decomposition
  --calibration        Confidence vs actual win rate
  --city-report        Win rate by city, day-of-week, month
  --skip-funnel        Why trades were skipped (filter analysis)
  --all                Run all reports

Usage:
    python scripts/analytics.py --all
    python scripts/analytics.py --model-report
"""

import json
import pathlib
import sys
from collections import defaultdict
from datetime import datetime

DATA_DIR = pathlib.Path(__file__).parent.parent / "data"
TRADES_FILE = DATA_DIR / "paper_trades.jsonl"
SKIP_FILE = DATA_DIR / "skip_events.jsonl"


def _load_jsonl(path: pathlib.Path) -> list:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def _resolved_trades() -> list:
    return [t for t in _load_jsonl(TRADES_FILE) if t.get("status") == "resolved"]


# =========================================================================
# #2  Per-model accuracy
# =========================================================================

def model_report() -> None:
    trades = _resolved_trades()
    if not trades:
        print("\n  No resolved trades yet.")
        return

    model_errors = defaultdict(list)
    for t in trades:
        actual = t.get("actual_temp")
        model_temps = t.get("model_temps")
        if actual is None or not model_temps:
            continue
        for model, temp in model_temps.items():
            if temp is not None:
                model_errors[model].append(actual - temp)

    if not model_errors:
        print("\n  No trades with both actual_temp and model_temps.")
        return

    print(f"\n{'Model':<22s} {'MAE':>6s}  {'Bias':>6s}  {'Worst':>6s}  {'n':>5s}")
    print("-" * 55)
    rows = []
    for model, errors in sorted(model_errors.items()):
        abs_errors = [abs(e) for e in errors]
        mae = sum(abs_errors) / len(abs_errors)
        bias = sum(errors) / len(errors)
        worst = max(abs_errors)
        rows.append((model, mae, bias, worst, len(errors)))

    for model, mae, bias, worst, n in sorted(rows, key=lambda r: r[1]):
        flag = "  !!!" if mae > 4 or abs(bias) > 2 else ""
        print(f"  {model:<20s} {mae:5.1f}°  {bias:+5.1f}°  {worst:5.1f}°  {n:5d}{flag}")


# =========================================================================
# #4  Calibration curve
# =========================================================================

def calibration_report() -> None:
    trades = _resolved_trades()
    if not trades:
        print("\n  No resolved trades yet.")
        return

    bins = defaultdict(lambda: {"wins": 0, "total": 0})
    for t in trades:
        conf = t.get("confidence")
        if conf is None:
            sig = t.get("signal_strength", "")
            conf = {"strong": 0.88, "moderate": 0.80, "weak": 0.68}.get(sig, 0.72)
        outcome = t.get("outcome", "")
        won = t.get("pnl", 0) > 0
        bucket_key = round(conf * 10) / 10  # bin to nearest 0.1
        bins[bucket_key]["total"] += 1
        if won:
            bins[bucket_key]["wins"] += 1

    if not bins:
        print("\n  No calibration data.")
        return

    print(f"\n{'Confidence':>12s}  {'n':>5s}  {'Win%':>6s}  {'Expected':>8s}  {'Status'}")
    print("-" * 55)
    for conf_bin in sorted(bins):
        data = bins[conf_bin]
        n = data["total"]
        win_pct = data["wins"] / n * 100 if n else 0
        expected = conf_bin * 100
        diff = win_pct - expected
        if abs(diff) > 15:
            status = "OVER" if diff > 0 else "UNDER"
        elif abs(diff) > 8:
            status = "~ok"
        else:
            status = "OK"
        print(f"  {conf_bin:>10.0%}  {n:5d}  {win_pct:5.1f}%  {expected:6.0f}%  {status}")


# =========================================================================
# #6  City / time stratification
# =========================================================================

def city_report() -> None:
    trades = _resolved_trades()
    if not trades:
        print("\n  No resolved trades yet.")
        return

    by_city = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "errors": []})
    by_dow = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    by_strategy = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})

    for t in trades:
        pnl = t.get("pnl", 0)
        won = pnl > 0
        city = t.get("location", "Unknown")
        strategy = t.get("strategy", "core")

        c = by_city[city]
        if won:
            c["wins"] += 1
        else:
            c["losses"] += 1
        c["pnl"] += pnl
        actual = t.get("actual_temp")
        forecast = t.get("forecast_temp")
        if actual is not None and forecast is not None:
            c["errors"].append(abs(actual - forecast))

        entered = t.get("entered_at", "")
        try:
            dt = datetime.fromisoformat(entered.replace("Z", "+00:00"))
            dow_name = dt.strftime("%A")
        except Exception:
            dow_name = "Unknown"
        d = by_dow[dow_name]
        if won:
            d["wins"] += 1
        else:
            d["losses"] += 1
        d["pnl"] += pnl

        s = by_strategy[strategy]
        if won:
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["pnl"] += pnl

    # --- By city (by volume) ---
    print(f"\n{'City':<18s} {'n':>4s}  {'Win%':>6s}  {'PnL':>9s}  {'MAE':>6s}")
    print("-" * 50)
    for city in sorted(by_city, key=lambda c: -(by_city[c]["wins"] + by_city[c]["losses"])):
        c = by_city[city]
        n = c["wins"] + c["losses"]
        wr = c["wins"] / n * 100 if n else 0
        mae = sum(c["errors"]) / len(c["errors"]) if c["errors"] else None
        mae_str = f"{mae:5.1f}°" if mae is not None else "    —"
        flag = "  !!!" if n >= 3 and wr < 40 else ""
        print(f"  {city:<16s} {n:4d}  {wr:5.1f}%  ${c['pnl']:>8.2f}  {mae_str}{flag}")

    # --- By difficulty (hardest to easiest, min 3 resolved) ---
    ranked = []
    for city, c in by_city.items():
        n = c["wins"] + c["losses"]
        if n < 3:
            continue
        wr = c["wins"] / n * 100
        mae = sum(c["errors"]) / len(c["errors"]) if c["errors"] else None
        ranked.append((city, wr, n, c["pnl"], mae))

    if ranked:
        print(f"\n  Ranked by difficulty (hardest first, min 3 trades):")
        print(f"  {'City':<18s} {'n':>4s}  {'Win%':>6s}  {'PnL':>9s}  {'MAE':>6s}")
        print("  " + "-" * 48)
        for city, wr, n, pnl, mae in sorted(ranked, key=lambda x: x[1]):
            mae_str = f"{mae:5.1f}°" if mae is not None else "    —"
            marker = "  HARD" if wr < 40 else "  EASY" if wr >= 70 else ""
            print(f"  {city:<16s} {n:4d}  {wr:5.1f}%  ${pnl:>8.2f}  {mae_str}{marker}")

    # --- By day of week ---
    print(f"\n{'Day':<12s} {'n':>4s}  {'Win%':>6s}  {'PnL':>9s}")
    print("-" * 38)
    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for day in day_order:
        if day not in by_dow:
            continue
        d = by_dow[day]
        n = d["wins"] + d["losses"]
        wr = d["wins"] / n * 100 if n else 0
        print(f"  {day:<10s} {n:4d}  {wr:5.1f}%  ${d['pnl']:>8.2f}")

    # --- By strategy ---
    if len(by_strategy) > 1:
        print(f"\n{'Strategy':<12s} {'n':>4s}  {'Win%':>6s}  {'PnL':>9s}")
        print("-" * 38)
        for strat, s in sorted(by_strategy.items()):
            n = s["wins"] + s["losses"]
            wr = s["wins"] / n * 100 if n else 0
            print(f"  {strat:<10s} {n:4d}  {wr:5.1f}%  ${s['pnl']:>8.2f}")


# =========================================================================
# #3  Skip funnel
# =========================================================================

def skip_funnel() -> None:
    events = _load_jsonl(SKIP_FILE)
    if not events:
        print("\n  No skip events logged yet. Run a scan to populate.")
        return

    by_reason = defaultdict(lambda: {"count": 0, "edges": [], "spreads": []})
    for e in events:
        reason = e.get("reason", "unknown")
        r = by_reason[reason]
        r["count"] += 1
        edge = e.get("edge")
        if edge is not None:
            r["edges"].append(edge)
        spread = e.get("spread")
        if spread is not None:
            r["spreads"].append(spread)

    total = sum(r["count"] for r in by_reason.values())
    print(f"\n  Total skipped: {total}")
    print(f"\n{'Reason':<28s} {'Count':>6s}  {'%':>5s}  {'Avg Edge':>9s}  {'Avg Spread':>11s}")
    print("-" * 70)
    for reason in sorted(by_reason, key=lambda r: -by_reason[r]["count"]):
        r = by_reason[reason]
        pct = r["count"] / total * 100 if total else 0
        avg_edge = sum(r["edges"]) / len(r["edges"]) if r["edges"] else None
        avg_spread = sum(r["spreads"]) / len(r["spreads"]) if r["spreads"] else None
        edge_str = f"{avg_edge:+.3f}" if avg_edge is not None else "     —"
        spread_str = f"{avg_spread:.1f}°" if avg_spread is not None else "     —"
        print(f"  {reason:<26s} {r['count']:6d}  {pct:4.1f}%  {edge_str:>9s}  {spread_str:>11s}")


# =========================================================================
# CLI
# =========================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Weather trader analytics")
    parser.add_argument("--model-report", action="store_true", help="Per-model accuracy")
    parser.add_argument("--calibration", action="store_true", help="Confidence calibration")
    parser.add_argument("--city-report", action="store_true", help="Win rate by city/day/strategy")
    parser.add_argument("--skip-funnel", action="store_true", help="Skip reason analysis")
    parser.add_argument("--all", action="store_true", help="Run all reports")
    args = parser.parse_args()

    if not any([args.model_report, args.calibration, args.city_report, args.skip_funnel, args.all]):
        args.all = True

    if args.all or args.model_report:
        print("\n📊 Per-Model Accuracy Report")
        print("=" * 55)
        model_report()

    if args.all or args.calibration:
        print("\n📊 Confidence Calibration Report")
        print("=" * 55)
        calibration_report()

    if args.all or args.city_report:
        print("\n📊 City / Time Stratification Report")
        print("=" * 55)
        city_report()

    if args.all or args.skip_funnel:
        print("\n📊 Skip Funnel Report")
        print("=" * 70)
        skip_funnel()

    print()
